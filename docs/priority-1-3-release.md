# Priority 1.3 release procedure

This is the authoritative, fail-closed build and release order for Priority 1.3. Run it only from a reviewed release commit and only in the approved production change window. The commands below deliberately separate commit/export, acceptance-manifest preparation, outer-spec freezing, backup, and guarded release.

## 1. Review and explicitly include release sources

Required new production and test files must be explicitly included in the release commit. Review `git status --short`, then name every approved path after `git add --`. **Never use `git add -A` for a release.** In particular, verify that the reviewed commit contains:

- `acceptance_artifacts.py`
- `release_argparse.py`
- `app/wsgi.py`
- executable `scripts/verify_priority_1_3_remote.py`
- `deploy/release_bundle.py`
- `deploy/guarded_priority_1_3_release.py`
- `deploy/verify_priority_1_3_acceptance.py`
- their owning tests, including `tests/test_release_gate_cli_and_build.py`
- this procedure and the README link

Before proceeding, require a clean worktree and an empty index relative to the selected commit:

```bash
git status --short
git diff --cached --quiet
SOURCE_SHA="$(git rev-parse --verify HEAD^{commit})"
test "${#SOURCE_SHA}" -eq 40
```

Do not build from copied working-tree files. `prepare-release-tree` uses `git archive` for the exact 40-character commit and fails if a mandatory file exists only as an untracked or uncommitted working-tree file.

## 2. Run the literal parser and build gate tests

```bash
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -B -m unittest \
  tests.test_release_gate_cli_and_build.ReleaseTreePreparationTests \
  tests.test_release_gate_cli_and_build.DuplicateReleaseCliTests -v

PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -B -m unittest \
  tests.test_release_bundle \
  tests.test_guarded_priority_1_3_release \
  tests.test_priority_1_3_acceptance \
  tests.test_production_test_verifier -v
```

The first command is the parser-test literal: it exhaustively duplicates every value-bearing option owned by the release bundle subcommands, guarded release, privileged helper request envelopes, acceptance collector, and installed remote verifier. Every duplicate must terminate parsing with `SystemExit(2)`; the same suite also proves positive dispatch.

## 3. Prepare the exact release tree

Choose fresh absolute paths. The production interpreter must be the interpreter used by the installed service and must have the production dependencies, including the RGB Matrix binding.

```bash
REPOSITORY="$(pwd -P)"
STAGE="/root/matrix-priority-1-3-${SOURCE_SHA}"
PRODUCTION_PYTHON="/home/admin/matrix/venv/bin/python"

./.venv/bin/python -I -B deploy/release_bundle.py prepare-release-tree \
  --repository "$REPOSITORY" \
  --source-commit "$SOURCE_SHA" \
  --stage "$STAGE" \
  --python "$PRODUCTION_PYTHON" \
  --uid 0 \
  --gid 0
```

The command performs this fixed order internally:

1. resolve and archive the exact commit;
2. copy only the production export into fresh `$STAGE/release`;
3. validate mandatory topology, executable mode, and committed runtime/import files;
4. run `prepare_acceptance_manifest`, creating `.matrix-release.json` inside the installable tree;
5. import `app.wsgi` from a copied installed tree using `PRODUCTION_PYTHON -I -B` semantics;
6. freeze `$STAGE/release-spec.json`, including the already-created inner manifest;
7. stage the exact exported `deploy/release_bundle.py` as `$STAGE/release_bundle.py`.

A preseeded `.matrix-release.json`, missing commit-export file, non-executable verifier, WSGI import failure, existing stage, or attempt to freeze the outer spec before the inner manifest fails closed.

## 4. Create the immutable backup and manifests

All paths below must be fresh where required. Record whether `matrix-web.service` was active before the transaction; the example uses `active`.

```bash
DESTINATION_ROOT="/home/admin/matrix"
BUNDLE="/root/matrix-priority-1-3-backup-${SOURCE_SHA}"
RELEASE_MANIFEST="$STAGE/release-manifest.json"
BACKUP_MANIFEST="$BUNDLE/backup-manifest.json"
RELEASE_SPEC="$STAGE/release-spec.json"

"$PRODUCTION_PYTHON" -I -B "$STAGE/release_bundle.py" backup \
  --release-root "$STAGE/release" \
  --destination-root "$DESTINATION_ROOT" \
  --bundle "$BUNDLE" \
  --release-manifest "$RELEASE_MANIFEST" \
  --backup-manifest "$BACKUP_MANIFEST" \
  --service matrix-web.service \
  --release-spec "$RELEASE_SPEC"

RELEASE_MANIFEST_DIGEST="$(sha256sum "$RELEASE_MANIFEST" | cut -d' ' -f1)"
BACKUP_MANIFEST_DIGEST="$(sha256sum "$BACKUP_MANIFEST" | cut -d' ' -f1)"
RELEASE_SPEC_DIGEST="$(sha256sum "$RELEASE_SPEC" | cut -d' ' -f1)"
```

Do not regenerate or edit the tree, inner manifest, outer spec, release manifest, or backup after these digests are recorded.

## 5. Run the guarded transaction with the complete current argv

Set reviewed values for every placeholder. `ATTEMPT_BUDGET_SECONDS` must cover acceptance; the current guarded topology uses 6600 seconds with a 60-second commit reserve and a 7200-second watchdog.

```bash
./.venv/bin/python -I -B deploy/guarded_priority_1_3_release.py run \
  --boundary b1 \
  --ssh-target "$SSH_TARGET" \
  --ssh-identity-file "$SSH_IDENTITY_FILE" \
  --ssh-known-hosts-file "$SSH_KNOWN_HOSTS_FILE" \
  --stage "$STAGE" \
  --bundle "$BUNDLE" \
  --destination-root "$DESTINATION_ROOT" \
  --service matrix-web.service \
  --initial-service-state active \
  --health-url http://127.0.0.1:5000/ \
  --health-status 302 \
  --health-timeout-seconds 30 \
  --attempt-budget-seconds 6600 \
  --commit-reserve-seconds 60 \
  --watchdog-seconds 7200 \
  --expected-sha "$SOURCE_SHA" \
  --expected-version "$EXPECTED_VERSION" \
  --previous-sha "$PREVIOUS_SHA" \
  --previous-version "$PREVIOUS_VERSION" \
  --transaction-version 1 \
  --transaction-registry-root /run/matrix-priority-1-3/transactions \
  --destination-identity matrix-production \
  --release-stamp "$RELEASE_STAMP" \
  --release-manifest-digest "$RELEASE_MANIFEST_DIGEST" \
  --backup-manifest-digest "$BACKUP_MANIFEST_DIGEST" \
  --release-spec-digest "$RELEASE_SPEC_DIGEST" \
  --session-cookie-file "$SESSION_COOKIE_FILE" \
  --evidence-dir "$EVIDENCE_DIR" \
  --gitlab-project "$GITLAB_PROJECT_ID" \
  --gitlab-mr "$GITLAB_MR_IID" \
  --gitlab-token-file "$GITLAB_TOKEN_FILE" \
  --chromium "$CHROMIUM"
```

Use `--boundary b2` only when the approved boundary is B2. Use `--resume` only to reconcile the same canonical transaction identity and immutable manifests after an interrupted invocation; never use it to change candidate identity, paths, digests, scope, operation, or timeouts.
