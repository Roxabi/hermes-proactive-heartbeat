# After install

1. Confirm the plugin is enabled for the active Hermes home/profile.
2. Put delivery settings under `plugins.entries.proactive-heartbeat.settings.delivery`.
3. Enable only the use cases you have collectors for (`sense`, `host`, `stale_prs`, `dependabot_alerts`).
4. Optionally set `TYPESAFE_API_KEY` in `$HERMES_HOME/.env`. Without it, ticks still run on deterministic fallbacks.
5. Run `hermes proactive-heartbeat setup`.
6. Run `hermes proactive-heartbeat doctor`.
7. Run one manual `hermes proactive-heartbeat tick` and confirm stdout is either `{"wakeAgent": false}` or a single `heartbeat_candidate` object.
8. Confirm the Hermes gateway/cron scheduler is running so the job can fire.
