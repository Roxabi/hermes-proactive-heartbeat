# Changelog

All notable changes to this plugin are documented here. Versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html); releases are tagged
`proactive-heartbeats/vX.Y.Z`.

## 0.1.1 — 2026-09-18

### Fixed

- Install docs: `hermes plugins install --ref` rejects anything that is not a full 40-character commit SHA, so the tag-based command shipped in 0.1.0 could not run. The install section now resolves a release tag to its commit — and warns that an unpeeled `git ls-remote` returns the annotated tag object, not a commit.
- Concrete SHAs now live in the release notes only. Hardcoding one in the tree pins a release the tree has already moved past.

No runtime change: the 0.1.0 collector SDK, engine behavior, and stdout contract are untouched.

## 0.1.0 — 2026-09-18

First tagged release. Nothing before this tag was published, so the contract below is the baseline rather than a delta.

### Install

`hermes plugins install --ref` takes only a 40-character commit SHA, so pin this release by its tagged commit:

```bash
hermes plugins install Roxabi/hermes-proactive-heartbeats --ref a9a5d883135df15ee609c98aaffe4ab37feb0372
```

### Decide before the model call

- A tick collects facts in plain Python, gates them deterministically, and prints exactly `{"wakeAgent": false}` when nothing is due — no agent wake, no tokens.
- When something is due, stdout is one `heartbeat_candidate` carrying the compact facts and the already-selected action, so the woken agent writes the message instead of re-deciding whether to speak.

### Collector SDK

- `Signal.decision` takes exactly one policy: a deterministic `ActionSpec` rule resolved in-process, or a semantic `JudgmentSpec` resolved by an optional TypeSafe batch. There is no dual form.
- `ActionSpec` requires an explicit `wake_agent` boolean; `SILENT` is the exported canonical non-waking action.
- `Signal.initial_observation` chooses the first-tick policy per signal: `"baseline"` (default) records a newly seen fingerprint without resolving it, `"eligible"` resolves it immediately so a critical condition can wake on tick 1.
- Candidate selection is deterministic: highest `priority`, then collector id, then fingerprint. A semantic decision never outranks a rule by virtue of being semantic.
- Decision provenance is stamped on the payload as `rule`, `typesafe`, or `fallback`.

### Named heartbeats

- One plugin serves many heartbeats. Each `heartbeats/{name}.json` owns one Hermes cron job, one generated shim, and one state key `heartbeat:{name}`.
- Collector implementations and operator policy live under `$HERMES_HOME/proactive-heartbeats/`, never in this repository.
- `setup` is idempotent: it reconciles one cron job per heartbeat file and removes the job when the file is deleted.
- Heartbeat state is versioned (`STATE_VERSION = 2`); a record written by another version is discarded rather than migrated.

### Operations

- `setup`, `tick --name`, `status`, and `doctor` commands; collector load or execution failures exit non-zero instead of reporting a quiet success.
- Pending decisions are reused while a signal stays active with unchanged facts, so an unresolved judgment costs at most one model call.
- TypeSafe is optional: without `TYPESAFE_API_KEY`, rules stay deterministic and semantic decisions use their configured fallback.
