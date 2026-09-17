# hermes-proactive-heartbeats

Native Hermes plugin for named proactive heartbeats: collectors emit signals, deterministic gates and optional TypeSafe judgments choose whether to wake the agent, and Hermes Cron owns scheduling, the `wakeAgent` gate, and delivery. One plugin, many heartbeats — each with its own JSON, cron job, shim, and persisted state.

## Install

Pin an exact full-length commit SHA (not a branch name):

```bash
# discover a pin
git ls-remote https://github.com/Roxabi/hermes-proactive-heartbeats.git HEAD
# or: gh api repos/Roxabi/hermes-proactive-heartbeats/commits/main --jq .sha

hermes plugins install Roxabi/hermes-proactive-heartbeats --ref <40-char-sha>
hermes plugins enable proactive-heartbeats
hermes proactive-heartbeats setup
```

`$HERMES_HOME/config.yaml` only enables the plugin (and optionally overrides the config directory):

```yaml
# $HERMES_HOME/config.yaml
plugins:
  enabled:
    - proactive-heartbeats
  entries:
    proactive-heartbeats:
      settings:
        config_dir: proactive-heartbeats   # default; relative to $HERMES_HOME
```

Root file — shared TypeSafe/cooldown defaults (`$HERMES_HOME/proactive-heartbeats/proactive-heartbeats.json`):

```json
{
  "typesafe_threshold": 0.65,
  "default_cooldown_seconds": 14400,
  "defaults": {
    "delivery": {
      "schedule": "every 15m",
      "target": "local"
    }
  }
}
```

Heartbeats are discovered by glob: `$HERMES_HOME/proactive-heartbeats/heartbeats/{name}.json`. Drop a file, run `setup`. Delete a file, `setup` removes that cron job. Root `defaults.delivery` is the base; the heartbeat file wins on conflict.

```json
{
  "delivery": {
    "schedule": "every 15m",
    "target": "local"
  },
  "context": {
    "who": "operator"
  },
  "collectors": {
    "probe": {
      "enabled": true,
      "token": "abc"
    }
  }
}
```

**Delivery target:** Hermes Cron `--deliver` grammar. Standalone CLI cron jobs have no chat `origin`, so prefer a concrete target such as `local`, `telegram`, `discord`, or `signal` (plus a configured home channel where applicable). `doctor` warns when a heartbeat still uses `origin`.

Root `context` plus heartbeat `context` (file wins per key) are copied into every wake payload as `heartbeat_candidate.context`, with `heartbeat` and `now` added at tick time. Collector observations go in `inputs`; TypeSafe/fallback choice in `judgment`.

Collectors are **not** shipped in this plugin. Each id in `collectors` loads `$HERMES_HOME/proactive-heartbeats/collectors/{id}.py` (user-owned). The plugin only provides the runtime and a small SDK (`models`, `collector_exec`). An enabled collector whose file is missing or unloadable fails `tick` and `doctor` (no quiet success).

`setup` writes the root skeleton and `collectors/` / `heartbeats/` dirs if missing, never overwrites existing files, and reconciles one Hermes cron job per heartbeat file (`proactive-heartbeats-{name}`). The shim runs `hermes proactive-heartbeats tick --name {name}`.

### Collector module

`$HERMES_HOME/proactive-heartbeats/collectors/probe.py`:

