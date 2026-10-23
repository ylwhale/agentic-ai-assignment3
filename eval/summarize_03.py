"""Read-only, independent Assignment 3 evaluation summary.

Run from the project root: python -m eval.summarize_03
Uses actual run traces and independent tracker rows; does not change the database.
Schema token counts are the empirically measured constants in LAB_03.json.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sqlite3
import statistics
import yaml

from harness.formats import normalize_path, parse_triage_comment

ROOT = Path(__file__).resolve().parents[1]
WANTED = {
    "harness": {"flaky_writes": True, "readback_fail": False,
                "key_mode": "derived", "verify": "on"},
    "control": {"flaky_writes": True, "readback_fail": False,
                "key_mode": "uuid", "verify": "off"},
}
# Measurements of vendor-facing tool schemas, made with a constant baseline request.
STARTER_TOKENS = {"tracker_read_issue": 51, "code_search_repo": 114,
                  "code_read_file": 48, "tracker_post_comment": 70,
                  "pulls_open_pr": 103}
REDESIGNED_TOKENS = {"tracker_read_issue": 286, "code_search_repo": 578,
                     "code_read_file": 458, "tracker_post_comment": 531,
                     "pulls_open_pr": 683}
SUBMIT_PLAN_TOKENS = 327
STARTER_LIST_TOKENS = 116
REDESIGNED_LIST_TOKENS = 1555
ALL_FIVE_TOKENS = 2536


def main() -> None:
    answers = {int(n): v for n, v in yaml.safe_load(
        (ROOT / "fixtures/answers.yaml").read_text())["eval"].items()}
    config = yaml.safe_load((ROOT / "settings.yaml").read_text())
    model = config["model"]
    prices = yaml.safe_load((ROOT / "pricing.yaml").read_text())
    usd_per_million = float(prices["models"][model]["input"])
    db = sqlite3.connect(ROOT / "tracker/tracker.db")
    db.row_factory = sqlite3.Row
    arms: dict[str, list] = {name: [] for name in WANTED}

    for path in sorted((ROOT / "runs").glob("*.jsonl")):
        if path.name.startswith("_") or path.name.endswith(".calls.jsonl"):
            continue
        records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if not records or records[0].get("type") != "run_start":
            continue
        first = records[0]
        if (first.get("lab") != "03" or first.get("way") != "planned"
                or first.get("issue") not in answers or "scripted" in first.get("backend", "")
                or records[-1].get("type") != "run_end"):
            continue
        end = next((e for e in records if e["type"] == "end"), None)
        if end is None or end.get("terminal_reason") in ("quota", "error", "interrupted"):
            continue
        for arm, switches in WANTED.items():
            if first.get("switches") == switches:
                arms[arm].append((path, first, records, end))

    for name, runs in arms.items():
        model_calls = []
        actual_costs = []
        total_tokens = []
        comments_hist = Counter()
        outcomes = Counter()
        offered = Counter()
        rejects = Counter()
        off_plan = Counter()
        no_tool_response = 0
        timeout_after_commit = 0
        grounded = 0
        prs_ok = 0
        duplicate_pr_runs = 0
        before = after = all_five = 0

        for path, first, records, end in runs:
            rid, issue = first["run_id"], first["issue"]
            totals = records[-1]["totals"]
            calls = [e for e in records if e["type"] == "model_call"]
            model_calls.append(len(calls))
            actual_costs.append(float(totals["cost_usd"]))
            total_tokens.append(int(totals["total_tokens"]))
            outcomes[end["terminal_reason"]] += 1
            no_tool_response += sum(not e.get("tool_calls") for e in calls)
            off_plan.update(e.get("tool") for e in records if e["type"] == "off_plan")

            for e in calls:
                offered.update(e.get("tools") or [])
                # Planner alone gets the textual descriptions. Reviser sees its saved plan.
                for_before = for_after = 0
                for tool in e.get("tools") or []:
                    for_before += STARTER_TOKENS.get(tool, SUBMIT_PLAN_TOKENS)
                    for_after += REDESIGNED_TOKENS.get(tool, SUBMIT_PLAN_TOKENS)
                if e.get("purpose") == "plan":
                    for_before += STARTER_LIST_TOKENS
                    for_after += REDESIGNED_LIST_TOKENS
                before += for_before
                after += for_after
                all_five += ALL_FIVE_TOKENS

            calls_path = path.with_name(path.stem + ".calls.jsonl")
            if calls_path.exists():
                for line in calls_path.read_text().splitlines():
                    record = json.loads(line)
                    if record["outcome"] in ("rejected", "denied", "protocol"):
                        rejects[record["tool"]] += 1

            comment_rows = list(db.execute(
                "SELECT * FROM comments WHERE run_id = ? AND issue = ? ORDER BY id", (rid, issue)))
            pr_rows = list(db.execute("SELECT * FROM prs WHERE run_id = ?", (rid,)))
            comments_hist[len(comment_rows)] += 1
            label, file = parse_triage_comment(comment_rows[0]["body"]) if comment_rows else (None, None)
            if len(comment_rows) == 1 and label == answers[issue]["label"] and file == normalize_path(answers[issue]["file"]):
                grounded += 1
            right_prs = [p for p in pr_rows if p["state"] == "open" and p["branch"] == f"repomind/issue-{issue}"]
            want_prs = int(file not in (None, "none"))
            prs_ok += len(right_prs) == want_prs and len(pr_rows) == len(right_prs)
            duplicate_pr_runs += len(pr_rows) > 1
            timeout_after_commit += db.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id=? AND outcome='timeout_after_commit'",
                (rid,)).fetchone()[0]

        n = len(runs)
        print(f"\n{name.upper()} ({n} runs)")
        if not n:
            continue
        print(f"  Model calls: {sum(model_calls)} total, {statistics.mean(model_calls):.3f} per run, range {min(model_calls)}–{max(model_calls)}")
        print(f"  Model cost: ${sum(actual_costs):.6f} total, ${statistics.mean(actual_costs):.6f} mean, range ${min(actual_costs):.6f}–${max(actual_costs):.6f}")
        print(f"  Total tokens per run: mean {statistics.mean(total_tokens):.1f}, range {min(total_tokens)}–{max(total_tokens)}")
        print(f"  Comments per run (number of runs): {dict(sorted(comments_hist.items()))}")
        print(f"  Duplicate-comment runs: {sum(c * (k > 1) for k,c in comments_hist.items())}")
        print(f"  Correct conditional PR runs: {prs_ok}/{n}; duplicate PR runs: {duplicate_pr_runs}")
        print(f"  Committed write timeouts: {timeout_after_commit}; grounded triage passes: {grounded}/{n}")
        print(f"  Terminal outcomes: {dict(outcomes)}")
        print(f"  Tool offerings: {dict(offered)}")
        print(f"  Rejected/denied/protocol tool attempts: {dict(rejects)}; off-plan calls: {dict(off_plan)}; model calls with no tool call: {no_tool_response}")
        print(f"  Tool-surface counterfactuals at ${usd_per_million}/M uncached input tokens:")
        for title, tokens in (("starter", before), ("selective redesigned", after), ("all five every call", all_five)):
            print(f"    {title}: {tokens/n:.2f} tokens/run; ${tokens/n * usd_per_million / 1000000:.8f}/run")

    db.close()


if __name__ == "__main__":
    main()
