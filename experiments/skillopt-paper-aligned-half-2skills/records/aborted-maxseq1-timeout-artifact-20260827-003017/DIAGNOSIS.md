# Aborted Max-Seq-1 Attempt

- Started: `2026-08-26 23:49 EDT`
- Stopped: `2026-08-27 00:27 EDT`
- Formal-use status: excluded from Accuracy and ASR
- Work archive: `/work/tc442/skill-evolution-poison-runs/skillopt-paper-aligned-half-2skills-aborted-maxseq1-timeout-artifact-20260827-003017/`

## Cause

Each SearchQA trajectory submitted 24 concurrent requests while its Qwen3.5
endpoint admitted one sequence. The 960-second task timer started before vLLM
queue service, so queue delay became a task timeout. This made the partial R0
condition-dependent and invalid for comparison.

## Persisted Evidence

| Condition | Rows | Task timeouts | Results SHA256 |
|---|---:|---:|---|
| clean | 32 | 17 | `434773f4f0aa5432ff879ccb9ab525baf2f66e6571bae8fb036821abdd607041` |
| db_delete | 97 | 0 | `6fe2899be5911b23ae7c78a0f055b600001aab46b1a86fea6311f6d49f10ed3e` |
| file_delete | 42 | 9 | `36d99aba51a1520150a725c9cc5f600a8f9e90d9f9b1a59d9b71734921b90bfb` |
| mock_api | 85 | 0 | `8c27aa49e60ee0e15516c5fcb6b1eb747f3cca4033fd5135c4d8b6373bf73b6c` |

All 256 JSONL rows were parseable and had unique task IDs. Mechanical resume
was possible, but rejected because it would retain false failures and mix two
serving schedules inside one R0.

## Replacement

The clean restart uses `target max_num_seqs=4`, `env.workers=4`, and one
analyst worker per trajectory against each two-slot shared optimizer. Qwen3.8
thinking stays enabled and its configured `reasoning_effort=medium` is now
fixed in the serving template.
