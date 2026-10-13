# Verification

## Deterministic gate (binary reward)
`test.sh` first freezes `packages/seroval/src`, `/app/output` and the public-run
evidence log into a root-owned snapshot, then runs the pytest prepass
(`deterministic/check.py --prepass` → `pytest_checks.py`) in a suite RewardKit
never discovers. RewardKit scores only the precomputed rows in a separate
minimal suite (`check.py`, `verifier.json`, `process/check.py`).

16 weighted rows (total weight 75):

| Rows | Weight | Purpose |
|---|---|---|
| `test_explicit_public_group[G1..G3]` | 5 | Sealed public probe groups; overlapping security + compatibility checks |
| `test_explicit_public_group[G4]` | 4 | Sealed public probe group; overlapping security + compatibility checks |
| `test_implicit_upstream_behavior[*]` (4) | 4 | Upstream suites and package build in a chrooted replay |
| `test_private_behavior[*]` security (5) | 6 | Holdout length-cap and non-buffer backing-source cases |
| `test_private_behavior[supported_*]` (2) | 4 | Supported typed-array / DataView round trips |
| `test_required_artifacts` | 2 | `plan.md`/`findings.json` contract + evidence that the reported reproducer status/score was actually observed on the submitted source digest |

Each row carries `failure_category` (`behavioral_failure`, `build_failure`,
`artifact_contract`, `validation_execution_error`, `submission_scope_or_replay_error`,
`setup_or_execution_error`). Probe/replay timeouts and crashes are `error`, not
behavioral failures.

`binary_reward = 1` iff every row passes and `process/check.py` confirms the
plan headings.

## Semantic judge (continuous reward only)
Runs only after a successful deterministic pass, with `ANTHROPIC_API_KEY` and a
real agent trajectory. Dimensions: `process`, `output_quality`, `validity`,
`task_quality` over `judge_inputs/` (instruction, trajectory + scope reading aid,
frozen artifacts, staged production source, deterministic rows, public probe
output, verifier-only `case_semantics.md`).

`continuous_reward = 0.55·deterministic + 0.20·min(plan gate, process judge) + 0.25·output_quality`
(output_quality omitted from the denominator when the judge did not run).

## Oracle evidence
Public probe: 18 executions over 10 distinct commands in four overlapping groups.
Measured in a local image emulation: no-op 8/18 public; FIX-1 only 16/18 public
(fails the non-buffer backing-source check in G2 and G4) and 14/16 verifier rows;
FIX-1 + FIX-2 18/18 public, 16/16 rows, reward 1.0.

## Fallback
If staging, freezing or RewardKit fails, observed prepass rows are preserved,
`verifier_error.json` is written, and the binary reward is 0.

## v12 public probe layout

Four mixed groups run four commands each. Eight unique checks (security-boundary and compatibility) are distributed twice across the groups; the agent receives only aggregate group scores. The probe does not emit diagnostic context or disclose the individual command mapping. Private verifier-owned cases are separate and additionally exercise non-buffer DataView backing sources.
