# Changelog

All notable changes to this plugin are documented here. Versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html); releases are tagged
`proactive-heartbeats/vX.Y.Z`.

## 0.2.1 — 2026-09-18

### Fixed

- `delivered` in the heartbeat state only ever grew. Measured on a live install, a tick still carried records for fingerprints the collector had stopped emitting hours earlier, so every collector minting content-addressed or date-anchored fingerprints — a daily digest keyed `…:digest:{date}`, a delta keyed by a hash of the new items — added at least one permanent record per day. The engine now persists a delivery record only while its fingerprint is in that collector's active set for the tick being persisted.
- This is a behavioral no-op by construction: `_is_due` returns `True` for a fingerprint absent from its collector's previous active set **before** it reads `delivered`, so a record for an inactive fingerprint can suppress nothing, at any age and for any `repeat_after_seconds`. Dropping it changes no decision.
- Retention is fail-closed. A collector that failed this tick has no fresh active set, so every one of its records is kept untouched, on the fail-closed return as well as the normal one. A key that names no collector loaded by this heartbeat — a collector temporarily disabled in the heartbeat JSON, for instance — is kept too: pruning never shreds state it cannot judge.
- Collectors that need memory outliving a fingerprint must keep it in their own `Snapshot.state`; `context.delivered` now covers the fingerprints active on the previous tick, not the whole history.

### Tests

- Five cases pin the observable rule: a vanished fingerprint's record is gone from the next persisted state, a still-active record survives and keeps suppressing inside its cooldown, a failed collector's records all survive, an unloaded collector's records survive, and a fingerprint containing `:` is matched to its own collector rather than split at the wrong separator.

## 0.2.0 — 2026-09-18

### Added

- `TickContext.delivered`: the delivery records the engine persisted for **the collector being invoked**, keyed by its own fingerprints with no `{collector}:` prefix, each holding `{"at": "<iso8601>", "action": "<action name>"}`. A collector could see what it observed, never what the engine actually announced: a signal stamped `silent` because a higher-priority signal won the tick was indistinguishable from one that really woke the agent. Digest and delta collectors need that difference — "list everything once a day, otherwise only what is new" is not implementable without it. Action `baseline` means the fingerprint was only baselined and `silent` that it was due but did not wake the agent, so both mean "not announced to the user"; any other name is the action that woke it. A collector never sees a sibling collector's fingerprints, and the first tick sees `{}`.

### Migration

- No collector change is required. Collectors that construct a `TickContext` themselves — tests, harnesses — get the new field by default (an empty mapping), and `collect()` keeps its signature. The persisted state schema, `STATE_VERSION`, the gates, and the stdout contract are untouched.

## 0.1.2 — 2026-09-18

### Documentation

- First-tick semantics were described as "recorded without resolution", which reads as "due on the next tick". The actual behavior — verified on a live install — is that a `baseline` signal is stamped `delivered: baseline`, which **starts the cooldown clock**: it can only wake one cooldown later (4 h by default). A fingerprint that first appears on any later tick has no delivery record and is due immediately.
- `Cooldown` now states that it also applies to a baseline stamp, and that `repeat_after_seconds: 0` means "due on every tick while active".
- Collector guidance is explicit: anything that must not sit unreported for a cooldown window belongs in `initial_observation="eligible"`.

### Tests

- Added coverage for a signal already active at the baseline tick: silent at the stamp, still silent one second before the cooldown elapses, due exactly when it does. The existing cooldown tests all introduced their signal after the baseline tick, so this timing path was unpinned.

No runtime change: the engine behaves exactly as in 0.1.1.

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
