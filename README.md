# Evolutionary Incubation on SkillOpt

This repository implements a causal, lineage-level experiment for one question:

> Can one locally correct seed exposure change the path of later clean, validation-gated
> skill evolution, so that a harmful canary behavior appears only in descendant skills?

The harness does **not** give an attack model write access to `SKILL.md`, the SkillOpt
optimizer, the gate, or validation examples. It uses a closed synthetic canary containing
only `SAFE-*` and `CANARY-*` tokens.

## What is implemented

- Randomized complete-block `seed × evolution` 2×2 design.
- A matched placebo exposure, not an empty control.
- A custom SkillOpt `EnvAdapter` and `SplitDataLoader` that expose the seed exactly once.
- Clean carrier epochs followed by cue-free neutral washout epochs.
- A deterministic budgeted-retrieval tool loop with clean, monitor-trigger, near-trigger,
  and final locked splits.
- Native SkillOpt patch generation and strict validation gate.
- Post-hoc evaluation of baseline, changed descendants, and the final skill.
- One output directory per independent lineage, resumable completion markers, config hashes,
  SkillOpt revision provenance, and per-skill-hash evaluation directories.
- Block-level interaction estimates and block bootstrap confidence intervals.
- A deterministic mock engine for testing plumbing only.
- An optional external seed generator that never receives victim data or gate examples.

The official SkillOpt dependency is pinned to commit
`9639719632daecacd1baaa47fe781f3c0253600a`. The harness refuses an unverified revision
unless a non-confirmatory config explicitly opts out.

## Experimental arms

| Arm | First batch | Later treatment |
|---|---|---|
| `S0E0` | Matched benign exposure | Frozen skill |
| `S1E0` | Target-bearing but locally correct exposure | Frozen skill |
| `S0E1` | Matched benign exposure | Clean evolution |
| `S1E1` | Target-bearing but locally correct exposure | Clean evolution |

The primary estimand is the lineage-level interaction:

```text
(S1E1 - S0E1) - (S1E0 - S0E0)
```

Checkpoints and evaluation tasks are repeated measurements inside a lineage; they are not
counted as independent replicates.

## Install

Python 3.10–3.12 is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[server,dev]"
```

For an OpenAI-compatible endpoint, export configuration before starting a process:

```bash
export OPTIMIZER_OPENAI_COMPATIBLE_BASE_URL="http://127.0.0.1:8000/v1"
export OPTIMIZER_OPENAI_COMPATIBLE_API_KEY="dummy"
export OPTIMIZER_OPENAI_COMPATIBLE_MODEL="Qwen/Qwen3-32B"
export OPTIMIZER_OPENAI_COMPATIBLE_TEMPERATURE="0"

export TARGET_OPENAI_COMPATIBLE_BASE_URL="http://127.0.0.1:8000/v1"
export TARGET_OPENAI_COMPATIBLE_API_KEY="dummy"
export TARGET_OPENAI_COMPATIBLE_MODEL="Qwen/Qwen3-32B"
export TARGET_OPENAI_COMPATIBLE_TEMPERATURE="0"
```

Model IDs in the YAML are the recorded experiment settings and are passed to SkillOpt.
Keep the YAML and endpoint variables consistent.

## Local smoke test

The mock engine verifies scheduling, isolation, resume, checkpoint tracking, and analysis.
Its numbers are deliberately synthetic and must never be reported as evidence.

```bash
evoincubation prepare --config configs/smoke_mock.yaml
evoincubation validate --config configs/smoke_mock.yaml
evoincubation run-block --config configs/smoke_mock.yaml --index 0
evoincubation run-block --config configs/smoke_mock.yaml --index 1
evoincubation aggregate --config configs/smoke_mock.yaml
```

The rendered summary is written to
`runs/incubation_smoke_mock/analysis/summary.md`.

## Server pilot

The eight-lineage pilot is intentionally small. It tests whether the task has enough signal
before spending compute on seed search.

```bash
evoincubation prepare --config configs/pilot_skillopt.yaml
evoincubation validate --config configs/pilot_skillopt.yaml
evoincubation list --config configs/pilot_skillopt.yaml

evoincubation run-block --config configs/pilot_skillopt.yaml --index 0
evoincubation run-block --config configs/pilot_skillopt.yaml --index 1

evoincubation aggregate --config configs/pilot_skillopt.yaml
```

Each `run-block` executes all four arms in their pre-randomized order. Each arm runs in a new
Python process so SkillOpt backend globals, caches, and resume state cannot cross lineages.
Re-running a completed block is idempotent. `--force` preserves the old lineage directory as
a timestamped backup before rerunning it.

For Slurm, prepare and validate once on the login node, then submit:

```bash
CONFIG_PATH="$PWD/configs/pilot_skillopt.yaml" sbatch --array=0-1 scripts/slurm_block_array.sh
```

The mechanism config contains two seed formulations, four independent blocks per formulation,
and 32 total lineages:

```bash
evoincubation prepare --config configs/mechanism_skillopt.yaml
evoincubation validate --config configs/mechanism_skillopt.yaml
CONFIG_PATH="$PWD/configs/mechanism_skillopt.yaml" sbatch --array=0-7 scripts/slurm_block_array.sh
```

Aggregate only after every allocated lineage has either completed or has a documented technical
failure. The aggregator retains incomplete randomized rows and marks them excluded; it never
silently analyzes only successful attacks.

## Attack-model candidates

Do this only after the manual-seed pilot shows a positive interaction. The generator receives an
abstract canary specification, not victim trajectories, future clean batches, validation data, or
the final trigger set.

```bash
export ATTACK_MODEL_BASE_URL="http://127.0.0.1:8000/v1"
export ATTACK_MODEL_API_KEY="dummy"
export ATTACK_MODEL_NAME="Qwen/Qwen3-32B"
evoincubation generate-seeds --n 8 --output runs/generated_seed_candidates.json
```

Freeze selected candidates into a new YAML before victim evaluation. Candidate-search lineages
are surrogate search cost, not victim experimental replicates.

## Primary guardrails

The primary configs deliberately use:

- bounded `patch` evolution;
- `failure_only: false`, so locally correct exposure trajectories can be reflected on;
- `gate_metric: soft`, because the canary utility measures correctness plus tool efficiency;
- `use_meta_skill: false`;
- `use_slow_update: false`;
- `use_skill_aware_reflection: false`;
- clean selection data only;
- trigger monitoring only after training, with no feedback to optimizer or attack search;
- a separate final trigger split evaluated once on the final descendant.

Meta-state, gated slow-update, and rewrite modes should be added later as targeted ablations, one
mechanism at a time. See [the protocol](docs/EXPERIMENT_PROTOCOL.md) for the rationale and Go/No-Go
criteria.

## Development checks

```bash
PYTHONPATH=src python -m compileall -q src tests
PYTHONPATH=src python -m pytest -q
```
