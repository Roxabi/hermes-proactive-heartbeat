# hermes-proactive-heartbeat

Native Hermes plugin for an extensible proactive heartbeat: collectors emit signals, deterministic gates and optional TypeSafe judgments choose whether to wake the agent, and Hermes Cron owns scheduling, the `wakeAgent` gate, and delivery.

## Install

Pin an exact commit SHA:

```bash
hermes plugins install Roxabi/hermes-proactive-heartbeat --ref a6896b9b194ed9bbea955acf233552d2f0e62782
hermes plugins enable proactive-heartbeat
hermes proactive-heartbeat setup
```

Configure delivery and use cases under the active `$HERMES_HOME` (`~/.hermes` by default, or a named profile home):

```yaml
# $HERMES_HOME/config.yaml
plugins:
  entries:
    proactive-heartbeat:
      enabled: true
      settings:
        typesafe_threshold: 0.65
        default_cooldown_seconds: 14400
        delivery:
          schedule: "every 15m"
          target: "discord"          # Hermes --deliver grammar
          # failure_target: "local"
        use_cases:
          sense:
            enabled: true
            command: ["sense-status", "--json"]
          host:
            enabled: true
            hosts:
              - name: local
          stale_prs:
            enabled: true
            owner: your-org
            age_days: 7
          dependabot_alerts:
            enabled: true
            owner: your-org
            age_days: 3
```

Optional TypeSafe key (deterministic fallbacks stay active without it):

```bash
# $HERMES_HOME/.env
TYPESAFE_API_KEY=...
```

## Operator commands

```bash
hermes proactive-heartbeat setup    # write $HERMES_HOME/scripts shim + reconcile cron job
hermes proactive-heartbeat tick     # one collect/decide cycle; prints wake-gate stdout
hermes proactive-heartbeat status   # concise local status (no secrets)
hermes proactive-heartbeat doctor   # local health checks
```

Silent ticks print exactly:

```json
{"wakeAgent": false}
```

Wake ticks print one compact JSON object with `heartbeat_candidate` (no `wakeAgent` line). Hermes Cron reads that stdout contract.

## Architecture

| Layer | Owner |
| --- | --- |
| Schedule / wake gate / delivery | Hermes Cron |
| Collect / dedupe / TypeSafe batch / candidate | this plugin |
| Registration only (no background loops) | `register(ctx)` |

Durable plugin state lives under `$HERMES_HOME/plugin-data/` via `ctx.state`. Setup never edits `jobs.json` directly; it uses `hermes cron create|edit`.

## Add a use case

1. Implement `HeartbeatUseCase.collect(context, previous_state) -> Snapshot` (see `models.py`).
2. Emit stable fingerprints and compact facts; put trusted `ActionSpec` values in each signal's `JudgmentSpec`.
3. Register the case from `registry.build_registry(settings)` behind an explicit `use_cases.<id>.enabled: true` gate.

## Remove

```bash
hermes plugins disable proactive-heartbeat
hermes cron remove proactive-heartbeat
# optional: hermes plugins uninstall proactive-heartbeat
```

See [after-install.md](after-install.md) for the first-run checklist.
