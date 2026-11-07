# Eval results

Date: 2026-11-07 · TraceDesk commit `5efb144da669` · agents: Switch + OpenCode, opencode/deepseek-v4.1-flash-free (Investigator, Skeptic, Validator)

Each case is a seeded bug with a known answer (`eval/cases/<case>/expected.json`). One run per case,
driven through Switch by a human posting the problem and approving experiments. Scores are computed
from the ledger by `eval/run_eval.py`.

| Case | Root cause correct | False verified | Decoy rejected | Experiments | Approvals | Time to verdict | Introducing commit cited |
|---|---|---|---|---|---|---|---|
| pagination_off_by_one | yes | 0 | yes | 1 | 1 | 21 h 48 min | yes |
| mutable_default_shared_state | yes | 0 | yes | 1 | 1 | 5 min 28 s | yes |

Notes on individual runs:

- **pagination_off_by_one**: Investigator and Skeptic finished in about 3 minutes; the Validator then waited about 21 hours for OpenCode's free usage limit to reset, so the time to verdict is mostly that wait.
- **mutable_default_shared_state**: Uninterrupted run, including the human approval. The Investigator also proposed caller-list aliasing at the same lines; the Skeptic contradicted it because the failing call passes no list.

**Totals**: 2/2 root causes verified correctly, 0 false verifications, 2/2 decoys rejected, 2 experiments run, 2 approvals.

Scope: 2 small seeded cases, written by the builder, one run each. This shows that the
loop works end to end and that the guards held; it is not a measure of general debugging accuracy.

How to read it: *false verified* counts hypotheses the server let reach `verified` that do not match
the known answer; the guards are meant to keep it at 0. *Time to verdict* runs from creating the
investigation to its first `verified` and includes the time a human took to approve (and, when it
happened, waiting out model rate limits), so it is not agent speed alone.
