# After install

1. Confirm the plugin is enabled (`hermes plugins enable proactive-heartbeats`).
2. Run `hermes proactive-heartbeats setup` — it writes `$HERMES_HOME/proactive-heartbeats/proactive-heartbeats.json` if missing, plus empty `heartbeats/` and `collectors/` dirs.
3. Add a collector module at `$HERMES_HOME/proactive-heartbeats/collectors/{id}.py`.
4. Add a heartbeat file `$HERMES_HOME/proactive-heartbeats/heartbeats/{name}.json` with `delivery` (use a concrete `--deliver` target such as `local`, not bare `origin` for CLI jobs) and `collectors.{id}.enabled: true`.
5. Re-run `setup` so that heartbeat gets a cron job and shim. Deleting the JSON removes the job on the next setup.
6. Optionally override the directory with `plugins.entries.proactive-heartbeats.settings.config_dir`.
7. Optionally set `TYPESAFE_API_KEY` in `$HERMES_HOME/.env`. Without it, ticks still run on deterministic fallbacks.
8. Run `hermes proactive-heartbeats doctor`.
9. Run one manual `hermes proactive-heartbeats tick --name <name>` and confirm stdout is either `{"wakeAgent": false}` or a single `heartbeat_candidate` object.
10. Confirm the Hermes gateway/cron scheduler is running so the jobs can fire.
