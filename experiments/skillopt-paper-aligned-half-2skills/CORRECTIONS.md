# Protocol Corrections

## Why This Run Replaces the Prior Run

The previous `self-evolution-half-full-8gpu` run is preserved as a
non-thinking, force-final ablation. It is not a paper-protocol-aligned SkillOpt
run. This directory does not reuse its runtime outputs.

| Issue | Prior run | Corrected run |
|---|---|---|
| Optimizer | Qwen3.5-9B target-matched | Qwen3.8-27B-FP8 strong optimizer |
| Thinking | disabled for both roles | enabled for both roles |
| Optimizer output cap | 2,048 | 16,384 |
| Target/environment cap | 512 | 16,384 |
| Slow-update gate | force-accept (`false`) | validation-gated (`true`) |
| JSON recovery | missing | `json-repair==0.63.4` required |
| DocVQA scale | 100/53/187, batch 20 | 54/27/187, batch 40 |
| Reporting denominator | R0/Rbest/Rfinal | R0/Rbest/Rfinal; explicitly not Table 1 no-skill |
| Target scheduling | 24/16 rollout workers, one admitted sequence | four workers, four admitted sequences |
| Optimizer scheduling | 16 analyst workers per trajectory against two slots | one worker per trajectory, two trajectories per endpoint |
| Qwen3.8 reasoning effort | configured `medium`, silently served as default `xhigh` | `medium` fixed in optimizer serving template |
| Optimizer context | 65,536, causing a long attacked-skill analyst request to fail | 131,072, within Qwen3.8 native 262K support and the 190K-token local KV pool |

## Initial Skills

SkillOpt does not universally train from an empty skill. SearchQA's official
initial skill is a near-empty scaffold; DocVQA's official initial contains task
guidance. Clean R0 is byte-identical to the corresponding official
`initial.md`. An attacked R0 is exactly:

```text
reused DyMal payload bytes + official initial.md bytes
```

The relation and hashes are sealed in `records/initial-skill-index.json` and
the per-cell `injection_receipt.json` files. No no-skill pass is scheduled.

## Remaining Differences From Exact Paper Reproduction

This run aligns the algorithmic protocol but is not an exact reproduction of
the paper's hosted model setting or full data scale. It uses local Qwen models,
half cuts, four target replicas, and two optimizer replicas. Claims must be
limited to this local setting until additional models, full splits, and seeds
are run.

## Scheduling Correction

The initial live attempt used one admitted target sequence while each SearchQA
trajectory submitted 24 requests. Because rollout timeout accounting included
vLLM queue time, this produced false task timeouts and biased R0 by condition.
The attempt was stopped before any optimizer step, archived, and excluded from
all result tables. The replacement changes serving concurrency and rollout
worker count to four, and enforces the already configured Qwen3.8
`reasoning_effort=medium`. The scientific data and evolution protocol are
unchanged.

## Optimizer Context Amendment

At SearchQA db-delete step 9, one analyst request contained 49,385 input tokens
and reserved the configured 16,384 output tokens. Its 65,769-token envelope
exceeded the initial 65,536-token serving limit by 233 tokens. SkillOpt caught
the error as a zero-edit analyst result,
which would silently remove one failure minibatch from optimization. The
incomplete step was stopped before checkpointing. Optimizer context was raised
to 131,072 while target context and all model, prompt, thinking, output-budget,
data, gate, and seed settings remained unchanged. Qwen3.8 natively supports
262,144 tokens; the local endpoint exposes 190,581 KV-cache tokens, so 131,072
fits one maximum-length request. The replacement optimizer qualification uses
a 96K-token input with the full 16,384-token output budget before trajectories
are allowed to resume.

Resume now restores persisted `trajectory_digest.json` entries for completed
steps in the current epoch. This reconstructs the same step buffer that an
uninterrupted run would have used. Completed requests below 65K are retained;
only the incomplete step is resumed, and the missing analyst minibatch is
retried under the expanded admission limit.

## Active-Scope Reduction

On `2026-08-27`, DocVQA was removed from the active run before any DocVQA
trajectory launched. This is a wall-clock scope reduction, not a data-dependent
exclusion: no DocVQA result existed when the decision was made. The historical
experiment ID and directory remain unchanged so existing SearchQA checkpoints,
receipts, and provenance paths stay valid.

The effective experiment now contains four SearchQA trajectories and 9 ASR
probes. The pipeline was stopped at approximately `11:26 EDT` with committed
SearchQA checkpoints clean/db-delete/file-delete/mock-api = `7/8/7/7` of 20.
SearchQA model, data, seed, attack bytes, validation gate, and checkpoint rules
are unchanged. Conclusions must be limited to SearchQA; cross-base-skill
generalization cannot be deduced from this experiment.