```python
from models import ActionSpec, JudgmentSpec, Signal, Snapshot, TickContext

SILENT = ActionSpec(
    name="silent",
    priority=0,
    instruction="Do not message the user.",
    max_sentences=0,
)
NOTIFY = ActionSpec(
    name="notify",
    priority=50,
    instruction="Tell the user the probe fired in one short sentence.",
    max_sentences=1,
)


class Collector:
    id = "probe"  # must match the filename stem

    def __init__(self, config: dict) -> None:
        self._config = dict(config)

    def collect(self, context: TickContext, previous_state: dict) -> Snapshot:
        del context, previous_state
        token = str(self._config.get("token") or "").strip()
        if not token:
            return Snapshot(state={"ok": False}, diagnostics={"error": "unconfigured"})
        return Snapshot(
            signals=(
                Signal(
                    fingerprint=f"probe:{token}",
                    facts={"token": token},
                    judgment=JudgmentSpec(
                        question={
                            "type": "choice",
                            "instructions": "Should the agent notify about this probe?",
                            "criteria": {
                                "silent": "Noise or already handled.",
                                "notify": "Worth a short proactive message.",
                            },
                        },
                        actions={"silent": SILENT, "notify": NOTIFY},
                        fallback_label="notify",
                    ),
                ),
            ),
            state={"ok": True, "token": token},
        )
```

JSON envelope under `collectors.<id>`:

| key | required | meaning |
| --- | --- | --- |
| `enabled` | yes | must be `true` to load |
| *(anything else)* | no | passed as `config` to the collector |

Helpers for command/SSH collectors: `collector_exec.resolve_source`, `load_json_payload`, `build_argv`.

Optional TypeSafe key (deterministic fallbacks stay active without it):

```bash
# $HERMES_HOME/.env
TYPESAFE_API_KEY=...
```

## Operator commands

```bash
hermes proactive-heartbeats setup            # root skeleton + one cron per heartbeats/*.json
hermes proactive-heartbeats tick --name care # one collect/decide cycle
hermes proactive-heartbeats status           # concise local status (no secrets)
hermes proactive-heartbeats doctor           # config, shim, cron job, collector import
```

`doctor` validates heartbeat JSON, imports enabled collectors, checks shims, and looks up Cron jobs. It does not prove the scheduler is currently firing — after `setup`, run one manual `tick --name …` and confirm either a wake JSON object or exactly `{"wakeAgent": false}` with exit code 0. Collector load/execution failures exit non-zero (Cron failure delivery), never a quiet success.

A second cron that ran the same `tick --name` would share that heartbeat's config and clobber its dedupe state. Do not do that — `setup` owns the job names.

Silent ticks print exactly:

```json
{"wakeAgent": false}
```

Wake ticks print one compact JSON object with `heartbeat_candidate` (no `wakeAgent` line). Hermes Cron reads that stdout contract.

### Tick semantics (short)

- **First tick** is a silent baseline: active fingerprints are stored and stamped `action: baseline` so cooldown can expire and re-evaluate later.
- **Non-winning** due candidates stay in `pending` and are reconsidered on the next tick.
- **Silent** TypeSafe/fallback decisions are stamped so they are not suppressed forever; they become due again after cooldown.
- Soft `Snapshot.diagnostics` stay in use-case state; hard collector exceptions / bad return types fail the tick.

## Architecture

| Layer | Owner |
| --- | --- |
| Schedule / wake gate / delivery | Hermes Cron (one job per heartbeat) |
| Collect / dedupe / TypeSafe batch / candidate | this plugin (state key `heartbeat:{name}`) |
| Registration only (no background loops) | `register(ctx)` |

Durable plugin state lives under `$HERMES_HOME/plugin-data/` via `ctx.state`. Setup never edits `jobs.json` directly; it uses `hermes cron create|edit`.

## Add a collector

1. Write `$HERMES_HOME/proactive-heartbeats/collectors/{id}.py` with a `Collector` (or any class with `collect()` and matching `id`).
2. Emit stable fingerprints and compact facts; put trusted `ActionSpec` values in each signal's `JudgmentSpec`.
3. Enable it from a heartbeat JSON: `collectors.{id}.enabled: true`.
4. Run `hermes proactive-heartbeats doctor` then one `tick --name …`.

## Remove

```bash
hermes plugins disable proactive-heartbeats
hermes cron remove proactive-heartbeats-<name>   # per heartbeat
# optional: hermes plugins uninstall proactive-heartbeats
```

See [after-install.md](after-install.md) and [CONTRIBUTING.md](CONTRIBUTING.md).
