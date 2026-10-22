"""Run a way of doing the triage task.

    python run.py --way planned --set dev                         # one try on the dev issues
    FLAKY_WRITES=1 python run.py --way planned --set dev --runs 3                 # Part C's tests
    FLAKY_WRITES=1 READBACK_FAIL=1 python run.py --way planned --set dev --runs 3 # the third exit
    FLAKY_WRITES=1 python run.py --way planned --set eval --runs 2                # Part D, your harness
    FLAKY_WRITES=1 KEY_MODE=uuid VERIFY=off \
        python run.py --way planned --set eval --runs 2 --start-sweep 3           # Part D, the control
    python run.py --way planned --backend harness.scripted:ScriptedTriage --set dev   # free
    python run.py --way agent --backend never-stops --set dev     # the agent's bounds, free

The four switches (FLAKY_WRITES, READBACK_FAIL, KEY_MODE, VERIFY) are environment
variables, read by harness/switches.py and recorded in every run's run_start line.

Each run writes runs/<run_id>.jsonl. Success is not decided here:
eval/check_triage.py reads the tracker for each run_id and decides.

Running the same command again skips the runs on the evaluation issues that this
configuration (way, --critique, --notes, the four switches, backend) has already finished, so a
measurement can be split over days. Runs on the development issues always run anew.
If your vendor's daily quota runs out, the sweep stops; run the same command again
when it resets. To start a configuration over, on the same issues:

    FLAKY_WRITES=1 python run.py --discard --way planned --set eval                            # your harness's
    FLAKY_WRITES=1 KEY_MODE=uuid VERIFY=off python run.py --discard --way planned --set eval   # the control's

`make reset` archives the tracker's database and moves every earlier run of this
assignment on the evaluation issues to runs/discarded/, so Part D starts clean.
"""

import argparse
import sys

from harness import switches
from harness.runner import QUOTA_MESSAGE, discard, run_sweeps
from harness.tracker import Tracker


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--way", required=True, help="a module in ways/: direct, workflow, agent, hybrid, planned, notes")
    p.add_argument("--set", default="dev", choices=["dev", "eval"], help="which issues (default dev)")
    p.add_argument("--issues", help="comma-separated issue numbers; overrides --set")
    p.add_argument("--runs", type=int, default=1, help="sweeps over the issues (default 1)")
    p.add_argument("--start-sweep", type=int, default=1, help="number of the first sweep, to resume")
    p.add_argument("--backend", help="default: the provider in settings.yaml; never-stops, or a harness.scripted: model, tests your code for free")
    p.add_argument("--jobs", type=int, help="runs at once (default from settings.yaml)")
    p.add_argument("--critique", default="off", choices=["off", "self", "external"],
                   help="Part D's second pass before posting (default off); recorded in run_start")
    p.add_argument("--notes", default="off", choices=["off", "on"],
                   help="Part G: show the progress file to the model (default off); recorded in run_start")
    p.add_argument("--over-budget", action="store_true", help="ignore the assignment's token ceiling (ask first)")
    p.add_argument("--discard", action="store_true",
                   help="move this configuration's runs on these issues (the switches set now pick which arm), "
                        "and their rows in the tracker, to runs/discarded/; their tokens stay in the ledger")
    a = p.parse_args(argv)

    issues = ([int(x) for x in a.issues.split(",")] if a.issues
              else Tracker(flaky=False, readback_fail=False).issue_numbers(a.set))
    if a.discard:
        discard(a.way, issues, critique=a.critique, notes=a.notes, switches=switches.current())
        return 0
    sweeps = range(a.start_sweep, a.start_sweep + a.runs)
    rows = run_sweeps(a.way, issues, sweeps, a.backend, jobs=a.jobs, over_budget=a.over_budget,
                      critique=a.critique, notes=a.notes)
    tokens = sum(r["total_tokens"] for r in rows)
    cost = sum(r["cost_usd"] for r in rows)
    errors = sum(1 for r in rows if r["error"] and not (r["quota"] or r["rate_limited"]))
    print(f"\n{len(rows)} runs, {tokens:,} tokens, ${cost:.4f}, {errors} with errors")
    limited = sum(1 for r in rows if r["rate_limited"])
    if limited:
        print(f"{limited} runs stopped by rate limits are not finished: run the same command again to redo them"
              " (add --jobs 1 on a free tier)")
    if any(r["quota"] for r in rows):
        print(QUOTA_MESSAGE)
        return 0
    return 1 if errors == len(rows) and rows else 0


if __name__ == "__main__":
    sys.exit(main())
