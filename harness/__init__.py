"""RepoMind harness: the parts of the system the course provides.

You build on these; you do not edit them. The checkers assume they are unchanged.

    settings  course settings (model, ceilings)
    pricing   the dated price sheet
    formats   the formats a checker reads: comments, terminal reasons, paths, plans
    trace     one JSONL line per model call, per tool call, per plan and per event
    client    the metered model client, with a hard per-run ceiling
    backends  the real model provider
    scripted  scripted models for testing without spending tokens
    tracker   the mock issue tracker and remote, in SQLite; it can lie (Assignment 3)
    switches  FLAKY_WRITES, READBACK_FAIL, KEY_MODE and VERIFY, read from the environment
    roles     which of your tools plays each role (tools/roles.yaml)
    calls     the call record, one line per tool call (runs/<run_id>.calls.jsonl)
    runner    runs a way over issues, stamps run IDs, keeps the token ledger
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The assignment this copy of the repository is for. The runner records it on every
# run (run_start.lab), and each assignment's checker reads only its own runs.
LAB = "03"
