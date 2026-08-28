# Final Test Acceleration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use spml:ml-subagent-dev to implement this plan task-by-task.

**Goal:** Reduce the active experiment's remaining wall time without changing any checkpoint, test item, model weight, decoding parameter, metric, or canonical artifact contract.

**Experiment directory:** `experiments/skillopt-paper-aligned-half-2skills/`

**Hypothesis:** Precomputing the not-yet-started `file_delete/Rbest` test on three identical Qwen3.5 endpoints while the canonical trainer finishes `file_delete/R0` will replace two serial 700-item evaluations with parallel work and preserve exactly the same evaluation estimand.

**Validation scope:** Static shard/merge tests, frozen checkpoint and test-ID hashes, live endpoint identity checks, one-item scratch probe, exact 700-ID merged coverage, and native trainer consumption followed by the existing completion-receipt validator.

**Evaluation design:** The active trainer remains authoritative. Acceleration workers may write only isolated shard directories for a checkpoint whose canonical result directory does not yet exist. A coordinator performs a fail-closed atomic merge before the trainer reaches that checkpoint. Any incomplete acceleration artifact is ignored and the native trainer remains able to run normally.

**Architecture:** Reuse the official SearchQA adapter and Qwen chat backend in independent evaluator processes. GPU2 reuses the frozen target endpoint on port 19352; GPUs8 and 9 host two extra Qwen3.5 endpoints with byte-identical weights and serving parameters. Three disjoint ID shards are evaluated separately, verified, and atomically materialized as `file_delete/test_eval`.

---

## Shared Scaffold

### Existing infra

- Official SkillOpt source: `/work/tc442/skill-evolution-poison-data/skillopt-official/source/SkillOpt-9639719/`.
- Frozen SearchQA split: `/work/tc442/skill-evolution-poison-data/skillopt-official/materialized/skillopt_paper_aligned_half_2skills/searchqa/`.
- Active run root: `/work/tc442/skill-evolution-poison-runs/skillopt-paper-aligned-half-2skills/`.
- Native SearchQA rollout is resume-aware by task ID.
- Existing artifact validation remains authoritative.

### Needs setup

- `scripts/run_parallel_test_shard.py`: exact official-adapter evaluator and fail-closed shard merger.
- `tests/test_parallel_test_shard.py`: deterministic shard and merge contract tests.
- Run-root `acceleration/`: isolated logs, shard results, endpoint receipts, and merge provenance.
- Extra target endpoints on ports 19355 and 19356 using physical GPUs8 and 9.

## Subtask 1: Sharded Checkpoint Evaluator

**Role:** Evaluate disjoint frozen test subsets without touching a canonical result directory.

**Implementation:** Add `scripts/run_parallel_test_shard.py`. Load the existing flattened runtime config, bind an explicit target endpoint, instantiate the official SearchQA adapter, rebuild the frozen 700-item `valid_unseen` batch, select `items[shard_index::shard_count]`, verify checkpoint and split hashes, and write only an isolated shard directory plus a manifest.

**Unit Tests:** Verify shard disjointness and full coverage, checkpoint selection, manifest binding, refusal of invalid shard indices, and refusal to target a canonical directory in evaluate mode.

**Expected Conclusion:** The evaluator produces official-schema results for a deterministic subset without sharing a writer with the active trainer.

## Subtask 2: Atomic Shard Merger

**Role:** Convert complete isolated shards into one canonical resume artifact.

**Implementation:** In the same script, add a merge mode that validates identical experiment/run/checkpoint/split bindings, exact disjoint coverage of all 700 IDs, one result per ID, and matching model/decoding contracts. Copy optional prediction evidence into a temporary sibling directory, write results and acceleration provenance, and atomically rename only if the canonical destination remains absent.

**Unit Tests:** Reject missing IDs, duplicate IDs, mismatched checkpoint hashes, mismatched split hashes, incomplete results, and an existing canonical destination; accept a complete synthetic partition.

**Expected Conclusion:** A complete merge is all-or-nothing and native SkillOpt can resume from it.

## Subtask 3: Accelerated Final Evaluation [INTEGRATION]

**Hypothesis:** Parallel precomputation reduces the `file_delete` critical path while leaving final Accuracy and ASR definitions unchanged.

**Components consumed:** Official SearchQA adapter, Subtask 1 evaluator, Subtask 2 merger, existing Qwen3.5 target serving contract, and existing artifact validator.

**Implementation:** Launch identical target endpoints on GPUs8/9; qualify ports 19355/19356; run three `file_delete/Rbest` shards on ports 19352/19355/19356; merge them before the native trainer finishes R0; allow the trainer to read the 700 existing results, write its native summary, reuse identical Rbest for Rfinal, and seal its normal completion receipt. Do not stop optimizer endpoints or modify active trainer processes.

**Integration Tests:** Dry-run bindings, one-item scratch live probe, three-shard 700-ID coverage, canonical no-writer check, native summary generation, and existing completion receipt validation.

**Validation Pyramid:** L0 static contract tests plus L1 live one-item evaluation and final native artifact validation.

**Evaluation contract:** Dataset, checkpoint bytes, temperature, thinking mode, output cap, seed, task timeout, scorer, and 700-item denominator are unchanged. Physical GPU and endpoint port are execution topology only and are recorded. Failure leaves canonical output untouched and the native trainer proceeds normally.

**Expected Conclusion:** `file_delete/test_eval` contains exactly 700 validated rows consumed by the original trainer, with no duplicate IDs or concurrent canonical writer; remaining wall time becomes approximately `max(R0, sharded Rbest)` rather than `R0 + Rbest`.

