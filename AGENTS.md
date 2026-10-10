# AGENTS.md — how to work on Bongo Matrix

Read this first. It replaces the older multi-profile release process described in
`/root/MATRIX_HANDOFF.md.archived-*` and old worker state files; those are history, not instructions.

## Definition of done
A change is done when **all** of these are true:
1. A focused test that failed before the change passes after it.
2. The GitLab pipeline for the commit on `main` is green (MR pipeline, then merge).
3. If the change is user-visible and the owner wants it live: `python3 deploy/ship.py --apply`
   reported `DEPLOYED <version>`.

Nothing else is required. No frozen artifacts, receipts, independent review chains or
tracker readback unless the change is in the "needs a second reviewer" list below.

## Daily loop
1. Branch from `origin/main`.
2. Write the failing test, fix, run **only the affected test modules locally**:
   `/root/matrix_p17_review_remediation/.venv-ci/bin/python -m unittest tests.test_<module>`
   (browser tests: `MATRIX_CHROMIUM=/usr/bin/google-chrome`). Run `rm -f native-*.json temporal-*.json` afterwards.
3. Never run the full suite locally; GitLab runs it (sharded, ~half the old 33 min).
4. Push, open an MR to `main`, wait for green, merge. Cancel the duplicate push pipeline if one appears.
5. User-visible change → bump `APPLICATION_VERSION` in `webapp.py` (and the pin in `tests/test_webapp.py`).
   Tests must reference `webapp.APPLICATION_VERSION`, never a literal version.

## Deploying (when the owner asks to ship; "push to prod" is enough, no further gate)
```
python3 deploy/ship.py            # dry run: CI status, version, files that differ from production
python3 deploy/ship.py --apply    # deploy origin/main
python3 deploy/ship.py --rollback /var/tmp/matrix-ship-<n>   # printed after every deploy
```
`ship.py` refuses unless main's CI succeeded, the version changed and only runtime code differs.
It backs up code + state, publishes atomically, restarts, checks the public version and rolls
back by itself on failure. It never writes `data/`, `resources/media`, config, drafts, uploads,
secrets or the pairing PIN. Production checkout stays `admin:matrix-web` mode `0750`.
Result JSON: `/root/.local/state/matrix-ship/`. After a deploy tell the owner the version,
the rollback command, and that manual acceptance is theirs.

## Not release gates
- **Native-driver acceptance** (`deploy/feature_release_acceptance.py`, `TARGET_NATIVE_DRIVER`):
  failed repeatedly on live trials for reasons unrelated to the shipped features. It is tracked in
  GitLab issue #31 and is **report-only** until that issue is fixed. Do not block a release on it
  or start a diagnosis cycle from it unless that issue is the task.
- Physical-device CI job (`physical_device_acceptance`) runs only on protected `main` pushes and is
  not required for a deploy.

## Needs a second reviewer (use `matrix-release-auditor`) before merge
Only: `deploy/ship.py`, `deploy/ship_remote.py`, pairing/auth/secret handling, anything that writes
user state or config, systemd units. Everything else: one builder, CI is the reviewer.

## Notifications and history
- A background "completed (exit 0)" notice for work already recorded needs **no action**. Do not
  re-verify finished work because a late notice arrived.
- Exit code 0 means the process ended, not that the task succeeded — read its result file once.
- Current status lives in `/root/MATRIX_STATUS.md` (short). Update it at the end of a task.
- Old worktrees under `/root/archive/` and state under `.../hermes-worker-dashboard/archive/` are history.

## Owner preferences
- Progress updates: timestamped, start with `============= task X ================`, then current part and next step.
- Demos at `http://192.168.1.123:5001` (owner is remote; never localhost).
- Non-blocking bugs go to GitLab issues; don't stop delivery for them.
- Healthy production is not touched (no SSH tests, restarts or deploys) without a ship request from the owner.
