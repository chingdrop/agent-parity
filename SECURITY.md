# Security policy

## Supported versions

Security fixes are made against the latest `v1.x` release. Older releases are not patched.

| Version      | Supported |
|--------------|-----------|
| 1.x (latest) | Yes       |

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private vulnerability reporting instead:

[Report a vulnerability](https://github.com/chingdrop/agent-parity/security/advisories/new)

Include the affected version or commit, steps to reproduce, and the impact you see.

Backup contact: <!-- TODO(craig): backup security contact email, if wanted -->

## What to expect

This is a single-maintainer project, so these are best-effort targets: an acknowledgement within 7 days and an
assessment or plan within 30 days. Fixes are coordinated with the reporter before disclosure, and reporters are credited
unless they ask not to be.

## Scope

This repository contains synthetic demo data only (the fictional clients Acme Corp and Globex, on `*.example` domains).
No real credentials are ever committed: `.env` is gitignored, `config.yaml` holds only `${VAR}` references, and CI runs
gitleaks. If you find something that looks like a real credential or real environment data in the repository or its
history, please report it privately the same way.

In scope: the code, configuration, CI workflows and dependencies in this repository. Out of scope: vulnerabilities in
the EDR vendors' products or APIs, in Active Directory itself, or in how someone else has deployed or configured this
tool.

For how the tool handles credentials and data, see the [threat model](docs/threat-model.md).
