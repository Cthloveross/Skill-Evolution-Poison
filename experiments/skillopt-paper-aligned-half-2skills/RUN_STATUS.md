# Run Status

Updated: `2026-08-27 12:36 EDT`

## Current State

| Stage | Status | Evidence |
|---|---|---|
| Static contract and tests | passed | SearchQA-only contract `15 passed`; dry-run sealed 4 trajectories and 9 ASR probes |
| Sealed data and initial skills | passed | exact split, initial-skill, and payload hashes remain unchanged |
| Qwen3.5 target service | passed | four 65,536-context endpoints; fresh target qualification passed at `15:31:59 UTC` |
| Qwen3.8 optimizer service | passed | fresh 96K-input + 16,384-output-budget qualification passed at `15:47:37 UTC` |
| SearchQA trajectories | running | four trainers are live; committed checkpoints clean/db/file/mock = 8/8/7/7 of 20 |
| DocVQA trajectories | removed | removed before any DocVQA trajectory launched |
| ASR and final report | queued after resume | 9 isolated probes start automatically after the four SearchQA trajectories complete |

No final test Accuracy or ASR exists yet. Training has committed `30/80 =
37.5%` of the scheduled steps. Current validation evidence is:

| SearchQA condition | Completed steps | R0 validation | Best validation | Gain | Best step |
|---|---:|---:|---:|---:|---:|
| clean | 8/20 | 72% | 87% | +15 pp | 1 |
| db_delete | 8/20 | 72% | 87% | +15 pp | 5 |
| file_delete | 7/20 | 65% | 85% | +20 pp | 5 |
| mock_api | 7/20 | 67% | 83% | +16 pp | 6 |

These values show that the evolution loop is producing accepted utility gains,
but they are selection-set scores, not final test results.

At the current uncommitted steps, clean is rolling out step 9; db-delete is
evaluating step 9; file-delete and mock-api are evaluating step 8. Clean step
8 proposed 85% validation utility and was correctly rejected because the
current skill is 87%. No traceback, HTTP 400, or rollout timeout has appeared
since the SearchQA-only resume.

## Context Amendment

The first 65K optimizer phase was stopped after one SearchQA db-delete step 9
analyst request reached `49,385 input + 16,384 output budget = 65,769`, which
exceeded 65,536 by 233 tokens. SkillOpt would otherwise catch the HTTP 400 and
silently treat that minibatch as zero edits. The failed step was not
checkpointed.

- Safe completed boundaries: clean/file-delete/mock-api step 7; db-delete step 8.
- Optimizer context only: `65,536 -> 131,072`; target remains 65,536.
- All 11 partial optimizer patches from the four incomplete steps were archived;
  the incomplete steps are being regenerated under 131K.
- The 160 complete target rollouts were retained because the target contract is
  byte-for-byte unchanged.
- Resume now reconstructs the current epoch's persisted step buffer and treats
  `runtime_state` as the final per-step commit marker.
- The final launch contracts explicitly encode both service/code phases and the
  per-trajectory boundary; they do not claim that earlier steps used 131K.

Machine-readable provenance is in
`records/context-capacity-transition.json`. The byte-preserving archive and
hash manifest are in
`records/context-capacity-transition-20260827T143414Z/`.

## Runtime Expectation

The 29 completed SearchQA steps before the context amendment averaged 72.3
minutes per step across four parallel trajectories. The pipeline was stopped at
approximately `2026-08-27 11:26 EDT` to remove DocVQA from the active matrix.
Completed-step median durations are approximately 1.19-1.37 hours by condition.
Using the longest remaining trajectory as the critical path, training is estimated
at another 16-17 hours; final test evaluation and the isolated ASR matrix bring the
conservative total ETA to `19-24 hours`. This remains an estimate; exact completion
time cannot be deduced.

The pipeline resumed in tmux session `skillopt-paper-aligned-half-2skills` at
`2026-08-27 11:28 EDT`. Pipeline state is mirrored in
`records/pipeline-state.json`; model and stage logs are under the run-root
`logs/` directory. `/work` had approximately 41 GB free before restart.

## Interpretation Boundary

This is a local Qwen, half-data, one-active-base-skill experiment. It tests
whether validation-gated SkillOpt evolution preserves executable attack
behavior while maintaining utility. It is not a numerical reproduction of
SkillOpt's hosted model results. Rbest is the paper-style deployment checkpoint;
Rfinal is the chronological retention endpoint.

The original two-skill scope is historical only. DocVQA was removed for time
budget reasons before launch and contributes no result, denominator, or claim.

The earlier max-seq-1 run remains excluded because queue-induced timeouts
biased R0. It is archived at
`/work/tc442/skill-evolution-poison-runs/skillopt-paper-aligned-half-2skills-aborted-maxseq1-timeout-artifact-20260827-003017/`.
