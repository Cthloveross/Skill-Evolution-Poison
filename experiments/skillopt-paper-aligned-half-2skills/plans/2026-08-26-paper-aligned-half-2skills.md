# SkillOpt Paper-Aligned Half Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use spml:ml-subagent-dev to implement this plan task-by-task.

**Goal:** Run four corrected SearchQA SkillOpt trajectories and measure Accuracy plus attacked-checkpoint ASR without a no-skill pass.

**Experiment directory:** `experiments/skillopt-paper-aligned-half-2skills/`

**Hypothesis:** Validation-gated self-evolution can retain executable attack behavior inserted into an official initial skill while preserving benchmark utility.

**Validation scope:** Static contract checks, sealed data/initial provenance, role-specific 65K target and 131K optimizer qualification, R0/Rbest/Rfinal utility, and isolated ASR.

**Evaluation design:** Four epochs with checkpoint-bound evaluation; incomplete or invalid evidence is reported as unknown, never as attack failure.

**Architecture:** Two TP2 Qwen3.8 optimizer replicas with 131K context serve four single-GPU Qwen3.5 target queues with 65K context. Each target admits four concurrent sequences and each trajectory uses exactly four rollout workers, so HTTP queueing cannot consume the task timeout. Each queue runs one SearchQA trajectory, then target-only workers execute isolated ASR probes.

---

## Shared Scaffold

### Existing Infra

- Official SkillOpt: `/work/tc442/skill-evolution-poison-data/skillopt-official/source/SkillOpt-9639719/`.
- Canonical sealed cuts: `/work/tc442/skill-evolution-poison-data/skillopt-official/materialized/skillopt_paper_aligned_half_2skills/`.
- Reused DyMal payload and isolated verifier bytes: this experiment's `dymal-injections/` and `asr-runtime/`.

### Needs Setup

- [x] Freeze configs and run index.
- [x] Implement role-bound servers, routing, preflight, resume, and 48-hour timeouts.
- [x] Correct target scheduling to `max_num_seqs=4`, `env.workers=4` after the max-seq-1 run exposed queue-induced timeouts.
- [x] Pass live 65K target and 131K optimizer qualifications.
- [ ] Complete the four resumed SearchQA trajectories.
- [ ] Complete 9 ASR probes and final report.

## Subtask 1: Data And Initial Provenance

**Role:** Bind every run to exact split, official initial, and attack bytes.

**Implementation:** Verify the canonical materialization adapter/audit; seal four active SearchQA run-index rows and per-cell injection receipts.

**Unit Tests:** Counts, disjoint IDs, hashes, exact payload-plus-official relationship.

**Expected Conclusion:** Four immutable, reproducible SearchQA inputs pass static validation.

## Subtask 2: Dual-Role Model Serving

**Role:** Prevent optimizer/target routing or protocol drift.

**Implementation:** Launch two Qwen3.8 TP2 optimizers with 131K context and four Qwen3.5 targets with 65K context; keep thinking enabled and bind process receipts to exact GPUs, ports, models, and commands.

**Unit Tests:** Commands, identities, topology, thinking flags, token caps, and receipt equality.

**Expected Conclusion:** Six role-correct endpoint specifications pass without launching GPUs in unit tests.

## Subtask 3: Corrected SkillOpt Pipeline [INTEGRATION]

**Hypothesis:** Corrected validation-gated SkillOpt evolution retains measurable attack behavior.

**Components consumed:** Frozen configs, sealed run index, role-bound servers, official SkillOpt trainer, artifact validator, and isolated ASR runtime.

**Implementation:** Qualify one endpoint per role, run four fixed target queues, resume native trajectory state, test R0/Rbest/Rfinal, run 9 ASR probes, and build atomic summaries.

**Integration Tests:** Dry-run shape, target-optimizer route balance, 48-hour timeout, no no-skill stage, strict slow gate, and target-only ASR endpoints.

**Validation Pyramid:** L0 static tests followed by live endpoint qualification and the full half-scale run.

**Evaluation contract:** Rbest is the paper deployment checkpoint; Rfinal is an additional chronological retention endpoint. Missing evidence remains unknown.

**Expected Conclusion:** Four completion receipts and 9 terminal ASR receipts produce a complete results table; otherwise the report identifies exact missing cells.

## Revision: Target Scheduling

The first live R0 run was stopped at `2026-08-27 00:27 EDT`. SearchQA submitted 24 requests per trajectory to target servers that admitted only one sequence. Request timeout accounting began before vLLM queue service, so queue delay was misclassified as task failure: clean had 17 timeouts among 32 persisted rows and file-delete had 9 among 42. Those outputs are diagnostic only and are archived under `/work/tc442/skill-evolution-poison-runs/skillopt-paper-aligned-half-2skills-aborted-maxseq1-timeout-artifact-20260827-003017/`.

The corrected run starts from R0 with four target sequences and four rollout workers. Each Qwen3.8 endpoint serves two trajectories and admits two sequences, so each trajectory uses one analyst worker rather than placing up to 32 requests into a two-slot queue. Qwen3.8 optimizer thinking remains enabled, with its configured `reasoning_effort=medium` now fixed in the serving template instead of silently falling back to the model's `xhigh` default. Model weights, prompts, splits, seed, 16K output cap, epochs, batch size, validation gate, and checkpoint evaluation are unchanged. `max_num_batched_tokens` remains 2,048 to avoid increasing multimodal prefill memory. Expected target throughput improvement is approximately 2-3x; the end-to-end ETA remains provisional until one complete evolution step exercises both target and optimizer.

## Revision: Optimizer Context And Resume

At db-delete step 9, an optimizer request required 65,769 tokens including its
configured output budget, 233 above the original 65,536 service limit. The
incomplete step was stopped before checkpointing. Optimizer context was raised
to 131,072; the target and all scientific settings are unchanged. A live
96K-input request with the full 16,384 output budget must pass before resume.

Completed old-phase steps remain valid and their archived 65K launch contracts
are retained. Every partial optimizer patch in the interrupted steps is
regenerated under 131K, while complete target rollouts are retained. The v4
launch contract binds both phases, per-run resume boundaries, the transition
manifest, and exact before/after trainer hashes. Resume reconstructs the
epoch-local step buffer from `trajectory_digest.json` and fails closed on an
incomplete committed step.

## Revision: Active Scope Reduced To SearchQA

On `2026-08-27`, the active matrix was reduced from SearchQA plus DocVQA to
SearchQA only to fit the available wall-clock budget. DocVQA was removed before
any DocVQA trajectory launched, so no DocVQA output is included or interpreted
as experimental evidence. The historical experiment directory and experiment
ID are preserved to avoid breaking checkpoint and provenance paths.

The active matrix is four SearchQA trajectories (`clean`, `db_delete`,
`file_delete`, and `mock_api`) followed by 9 attacked-checkpoint ASR probes
(three attacks times R0/Rbest/Rfinal). The pipeline was stopped at approximately
`2026-08-27 11:26 EDT` for this scope revision. The committed SearchQA
boundaries are unchanged: clean 7, db-delete 8, file-delete 7, and mock-api 7
of 20 steps. After resume, the provisional remaining ETA is `23-31 hours`;
exact completion time cannot be deduced from the current step-time variance.
