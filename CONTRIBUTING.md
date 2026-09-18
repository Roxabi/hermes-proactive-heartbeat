# Contributing

Thanks for helping improve `hermes-proactive-heartbeats`.

## Before you start

- Use Python 3.10 or newer.
- Use Hermes 0.21.3 or newer for integration checks.
- Open an issue before making a large behavioral or configuration change.
- Never commit credentials, personal paths, hostnames, repository names, channel IDs, or operator data.

## Repository boundary

This repository contains the generic plugin runtime only:

- collection orchestration;
- deterministic decisions, semantic TypeSafe batching, and deduplication;
- named heartbeat configuration loading;
- Hermes Cron reconciliation;
- the collector SDK.

Collector implementations and operator configuration belong under the active `$HERMES_HOME/proactive-heartbeats/` directory. They must not be added to this repository, including as examples copied from a live environment.

Hermes Cron owns scheduling, the `wakeAgent` script gate, and delivery. `register(ctx)` must remain registration-only and must not create files, start loops, or reconcile jobs.

## Local setup

Create a virtual environment and install the pinned development tool:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ruff==0.15.10
```

Run the same quality gates as CI:

```bash
ruff format --check .
ruff check .
python3 -m compileall -q .
python3 -m unittest discover -s tests -v
```

If Hermes is installed, also validate plugin discovery and registration:

```bash
hermes plugins doctor . --ci
```

## Tests

Tests use the standard-library `unittest` runner. Add or update tests only for observable behavior: configuration boundaries, state transitions, wake-gate output, Cron reconciliation, or real error handling. Keep tests deterministic and isolated from the user's actual `HERMES_HOME`.

A quiet tick must remain the exact standalone line:

```json
{"wakeAgent": false}
```

## Pull requests

1. Fork the repository and create a focused branch.
2. Keep the change generic and remove obsolete paths rather than adding compatibility shims.
3. Update `README.md` or `after-install.md` when installation or operator behavior changes, and add a `CHANGELOG.md` entry when behavior visible to an operator changes.
4. Run all quality gates above.
5. Explain the user-visible behavior and verification in the pull request.

## Releases

A release is a tag plus a GitHub release; there is no publishing step and no release automation.

1. Land every change through a PR — `main` is frozen against direct pushes.
2. In one release PR, bump `version:` in `plugin.yaml` and add the matching `CHANGELOG.md` section.
3. After CI is green and the PR is merged with a merge commit, tag that merge commit `proactive-heartbeats/vX.Y.Z` and create the GitHub release from the changelog section.
4. State the tag's commit SHA in the release notes and in the README install command — `hermes plugins install --ref` accepts only a 40-character SHA, never a tag.

`plugin.yaml` `version`, the changelog heading, and the tag must agree.

By contributing, you agree that your contribution is licensed under the repository's MIT license.
