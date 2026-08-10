# GitLab CI workflow

The project-owned pipeline is the source of truth for broad automated verification of each pushed commit. It does not deploy to the Raspberry Pi or replace physical 64×32 checks.

## Development handoff

Use this sequence to avoid repeating deterministic work in an agent session:

1. Inspect only the relevant source and tests.
2. Write and run one focused failing test.
3. Implement the change and rerun the focused test.
4. Review the exact diff, commit, and push it.
5. Let GitLab run repository-wide syntax, tests, browser/media coverage, Ruff, dependency auditing, SAST, and secret detection.
6. Read `ci-summary.txt` or `ci-summary.json` first. Open an individual job log or report only when that summary is `FAIL` or `INCOMPLETE`.
7. After merge, verify deployment identity, service health, rollback readiness, and the physical panel separately.

Do not rerun the complete suite, repository-wide lint, coverage, dependency audit, SAST, secret detection, or Chromium regression locally after GitLab has passed them against the same commit. Repeat a broad check only when CI used a different commit, an artifact is missing, CI does not exercise the required environment, or independent reproduction was explicitly requested.

## Pipeline evidence

The pipeline publishes the following reports for 14 days:

- JUnit XML and Cobertura coverage from `full_test_suite`;
- GitLab code-quality JSON from Ruff;
- `dependency-audit.json` from `pip-audit`;
- GitLab SAST and secret-detection reports;
- bounded `ci-summary.json` and `ci-summary.txt` acceptance summaries.

The GitLab schedule reruns the complete pipeline on `main` so dependency and security changes can be detected even when no code is pushed.

The summary contains counts and status only. It deliberately excludes vulnerability descriptions and candidate secret text so routine agent reads cannot echo sensitive report content.

`ci_summary` runs even when an upstream job fails. Security-template `needs` are optional only so GitLab can construct the summary job when a template omits a scanner; the missing report still fails closed. Missing, malformed, truncated, inconsistent, or schema-invalid artifacts produce a sanitized `INCOMPLETE`; this includes malformed required or optional scanner metadata, warning/fatal analyzer messages, duplicate security IDs, inconsistent JUnit testcase outcomes, unresolved dependency metadata, and invalid vulnerability records. Informational analyzer messages are accepted only in GitLab's documented shape. Test, quality, dependency, new SAST, or secret findings produce `FAIL`; a complete clean result produces `PASS`. Summary counters are capped at 10,000,000 to keep hostile numeric input bounded. The parser currently accepts GitLab security-report schema major version 15 and fails closed when GitLab introduces an unsupported schema.

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
