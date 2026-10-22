"""Run one way of doing the task over a set of issues, several times.

For every run the runner (not your code, not the model):
  - makes a fresh run_id and a trace file, runs/<run_id>.jsonl, and writes its
    first line (run_start, which records the four switches of harness/switches.py)
    and last line (run_end);
  - builds a metered client with the harness's hard per-run ceiling, and a tool
    registry bound to that run_id, so every write is stamped with it; the
    registry's context carries the run's call record (ctx.calls,
    runs/<run_id>.calls.jsonl), its harness log and the switches;
  - calls ways/<way>.py's run(ctx), with the second pass's mode (--critique) and
    the progress file's setting (--notes) on ctx, and records both in run_start;
  - appends the run's totals to runs/_ledger.jsonl, and refuses to start new
    runs once the assignment's token ceiling is spent;
  - on the evaluation issues (the measured runs), skips the (issue, sweep) pairs
    this configuration (way, --critique, --notes, the four switches, backend) has
    already finished, so running the same command again resumes it; runs on the
    development issues always run anew;
  - stops the whole sweep when the vendor's daily quota is spent: the run it
    interrupted ends "quota", which never counts as finished; a run whose calls
    are still refused for rate limits after every retry ends "rate_limited", which
    never counts as finished either, and the sweep goes on.

`python run.py --discard ...` moves a configuration's runs, their call records and
their rows in the tracker to runs/discarded/ (discard(), below); their tokens stay
in the ledger. `make reset` moves every earlier run of this assignment on the
evaluation issues there too (set_aside_evaluation_runs(), below), and archives the
tracker's database.

A run that raises is recorded with its error and the sweep carries on.
"""

from __future__ import annotations

import importlib
import json
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from harness import LAB, ROOT
from harness import switches as switch_board
from harness.backends import REAL_BACKENDS, DailyQuotaReached, RateLimited, make_backend
from harness.calls import CallRecord
from harness.client import BudgetExceeded, ModelClient, ToolCall
from harness.formats import CRITIQUE_MODES, NOTES_MODES, UNFINISHED
from harness.pricing import PriceSheet
from harness.settings import Settings, load_settings
from harness.trace import TraceWriter
from harness.trace import read_trace
from harness.tracker import Tracker
from tools import RunContext, ToolRegistry, default_tools

QUOTA_MESSAGE = "daily limit reached; run the same command again when it resets; finished runs are kept"
FREE_BACKENDS = {"never-stops"}
SCRIPTED_PREFIX = "harness.scripted:"   # the scripted models in harness/scripted.py
_ledger_lock = threading.Lock()


@dataclass
class WayContext:
    """What ways/<way>.py receives. Everything a run may touch is on here."""
    run_id: str
    issue_number: int
    client: ModelClient
    tools: ToolRegistry
    trace: TraceWriter
    tracker: Tracker
    settings: Settings
    root: Path
    critique: str = "off"         # Assignment 2, Part D: off | self | external (run.py --critique)
    notes: str = "off"            # Assignment 2, Part G: off | on (run.py --notes)
    runs_dir: Path | None = None  # where this run's trace is; root/runs unless a checker says otherwise

    @property
    def progress_path(self) -> Path:
        """Part G's progress file for this run: runs/<run_id>.progress.json."""
        return Path(self.runs_dir or self.root / "runs") / f"{self.run_id}.progress.json"

    def call_tool(self, step: int, name: str, tool_input: dict, plan_step=None):
        """Call a tool from your own code, not at the model's request, and trace it.

        For the direct call, the workflow and the planned way's executor, where your
        code decides which tool runs. Returns the ToolResult (.content, .is_error).
        `step` is the step of the most recent model call, or 0 before the first.
        `plan_step` is the id of the plan step this call runs for (Assignment 2's
        executor passes it on every call); it goes on the tool_call line, and to
        the registry, which derives a write's key from it (Assignment 3)."""
        call = ToolCall(id=f"code-{name}-{step}", name=name, input=tool_input)
        result = self.tools.call(name, tool_input, plan_step=plan_step)
        self.trace.tool_call(step, call, result, plan_step=plan_step)
        return result

    def prompt(self, name: str) -> str:
        """Read prompts/<name>. Keep every prompt you write in prompts/."""
        return (self.root / "prompts" / name).read_text()

    @property
    def repo_summary(self) -> str:
        """The fixed repository summary for the direct call. Do not edit the file."""
        return (self.root / "fixtures" / "repo_summary.md").read_text()


