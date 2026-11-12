#!/usr/bin/env pwsh
# Local CI is intentionally kept aligned with GitHub Actions while billing is paused.
# Usage: pwsh scripts/ci.ps1 [-Mode fast|full|nightly] [-NoLint] [-NoTest] [-Serial]
# full/nightly also run npm ci + eslint + tsc and the Playwright UI smoke test (fast runs eslint/tsc when node_modules exists).
# fast/full tests run on 8 xdist workers (the suite is mostly sleeps/subprocess waits); -Serial opts out.
# full enforces a coverage floor (measured 65% on 2026-11-04; raise it as coverage improves).
param(
    [ValidateSet("fast", "full", "nightly")]
    [string]$Mode = "fast",
    [switch]$NoLint,
    [switch]$NoTest,
    [switch]$Serial
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot

Push-Location $root
try {
    $skipped = @()

    $fullish = $Mode -eq "full" -or $Mode -eq "nightly"
    $groups = @("--group", "dev")
    if ($fullish) { $groups += @("--group", "browser") }  # Playwright for the real-browser UI smoke test
    Write-Host "==> uv sync --locked $($groups -join ' ')" -ForegroundColor Cyan
    uv sync --locked @groups
    if ($LASTEXITCODE -ne 0) { throw "uv sync --locked failed" }

    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) {
        $skipped += "node (JS module tests will skip)"
        Write-Host "==> node: MISSING (JS tests skip via pytest mark)" -ForegroundColor Yellow
        if ($Mode -eq "full" -or $Mode -eq "nightly") {
            throw "node is required for the $Mode CI suite"
        }
    } else {
        Write-Host "==> node: $($node.Source)" -ForegroundColor Cyan
        if ($fullish) {
            Write-Host "==> npm ci (eslint / typescript dev tooling)" -ForegroundColor Cyan
            npm ci --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
            Write-Host "==> playwright chromium" -ForegroundColor Cyan
            uv run playwright install chromium   # no-op when this playwright's build is cached
            if ($LASTEXITCODE -ne 0) { throw "playwright install chromium failed" }
        }
    }

    Write-Host "==> example YAML smoke" -ForegroundColor Cyan
    uv run python -c @"
from pathlib import Path
from tqdaemon.config import Project
from tqdaemon.taskspec import parse_task_yaml
root = Path('examples/minimal')
qdir = root / 'experiments' / 'operator_queue'
proj = Project(name='minimal', root=root, queue_dir=qdir)
path = qdir / 'hello-progress.yaml'
spec = parse_task_yaml(path, proj)
assert spec.task_id == 'hello-progress', spec
print('ok', spec.task_id)
"@
    if ($LASTEXITCODE -ne 0) { throw "example YAML smoke failed" }

    if (-not $NoLint) {
        Write-Host "==> ruff check" -ForegroundColor Cyan
        uv run ruff check tqdaemon/ tests/ scripts/
        if ($LASTEXITCODE -ne 0) { throw "ruff check failed" }
        Write-Host "==> ruff format check" -ForegroundColor Cyan
        uv run ruff format --check tqdaemon/ tests/ scripts/
        if ($LASTEXITCODE -ne 0) { throw "ruff format check failed" }
        Write-Host "==> mypy (worker core + extracts)" -ForegroundColor Cyan
        uv run mypy
        if ($LASTEXITCODE -ne 0) { throw "mypy failed" }
        if ($node -and (Test-Path (Join-Path $root "node_modules/.bin"))) {
            Write-Host "==> eslint (web UI)" -ForegroundColor Cyan
            npm run -s lint
            if ($LASTEXITCODE -ne 0) { throw "eslint failed" }
            Write-Host "==> tsc --checkJs (// @ts-check files)" -ForegroundColor Cyan
            npm run -s typecheck
            if ($LASTEXITCODE -ne 0) { throw "tsc typecheck failed" }
        } else {
            $skipped += "eslint/tsc (run npm ci)"
        }
        Write-Host "==> progressive annotation hints (advisory)" -ForegroundColor Cyan
        uv run ruff check tqdaemon/ tests/ scripts/ --select ANN --exit-zero --output-format concise
    }

    Write-Host "==> generated docs" -ForegroundColor Cyan
    uv run python scripts/build_docs.py --check
    if ($LASTEXITCODE -ne 0) { throw "help bundle is stale (run scripts/build_docs.py)" }
    uv run python scripts/check_docs.py
    if ($LASTEXITCODE -ne 0) { throw "check_docs failed" }

    if (-not $NoTest) {
        $marker = if ($Mode -eq "fast" -or $Mode -eq "full") { "not stress and not slow and not nightly" } else { "stress or slow or nightly" }
        if ($Mode -eq "nightly") { $marker = "stress or slow or nightly" }
        Write-Host "==> pytest (-m `"$marker`")" -ForegroundColor Cyan
        $pytestArgs = @("pytest", "tests/", "-q", "-m", $marker, "--durations=20")
        if (-not $Serial -and $Mode -ne "nightly") {  # stress fixtures stay serial
            $pytestArgs += @("-n", "8")
        }
        if ($Mode -eq "full" -or $Mode -eq "nightly") {
            $pytestArgs += @("--cov=tqdaemon", "--cov-report=term-missing")
        }
        if ($Mode -eq "full") {
            $pytestArgs += @("--cov-fail-under=64")  # nightly runs only stress/slow tests
        }
        uv run @pytestArgs
        if ($LASTEXITCODE -ne 0) { throw "pytest failed" }
    }

    if ($Mode -eq "full" -or $Mode -eq "nightly") {
        Write-Host "==> package build" -ForegroundColor Cyan
        uv build
        if ($LASTEXITCODE -ne 0) { throw "uv build failed" }
        Write-Host "==> dependency consistency" -ForegroundColor Cyan
        uv pip check
        if ($LASTEXITCODE -ne 0) { throw "uv pip check failed" }
        Write-Host "==> tq doctor" -ForegroundColor Cyan
        $doctorHome = Join-Path $env:TEMP "tqdaemon-ci-doctor"
        New-Item -ItemType Directory -Force -Path $doctorHome | Out-Null
        $env:TQDAEMON_HOME = $doctorHome
        uv run tq doctor --json
        if ($LASTEXITCODE -ne 0) { throw "tq doctor failed" }
    }

    if ($Mode -eq "nightly") {
        Write-Host "==> scale benchmark (N=20000)" -ForegroundColor Cyan
        uv run python scripts/bench_scale.py 20000
        if ($LASTEXITCODE -ne 0) { throw "scale benchmark failed" }
    }

    if ($skipped.Count -gt 0) {
        Write-Host "==> skipped toolchains: $($skipped -join '; ')" -ForegroundColor Yellow
    }
    Write-Host "==> OK" -ForegroundColor Green
} finally {
    Pop-Location
}
