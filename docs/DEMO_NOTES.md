# Demo notes

The best moments from real room runs, for the demo video and the README. Everything quoted here comes
from the ledger (`st evidence list`, `st hypothesis events`), not from memory.

## Run 1 — seed bug `pagination_off_by_one` (2026-11-06, room `tracedesk`)

Investigation `36a02742-7aa2-42a5-9165-0edd4c1c95c4`, pinned at `5d637281e5ca`. Agents on
OpenCode `opencode/deepseek-v4.1-flash-free`.

**The Investigator competed against itself.** It proposed three hypotheses, each with server-quoted citations:
1. `page_offset` returns `page * per_page` (`shelf/paginate.py:11-13`, `16-21`, plus the introducing commit `5d63728`).
2. `browse` passes a 1-based page into `paginate` unconverted (`shelf/catalog.py:6-13`).
3. The decoy: the same refactor rewrote `page_count` as `-(-total // per_page)` (`shelf/paginate.py:4-8`).

**The Skeptic challenge that mattered (the decoy).** It contradicted hypothesis 3 with arithmetic and a citation:
> "-(-total // per_page) is exact ceiling division: total=5, per_page=2 gives -(-5//2)=3, which matches test_catalog.py:24-25."

It also contradicted hypothesis 2 by citing `paginate`'s 1-based contract (`shelf/paginate.py:16-21`). It moved
hypothesis 1 to `supported` only after working through a concrete input:
> "Sorted titles beloved,carrie,dune,emma,ivanhoe: page 1 with per_page=2 returns items[2:4]=Dune,Emma"

Then it handed off to `@validator`.

**The Validator predicted before it ran anything** (2026-11-07, after OpenCode's free quota reset).
It read the evidence and requested one test, experiment `848bc67a-1202-4a30-a8b2-1f16567dee09`:
`python -m pytest -q tests/test_catalog.py::test_first_page_starts_with_first_title`. Its prediction:
> "if H is right, page_offset(1,2)=2 so page 1 slices indices 2:4 → ['Dune','Emma'], and the test fails vs expected ['Beloved','Carrie']. If H is wrong and page 1 starts at 0, it passes."

Then it stopped and asked the human: "Please run: st approve 848bc67a-…".

**The human approval.** `st approve` printed the exact command, and the human confirmed it.

**The sandbox run and the verdict.** The Validator ran it itself. The run completed with exit code 1 in
2.5 s, and the logs are in `artifacts/…/stdout.log`. It concluded `confirms` and moved the hypothesis to `verified`:
> "Key line: AssertionError: assert ['Dune', 'Emma'] == ['Beloved', 'Carrie'] from tests/test_catalog.py:17 — page 1 slices indices 2:4, exactly what return page * per_page at shelf/paginate.py:13 predicts."

**Final ledger.** 1 `verified` (the real cause, matching `expected.json`), 2 `contradicted` (including the
decoy), 10 evidence rows (7 code citations, 2 commit citations, 1 sandbox output), and 7 logged transitions
by three different agents.

## Rejections worth showing (from the dry run of the same flow)

- `st experiment run` before approval: `rejected (experiment_not_approved): Experiment is 'requested'; it runs only after a human approves it`
- `st approve` with an agent token: `rejected (403 role_not_allowed): Only the human approver can approve or reject experiments`
- `verified` by the Investigator after a confirming run: `rejected (409 role_not_allowed): Only the validator or a human can mark a hypothesis verified`
- A planted `conftest.py` in the workspace: `rejected (workspace_dirty): The workspace differs from the pinned commit: added conftest.py`
