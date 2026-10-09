# CI test audit — 2026-10-09

Exact base: 7ae96302ada12fecc6e36d5494bfb1862962da47; tree:
f5eb517289c331a93079274f1051d8a583712206. Tests/CI/docs only. Historical CI414
and predecessor frozen evidence unchanged. No runtime, production or publication.

## Findings and governing evidence

Audited all .gitlab-ci.yml stages/jobs and scripts/ci_summary.py, requirements.txt,
README, docs/CI.md, docs/FUTURE_DEVELOPMENT.md cross-cutting/P0/P1.3–1.7 and
Splash/Sleep requirements. Every discovered module was inventoried for AST
classes/methods/imports, version/path/fixture/discovery assumptions; relevant
assembly/projection/browser-binding/CI contracts were traced through their usages.

STALE: assembly exact total442 fails with actual443 after valid reflow addition.
Exact projection total3 has the same defect class. Replace both with independent
named required behaviors, owning namespaces and uniqueness, not a new total.
INSUFFICIENT: basename-only control discovery can accept wrong owners; complete
manifest/preflight now binds all preserved module/class/method IDs independently.
VALID: full discovery, real browser/media, Node22/native WebSocket, regular node
copy, venv alias, system-Python sanitizer libraries, interpreter-aware fake release
fixtures. Preserve unchanged. Explicit schemas/client contracts0–5, wire schema1,
PIN v1 cutoff, preview version0.3.66, migration/predecessor/R9 fixtures and exact
112-line driver extraction are contract/history, not suite counts; preserve them.
VALID: summary rejects zero tests/missing/malformed/error reports and validates
JUnit outcomes and reviewed SAST source/schema15. Parser/security baselines stay.
STALE docs: unconditional commit/push; now authorization-bound and local guidance.
New fast collection preflight catches missing/imported/duplicate cases before the
long suite. No new tiers, selection, skips, xfails, tolerances, sleeps, count floors,
allow_failure, security exclusions, retries, physical-gate or timeout changes.

Independent ci/required-tests.json preserves all1661 baseline discovered IDs
(inherited included; AST-declared1649), plus9 explicit collection/CI regressions in the review-fix generation.
It is not regenerated in CI. Valid additions pass; removal needs requirements
review. Counts below are audit observations, not gate thresholds. Actual complete
per-module inventory is collection.json; every module runs in full discovery.

## Entire pipeline

| Stage/job | Governing contract | Disposition/edit/evidence |
|---|---|---|
| workflow/includes/default | MR/branch/schedule; runner/image/retry | Valid, unchanged |
| validate/python_syntax | all Python AST + shipped helper presence | Valid, retained and locally executed |
| test/full_test_suite | complete xmlrunner/coverage/Chromium/media | Preflight/artifact only; full command/exclusions/timeout retained |
| quality/python_static_analysis | Ruff E9,F63,F7,F82 + report | Valid, locally executed; no cuts |
| quality/dependency_audit | requirements vulnerability report | Valid, unchanged; freshness not inferred locally |
| security/semgrep-sast | scanner + reviewed baseline | Valid, unchanged; external execution pending |
| security/secret_detection | scanner/report required by summary | Valid, unchanged; external execution pending |
| summary/ci_summary | complete artifacts, zero/error rejection | Valid; owning tests executed; parser unchanged |
| physical/physical_device_acceptance | protected-main exact identity/recovery | Unchanged; NOT EXECUTED (device forbidden) |

## Every discovered module/test family

Full behavior names are independently preserved in ci/required-tests.json.

| tests/module.py | Cases | Governing contract | Disposition |
|---|---:|---|---|
| `test_acceptance_artifacts` | 9 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_announcement_board` | 39 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_api_timeouts` | 2 | Bounded offline provider/preview behavior | Valid mocked-network contracts; retain |
| `test_ci_collection` | 8 | Discovery ownership and required behaviors | New positive/negative mutation proof |
| `test_ci_physical_matrix_smoke` | 133 | README/current configuration, pairing, setup, persistence/lifecycle | Valid explicit fixtures; retain |
| `test_ci_pipeline` | 15 | All CI jobs/report failures/isolation/interpreter/physical boundaries | Preflight ordering/artifact regression |
| `test_ci_summary` | 9 | All CI jobs/report failures/isolation/interpreter/physical boundaries | Valid; retain |
| `test_clock_lava` | 7 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_config_store` | 116 | README/current configuration, pairing, setup, persistence/lifecycle | Valid explicit fixtures; retain |
| `test_custom_text` | 36 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_device_preview` | 9 | README/current configuration, pairing, setup, persistence/lifecycle | Valid explicit fixtures; retain |
| `test_device_preview_publication` | 1 | README/current configuration, pairing, setup, persistence/lifecycle | Valid explicit fixtures; retain |
| `test_display_controller` | 96 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_edit_preview` | 56 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_feature_release_failure_receipt` | 5 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_feature_release_full_frames` | 5 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_font_catalog` | 3 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_frame_appearance` | 5 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_frame_capture` | 5 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_grouped_screen_integration` | 6 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_guarded_priority_1_3_release` | 40 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_hardware_lock` | 4 | README/current configuration, pairing, setup, persistence/lifecycle | Valid explicit fixtures; retain |
| `test_matrix_framework` | 88 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_media_modules` | 22 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_media_playlists` | 32 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_media_resolver` | 14 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_media_sanitizer` | 6 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_network_setup` | 5 | README/current configuration, pairing, setup, persistence/lifecycle | Valid explicit fixtures; retain |
| `test_openweather` | 21 | Bounded offline provider/preview behavior | Valid mocked-network contracts; retain |
| `test_operation_completion_transport` | 15 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_pairing` | 17 | README/current configuration, pairing, setup, persistence/lifecycle | Valid explicit fixtures; retain |
| `test_preview_provider` | 22 | Bounded offline provider/preview behavior | Valid mocked-network contracts; retain |
| `test_preview_request_contract` | 3 | Bounded offline provider/preview behavior | Valid mocked-network contracts; retain |
| `test_priority_1_3_acceptance` | 24 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_priority_1_3_product_behavior` | 3 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_priority_1_3_remote_bootstrap` | 16 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_priority_1_4_physical` | 5 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_priority_1_4_transaction` | 15 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_priority_1_5_transaction` | 21 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_priority_1_7_transaction` | 22 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_production_test_verifier` | 3 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_rectify_ci_contract` | 2 | All CI jobs/report failures/isolation/interpreter/physical boundaries | Valid; retain |
| `test_release_assembly` | 3 | Release-proof discovery; owning projection/WebApp behaviors | Replace stale totals; fresh loader |
| `test_release_bundle` | 39 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_release_caller_contract` | 6 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_release_failure_custody` | 12 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_release_gate_cli_and_build` | 13 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_release_helper_transition` | 13 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_release_pixel_paths` | 6 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_release_projection` | 3 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_release_proof_controls` | 5 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_reload_contention_regression` | 2 | README/current configuration, pairing, setup, persistence/lifecycle | Valid explicit fixtures; retain |
| `test_screen_media` | 52 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_scroll_timing` | 9 | README/P0/P1 64×32 parity, timing, drafts and screen lifecycle | Valid behavioral/migration fixtures; retain |
| `test_secret_store` | 7 | README/current configuration, pairing, setup, persistence/lifecycle | Valid explicit fixtures; retain |
| `test_setup_portal` | 8 | README/current configuration, pairing, setup, persistence/lifecycle | Valid explicit fixtures; retain |
| `test_slice1_identity_and_codec` | 17 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_slice2_active_claim_and_registry` | 42 | Exact release identity/preservation/rollback/evidence; offline fake operators | Valid historical/wire fixtures; retain |
| `test_testing_pin` | 6 | README/current configuration, pairing, setup, persistence/lifecycle | Valid explicit fixtures; retain |
| `test_weather` | 18 | Bounded offline provider/preview behavior | Valid mocked-network contracts; retain |
| `test_webapp` | 444 | Paired routes, unsaved drafts, Canvas/reflow, grouped screens; real browser | Valid; current added behavior preserved |

## Evidence and execution limits

Private durable command/result ledger:
/root/.local/state/hermes-worker-dashboard/matrix-development-20261009-ci-modernize-result.json

Baseline RED reproduced443!=442. New helper CLI initially absent (new CI-feature
RED, not runtime defect). Pipeline preflight regression was RED before integration.
Mutation suite proves valid addition passes; foreign imported TestCase, duplicate
alias, dropped required case/module, empty collection and collection error fail.
Full final-byte suite/JUnit/coverage is separately recorded, not inferred from
focused GREEN. Raw logs remain private with command exit/hash records.

Docker default AppArmor could not load in this LXC. Disposable container uses
known CI-runner apparmor=unconfined execution only; no host/profile-policy change.
A first disposable container without an init reaper left Chromium process-group
orphans and raised cleanup errors in four projection subtests. Unchanged tests
passed in the same image with Docker --init; this is environment alignment, not
a relaxed lifecycle assertion. Exact node:22-bookworm provisioning, short sticky TMPDIR, umask0022, configured
Chromium and dependency-bearing Python avoid hostPython3.14 assumptions. Offline
execution has no device credentials and uses loopback fixtures only. External
GitLab analyzers, dependency/security freshness and physical acceptance remain
unexecuted, not claimed GREEN. Deferred findings remain unchanged/open for tracker
publisher reconciliation; no extra actors/tracker writes in this assignment.

## Review-fix generation and verification disposition

CI-COLLECTION-01 fixed: JSON duplicate module keys previously overwrote earlier
required behaviors. A minimal rejecting `object_pairs_hook` now fails before
collection/publication. CLI RED proved exit0 incorrectly accepted an earlier
missing behavior; focused GREEN asserts nonzero, diagnostic, empty stdout and no
output artifact. Original1661 IDs remain; observed discovery is1670/61 modules.
The original required manifest is preserved privately with its verified hash.

CI-COLLECTION-N01 retained: ownership currently supports top-level `test_*.py`
modules, not nested unittest packages. Canonical/module Counter mismatch rejects
nested discoveries fail-closed. Current repository has no nested test modules or
`load_tests` hooks. Introduce reviewed qualified recursive ownership before
adopting nested packages; no recursive-engine expansion in this change.

Previous corrected full run (original generation):1669 tests,1 assertion failure,
0 errors,0 skips. `WebAppTests.test_pixel_active_stroke_is_committed_before_keyboard_save_and_reload`
reported after.dirty=True (also reloaded.dirty=True), expectedFalse. Preserve
that JUnit/log/coverage; isolated unchanged exact node subsequently passed.

Save lifecycle inspection found revision assignment, submitted snapshot capture,
dirty clearing and the Pixel saved hook in one synchronous continuation. A real
HTTP/browser response barrier held the Save response: revision stayed1 and draft
remained dirty; after release revision2 already had dirtyFlag=False, exact current
snapshot/baseline equality and expected pixels/history. This does NOT prove the
full-run failure harmless. No evidence justifies replacing the dirty assertion,
adding sleep or claiming premature async-persist sampling. A provider-timing
barrier probe timed out without creating a snapshot mismatch: rejected hypothesis,
not product RED. Diagnostic trace initially omitted the preparation50ms sleep;
the later Save-response control retained it. All probes remain private evidence.

Disposition: duplicate-key blocker fixed; Pixel full-run counterexample remains
BLOCKED_UNRESOLVED_PRODUCT_COUNTEREXAMPLE, cause not isolated. Test and runtime
unchanged rather than hiding it. Final successor full-suite execution explicitly
not performed in this phase; independent review and complete verification pending.
External GitLab dependency/security and physical/device gates NOT EXECUTED.
