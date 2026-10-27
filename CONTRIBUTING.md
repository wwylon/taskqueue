# Contributing

## Setup

```text
git clone https://github.com/wwylon/taskqueue.git
cd taskqueue
uv sync --group dev
uv run tq doctor
```

## Tests

```text
uv run ruff check taskqueue/ tests/
uv run pytest -q -m "not stress"
uv run python scripts/build_docs.py --check
```

## Pull requests

- Open PRs against the public repository.
- Link an issue when fixing a bug; include `tq doctor` / OS / version for runtime problems.
- Maintainers may replay accepted changes through an internal mirror; your commits can appear rewritten in the next public export. Attribution is preserved in CHANGELOG / PR comments when practical.

## Security

See [SECURITY.md](SECURITY.md). Do not file vulnerabilities in public issues.
