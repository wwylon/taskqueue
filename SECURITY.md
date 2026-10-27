# Security

## Reporting

Email the maintainer listed on the GitHub profile for `wwylon/taskqueue`, or open a **private** GitHub security advisory if enabled. Do not post exploit details in public issues.

## Deployment notes

Default bind is loopback (`127.0.0.1`). See [docs/security.md](docs/security.md) for `auth_token`, scoped API tokens, and worker auth. Tasks run with the privileges of the daemon user and are not sandboxed — treat YAML and issue-supplied commands as untrusted input.