def is_free(backend_name: str) -> bool:
    """A scripted model: its tokens are estimates, and it costs nothing."""
    return backend_name in FREE_BACKENDS or backend_name.startswith(SCRIPTED_PREFIX)


class Ledger:
    def __init__(self, path: Path):
        self.path = path

    def spent_tokens(self) -> int:
        if not self.path.exists():
            return 0
        total = 0
        for line in self.path.read_text().splitlines():
            row = json.loads(line)
            if not is_free(row.get("backend") or ""):
                total += row.get("total_tokens", 0)
        return total

    def append(self, row: dict) -> None:
        with _ledger_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as fh:
                fh.write(json.dumps(row) + "\n")


def run_once(way: str, issue: int, sweep: int, backend_name: str, *, settings: Settings,
             prices: PriceSheet, tracker: Tracker, root: Path, runs_dir: Path,
             critique: str = "off", notes: str = "off", switches: dict | None = None) -> dict:
    run_id = f"{way}-{issue}-s{sweep:02d}-{uuid.uuid4().hex[:6]}"
    switches = dict(switches or switch_board.current())
    backend = make_backend(backend_name, settings)
    model = settings.model if backend_name in REAL_BACKENDS else getattr(backend, "name", backend_name)
    trace = TraceWriter(runs_dir / f"{run_id}.jsonl", run_id, {
        "lab": LAB, "way": way, "issue": issue, "sweep": sweep, "backend": backend_name,
        "model": model, "settings": settings.digest(), "price_sheet": prices.read_date,
        "critique": critique, "notes": notes, "switches": switches,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    client = ModelClient(backend, prices, session_ceiling=settings.session_token_ceiling)
    tools = default_tools(RunContext(run_id=run_id, tracker=tracker, repo_root=root / "fixtures" / "repo",
                                     calls=CallRecord(runs_dir / f"{run_id}.calls.jsonl", run_id, root),
                                     harness_log=runs_dir / f"{run_id}.harness.log", switches=switches))
    ctx = WayContext(run_id, issue, client, tools, trace, tracker, settings, root,
                     critique=critique, notes=notes, runs_dir=runs_dir)
    error = None
    try:
        importlib.import_module(f"ways.{way}").run(ctx)
    except DailyQuotaReached as exc:
        error = f"DailyQuotaReached: {exc}"
        if not trace.ended:
            trace.end("quota", detail="the vendor's daily quota stopped this run; run the command again to redo it")
    except RateLimited as exc:
        error = f"RateLimited: {exc}"
        if not trace.ended:
            trace.end("rate_limited", detail="the vendor still refused after every retry; run the command again to redo it")
    except BudgetExceeded as exc:
        error = f"BudgetExceeded: {exc}"
        if not trace.ended:
            trace.end("token_budget", detail="the harness ceiling stopped this run, not your loop")
    except NotImplementedError as exc:
        error = f"NotImplementedError: {exc}"
    except Exception as exc:  # the sweep goes on; the trace keeps the evidence
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}"
    totals = client.totals()
    trace.run_end(totals, error)
    return {"run_id": run_id, "way": way, "issue": issue, "sweep": sweep, "backend": backend_name,
            "critique": critique, "notes": notes, "total_tokens": totals["total_tokens"], "cost_usd": totals["cost_usd"],
            "error": error.splitlines()[0] if error else None,
            "quota": bool(error and error.startswith("DailyQuotaReached")),
            "rate_limited": bool(error and error.startswith("RateLimited")),
            "finished": time.strftime("%Y-%m-%dT%H:%M:%S")}


def run_header(path: Path) -> dict | None:
    """A trace's run_start line, read without the rest of the file."""
    try:
        with Path(path).open() as fh:
            first = json.loads(fh.readline() or "null")
    except (OSError, json.JSONDecodeError):
        return None
    return first if isinstance(first, dict) and first.get("type") == "run_start" else None


def same_configuration(start: dict, way: str, critique: str, notes: str, switches: dict | None = None) -> bool:
    """The same way, --critique, --notes and, when given, the same four switches."""
    recorded = {**switch_board.DEFAULTS, **(start.get("switches") or {})}
    return (start.get("lab") == LAB and start.get("way") == way
            and (start.get("critique") or "off") == critique and (start.get("notes") or "off") == notes
            and (switches is None or recorded == {**switch_board.DEFAULTS, **switches}))


def is_trace(path: Path) -> bool:
    """A run's trace, runs/<run_id>.jsonl: not the ledger, not a call record."""
    return not path.name.startswith("_") and not path.name.endswith(".calls.jsonl")


def finished_pairs(runs_dir: Path, way: str, critique: str, notes: str, backend_name: str,
                   switches: dict | None = None) -> set[tuple[int, int]]:
    """The (sweep, issue) pairs this configuration has finished on this backend: a run
    with a run_end line that neither the daily quota nor a rate limit stopped."""
    done = set()
    for path in sorted(Path(runs_dir).glob("*.jsonl")):
        start = run_header(path) if is_trace(path) else None
        if not start or start.get("backend") != backend_name:
            continue
        if not same_configuration(start, way, critique, notes, switches):
            continue
        records = read_trace(path)
        ends = [r.get("terminal_reason") for r in records if r.get("type") == "end"]
        if records and records[-1].get("type") == "run_end" and not set(ends) & set(UNFINISHED):
            done.add((start.get("sweep"), start.get("issue")))
    return done


def to_run(sweeps, issues, done: set, eval_issues: set) -> list[tuple[int, int]]:
    """The (sweep, issue) pairs to run: all of them, less those on an evaluation issue
    that this configuration has finished. A development run always runs anew."""
    return [(sweep, issue) for sweep in sweeps for issue in issues
            if not (issue in eval_issues and (sweep, issue) in done)]


def discard(way: str, issues: list[int], *, critique: str = "off", notes: str = "off", root: Path = ROOT,
            switches: dict | None = None, log=print) -> int:
    """Move this configuration's runs on these issues, with their call records, harness
    logs and progress files, to runs/discarded/, and their rows in the tracker to
    runs/discarded/tracker_rows.jsonl. Their tokens stay in the ledger."""
    runs_dir = root / "runs"
    dest = runs_dir / "discarded"
    moved = []
    for path in sorted(runs_dir.glob("*.jsonl")):
        start = run_header(path) if is_trace(path) else None
        if not start or start.get("issue") not in issues:
            continue
        if not same_configuration(start, way, critique, notes, switches):
            continue
        dest.mkdir(parents=True, exist_ok=True)
        path.rename(dest / path.name)
        for suffix in (".progress.json", ".calls.jsonl", ".harness.log"):
            extra = runs_dir / f"{start['run_id']}{suffix}"
            if extra.exists():
                extra.rename(dest / extra.name)
        moved.append(start["run_id"])
    rows = Tracker(root, flaky=False, readback_fail=False).remove_runs(set(moved))
    if rows:
        with (dest / "tracker_rows.jsonl").open("a") as fh:
            for table, items in rows.items():
                fh.writelines(json.dumps({"table": table, **item}) + "\n" for item in items)
    gone = sum(len(v) for k, v in rows.items() if k != "attempts")
    log(f"moved {len(moved)} runs and {gone} comments and pull requests to runs/discarded/; "
        "their tokens stay in the ledger")
    return len(moved)


def set_aside_evaluation_runs(root: Path = ROOT, log=print) -> int:
    """make reset's other half: move every run of this assignment on the evaluation
    issues, whatever its configuration or backend, with its call record, harness log
    and progress file, to runs/discarded/. Their rows in the tracker go with the
    archived database, and their tokens stay in the ledger. Without this, a run made
    before the reset would be resumed past (it is finished) and checked against an
    empty tracker."""
    runs_dir = root / "runs"
    dest = runs_dir / "discarded"
    issues = set(Tracker(root, flaky=False, readback_fail=False).issue_numbers("eval"))
    moved = 0
    for path in sorted(runs_dir.glob("*.jsonl")):
        start = run_header(path) if is_trace(path) else None
        if not start or start.get("lab") != LAB or start.get("issue") not in issues:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        path.rename(dest / path.name)
        for suffix in (".progress.json", ".calls.jsonl", ".harness.log"):
            extra = runs_dir / f"{start['run_id']}{suffix}"
            if extra.exists():
                extra.rename(dest / extra.name)
        moved += 1
    if moved:
        log(f"moved {moved} earlier runs on the evaluation issues to runs/discarded/; their tokens stay in the ledger")
    return moved


def run_sweeps(way: str, issues: list[int], sweeps: range, backend_name: str | None = None, *,
               jobs: int | None = None, over_budget: bool = False, root: Path = ROOT,
               critique: str = "off", notes: str = "off", log=print) -> list[dict]:
    if critique not in CRITIQUE_MODES or notes not in NOTES_MODES:
        raise SystemExit(f"--critique is one of {CRITIQUE_MODES}, --notes one of {NOTES_MODES}")
    switches = switch_board.current()
    on = [f"{k.upper()}={v if isinstance(v, str) else int(v)}" for k, v in switches.items()
          if v != switch_board.DEFAULTS[k]]
    if on:
        log("switches: " + " ".join(on))
    settings = load_settings(root / "settings.yaml")
    backend_name = backend_name or settings.provider
    if not backend_name or (backend_name in REAL_BACKENDS and not settings.model):
        raise SystemExit("settings.yaml: choose your provider and model first "
                         "(the handout, Getting started, step 2), then run `make ping`.")
    prices = PriceSheet(root / "pricing.yaml")
    tracker = Tracker(root)
    runs_dir = root / "runs"
    ledger = Ledger(runs_dir / "_ledger.jsonl")
    done = finished_pairs(runs_dir, way, critique, notes, backend_name, switches)
    plan = to_run(sweeps, issues, done, set(tracker.issue_numbers("eval")))
    skipped = len(sweeps) * len(issues) - len(plan)
    if skipped:
        log(f"skipping {skipped} runs on the evaluation issues that this configuration has already finished "
            "(development runs always run anew)")
    results: list[dict] = []
    stop = threading.Event()
    quota_said = threading.Event()

    def task(item):
        sweep, issue = item
        if stop.is_set():
            return None
        if not is_free(backend_name) and not over_budget:
            spent = ledger.spent_tokens()
            if spent >= settings.lab_token_ceiling:
                stop.set()
                log(f"lab token ceiling reached ({spent:,} >= {settings.lab_token_ceiling:,}); "
                    "no new runs. Ask before passing --over-budget.")
                return None
        row = run_once(way, issue, sweep, backend_name, settings=settings, prices=prices,
                       tracker=tracker, root=root, runs_dir=runs_dir, critique=critique, notes=notes,
                       switches=switches)
        ledger.append(row)
        flag = f"  ERROR {row['error']}" if row["error"] and not (row["quota"] or row["rate_limited"]) else ""
        log(f"{row['run_id']:<32} {row['total_tokens']:>9,} tok  ${row['cost_usd']:.4f}{flag}"
            + ("  stopped: daily quota" if row["quota"] else "")
            + ("  stopped: rate limited, not finished" if row["rate_limited"] else ""))
        if row["quota"]:
            stop.set()
            if not quota_said.is_set():
                quota_said.set()
                log(QUOTA_MESSAGE)
        return row

    with ThreadPoolExecutor(max_workers=max(1, jobs or settings.jobs)) as pool:
        for row in pool.map(task, plan):
            if row:
                results.append(row)
    return results
