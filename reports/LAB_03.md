# Assignment 3 · A tool surface that tells the truth, and writes that happen once

Fill in each section. Every number here must match `reports/LAB_03.json`, and
every count of writes must come from the tracker's database, not from your traces.

## Part A · The five definitions

- Strict tools, optional parameters, nullable or union-typed parameters (from
  `python tools/audit_schemas.py`):
- Does your vendor have a strict mode? What did `make ping` show?
- Consolidation, in one line:

`search_repo`, before and after:

| Before | After |
|---|---|
| | |

## Part B · Errors

| Tool | Transient | Permanent | Ambiguous |
|---|---|---|---|
| | | | |

- Protocol errors: `end_run` or `generic_result`, and why:
- The exact text of your `read_file` near-miss error for `src/report.ts`:

## Part C · Writes that may already have happened

- The key formula:
- How a run whose file is `none` skips the PR:
- One call-record line for a write:

```json
```

## Part D · Measure

1. The surface price, with its arithmetic: the five definitions together, each alone, and the
   planner's tool list, the starter's and yours; then a mean harness-arm run, each call
   charged for what it carried, and the same calls with all five on every one:

2. The table, from the tracker:

| Measure | Your harness | Control |
|---|---|---|
| Triage comments per run (should be 1) | | |
| Open PRs from `repomind/issue-<n>` per run (1 when the file is not `none`, else 0) | | |
| Writes that timed out and had in fact committed | | |
| Runs that ended `escalated` | | |
| Triage success (your grounded check from Assignment 1) | | |

3. The four shapes of tool misuse: which your surface narrowed, with evidence for
   each; which is still standing; did you see it in your runs, and how would you
   know if you had not?

4. Every prediction in `reports/PREDICTIONS_03.md` that missed, and why:

## AI assistance

Which tool, what it wrote, and what you checked:
