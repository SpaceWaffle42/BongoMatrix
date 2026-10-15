# GitLab CI workflow

The project-owned pipeline is the source of truth for broad automated verification of each pushed commit. It does not deploy to the Raspberry Pi or replace physical 64×32 checks.

## Development handoff

Use this sequence to avoid repeating deterministic work in an agent session:

1. Inspect governing requirements, the relevant source/tests, and discovery ownership.
2. Write and run one focused failing test.
3. Implement the change and rerun the focused test.
4. Review the exact diff. Commit/push only when the current assignment authorizes publication; local-only assignments leave edits uncommitted.
5. Let GitLab run repository-wide syntax, tests, browser/media coverage, Ruff, dependency auditing, SAST, and secret detection.
6. Read `ci-summary.txt` or `ci-summary.json` first. Open an individual job log or report only when that summary is `FAIL` or `INCOMPLETE`.
7. After merge, verify deployment identity, service health, rollback readiness, and the physical panel separately.

Do not rerun the complete suite, repository-wide lint, coverage, dependency audit, SAST, secret detection, or Chromium regression locally after GitLab has passed them against the same commit. Repeat a broad check only when CI used a different commit, an artifact is missing, CI does not exercise the required environment, or independent reproduction was explicitly requested.

## Collection and local verification

`full_test_suite` runs as GitLab `parallel: 2` shards. Each shard first runs
`scripts/ci_collection.py --output shards/collection-$CI_NODE_INDEX.json` with the
same dependency-bearing interpreter as canonical discovery. It loads, but does not
execute, every `tests/test_*.py` module. Import/load errors, empty collection,
foreign imported TestCases, duplicate IDs, or missing required named behaviors fail
before the long run. This is not a replacement test tier: the complete suite remains
mandatory, including real Chromium/media journeys.

Each shard then runs `scripts/ci_shard.py run` under `coverage run`. It loads the
whole suite with the canonical loader (`discover -s tests`, so every module is
imported exactly as in a single-process run; any load error fails every shard) and
executes only its share. Shards are whole TestCase classes, so `setUpClass` and
in-class order are unchanged, balanced longest-processing-time first by
`ci/test-weights.json` (per-test seconds from pipeline 418; unknown tests count
`default_seconds`). Weights only balance wall time; a stale file cannot drop a test.
Refresh it when shard durations drift apart.

`test_results` joins the shards even when one fails. `ci_shard.py verify` requires
identical collection inventories, a complete set of shard manifests whose
assignments exactly partition the inventory, and JUnit cases covering every
collected test exactly once with nothing foreign. It writes
`shard-verification.json`, then `coverage combine` (via `ci/coveragerc`) produces the
Cobertura `coverage.xml`. `ci_summary` requires that proof, and its JUnit case count
must equal the summary test count, so a dropped or duplicated shard cannot pass.

`ci/required-tests.json` is an independent named-behavior preservation contract
seeded from exact base `7ae96302ada12fecc6e36d5494bfb1862962da47`, plus the new
collection/CI regression IDs. It is not regenerated in CI and contains no minimum
or exact suite count. Additions are allowed; removal/rename of required behaviors
requires explicit requirement review, not a changed total. Module ownership is
checked independently against the actual loader; the projection fixture imports
a module, never a foreign TestCase. Historical assembly method names are retained
for contract continuity, not numeric expectations. See `CI_TEST_AUDIT.md` for the
complete module/job accounting and dispositions.

For CI-equivalent offline execution use `node:22-bookworm`, the exact packages
and regular `/usr/bin/node` copy in `.gitlab-ci.yml`, the repository `.venv-ci`
and `.venv` compatibility symlink, umask `0022`, and `MATRIX_CHROMIUM=/usr/bin/chromium`.
Run disposable Docker verification with `--init` so orphaned browser children are
reaped; a sleep-only PID1 can make strict process-group cleanup fail.
Use a dedicated short sticky temporary root (e.g. `/tmp/mcm`, mode `1777`) for
unprivileged/socket fixtures. Local Chrome is an explicitly configured fallback,
not proof of the CI image. No device credentials or production target are needed
for canonical simulated-hardware tests; do not execute the physical job locally.
Keep raw logs private, record actual exits and hashes, and run one final frozen-byte
canonical suite after focused gates. Infrastructure/setup errors are not assertion
failures, skipped/partial runs are not GREEN, and local success is not remote CI
or physical acceptance.

## Pipeline evidence

The pipeline publishes the following reports for 14 days:

- per-shard collection inventories, shard manifests and JUnit XML from `full_test_suite`;
- `shard-verification.json` and combined Cobertura coverage from `test_results`;
- GitLab code-quality JSON from Ruff;
- `dependency-audit.json` from `pip-audit`;
- GitLab SAST and secret-detection reports;
- bounded `ci-summary.json` and `ci-summary.txt` acceptance summaries.

The GitLab schedule reruns the complete pipeline on `main` so dependency and security changes can be detected even when no code is pushed.

The summary contains counts and status only. It deliberately excludes vulnerability descriptions and candidate secret text so routine agent reads cannot echo sensitive report content.

Ruff, dependency audit, SAST and secret detection declare `needs: []`, so they run at pipeline start alongside the suite instead of waiting for (or being skipped by) it. `ci_summary` uses `needs` on every report producer, so it starts only after those jobs finish. `ci_summary` runs even when an upstream job fails. Security-template `needs` are optional only so GitLab can construct the summary job when a template omits a scanner; the missing report still fails closed. Missing, malformed, truncated, inconsistent, or schema-invalid artifacts produce a sanitized `INCOMPLETE`; this includes malformed required or optional scanner metadata, warning/fatal analyzer messages, duplicate security IDs, inconsistent JUnit testcase outcomes, unresolved dependency metadata, and invalid vulnerability records. Informational analyzer messages are accepted only in GitLab's documented shape. Test, quality, dependency, new SAST, or secret findings produce `FAIL`; a complete clean result produces `PASS`. Summary counters are capped at 10,000,000 to keep hostile numeric input bounded. The parser currently accepts GitLab security-report schema major version 15 and fails closed when GitLab introduces an unsupported schema.

## SAST baseline

`ci/sast-baseline.json` records findings independently reviewed at the commit named in that file. The summary compares current GitLab SAST IDs with the accepted IDs:

- accepted findings are counted but do not repeatedly consume review effort;
- a new, identity-changed, or severity-changed finding is reported as new and fails `ci_summary`;
- removed findings disappear naturally;
- baseline changes require an exact report review and normal merge-request approval.

Baseline schema version, source commit, unique IDs, severities, and stored severity counts are validated before any accepted finding is applied. The summary job also pins the independently reviewed source commit through `--expected-baseline-source`; baseline updates must change both values in the same reviewed merge request.

A successful SAST analyzer job proves that scanning executed; it does not mean the report has zero findings. `ci_summary` enforces the accepted-versus-new distinction.

## What remains manual

CI cannot prove physical framebuffer parity, GPIO/device permissions, real brightness or colour, deployed SHA identity, service startup, state preservation, or rollback success. These remain release checks on the Raspberry Pi and LED panel.
