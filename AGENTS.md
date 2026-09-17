# Repository contract

Public native Hermes plugin. Keep the runtime generic: hostnames, repositories, channels, personal paths, and secrets belong under the active `HERMES_HOME`, never in this repository.

## Runtime boundaries

- Hermes Cron owns scheduling, the `wakeAgent` script gate, and delivery.
- The plugin owns collection, deterministic gates, TypeSafe batching, deduplication, and plugin state.
- `register(ctx)` only registers surfaces; setup and filesystem writes happen through explicit CLI commands.
- A quiet tick ends with the exact standalone line `{"wakeAgent": false}`.

## Quality

Run the commands declared in `.dev/stack.yml`. Tests use the standard library `unittest` runner and assert observable behavior. Lefthook is the local net: ruff on pre-commit, unittest on pre-push. CI is the authority gate. `compileall` is bytecode smoke, not a typechecker.
