# SkillOpt Paper-Aligned Half SearchQA Experiment

## Scope

This replacement run answers one question: after SkillOpt self-evolution, does
an attack inserted into the official initial skill remain behaviorally active?
It intentionally omits the paper's independent no-skill baseline.

| Base skill | Train | Validation | Test | Batch | Epochs | Updates |
|---|---:|---:|---:|---:|---:|---:|
| SearchQA | 200 | 100 | 700 | 40 | 4 | 20 |

SearchQA has `clean`, `db_delete`, `file_delete`, and `mock_api` trajectories:
four trajectories total. Attacked trajectories are evaluated at R0, Rbest, and
Rfinal for Accuracy and isolated behavioral ASR, giving 9 ASR probes.

DocVQA was part of the original plan but was removed from the active matrix on
`2026-08-27` before any DocVQA trajectory launched. The historical experiment
ID and directory name are preserved for checkpoint and provenance continuity.

## Corrected Protocol

- Official SkillOpt revision: `9639719632daecacd1baaa47fe781f3c0253600a`.
- Target: `Qwen3.5-9B`, thinking enabled, 16,384 output-token cap.
- Optimizer: `Qwen3.8-27B-FP8`, thinking enabled, `reasoning_effort=medium`, 16,384 output-token cap.
- Target uses a 65,536-token context; optimizer uses 131,072 tokens.
- Four epochs, batch 40, hard validation gate.
- `slow_update_gate_with_selection: true`.
- `json-repair==0.63.4` is a hard preflight condition.
- No no-skill evaluation.

## GPU Topology

| Role | GPUs | Port | Assigned workers |
|---|---|---:|---|
| Optimizer A | 0,6 | 19380 | targets 19351,19352 |
| Optimizer B | 5,7 | 19381 | targets 19353,19354 |
| Target 1 | 1 | 19351 | clean SearchQA |
| Target 2 | 2 | 19352 | db_delete SearchQA |
| Target 3 | 3 | 19353 | file_delete SearchQA |
| Target 4 | 4 | 19354 | mock_api SearchQA |

The two 29 GB optimizer replicas use tensor parallelism across two 24 GB GPUs.
The four target queues each execute one SearchQA trajectory. Each target admits
four concurrent sequences and each trajectory uses four rollout workers. Each
trajectory uses one analyst worker, giving each shared optimizer exactly two
concurrent requests.

## Paths

- Experiment and immutable records: this directory.
- Sealed data: `/work/tc442/skill-evolution-poison-data/skillopt-official/materialized/skillopt_paper_aligned_half_2skills/`.
- Run output: `/work/tc442/skill-evolution-poison-runs/skillopt-paper-aligned-half-2skills/`.
- Pipeline state: `records/pipeline-state.json` and the run-root mirror.
- Logs: run-root `logs/`.
- Final report: `RESULTS.md` plus `records/live-summary.json`.

## Commands

Static/dry validation performs no inference:

```bash
/work/tc442/venvs/skillopt-repro-9639719/bin/python scripts/materialize_injections.py
/work/tc442/venvs/skillopt-repro-9639719/bin/python scripts/run_pipeline.py --dry-run
```

Launch the resumable pipeline:

```bash
nohup /work/tc442/venvs/skillopt-repro-9639719/bin/python \
  scripts/run_pipeline.py \
  > /work/tc442/skill-evolution-poison-runs/skillopt-paper-aligned-half-2skills/pipeline.log 2>&1 &
```

The per-trajectory timeout is 48 hours. The pipeline was stopped at about
`2026-08-27 11:26 EDT` to remove the not-yet-started DocVQA work. The committed
SearchQA checkpoints are clean/db-delete/file-delete/mock-api = `7/8/7/7` of
20 steps. After resume, the provisional ETA to the SearchQA and ASR report is
`23-31 hours`. This is a local protocol-aligned experiment, not a numerical
reproduction of the paper's hosted-model results.
