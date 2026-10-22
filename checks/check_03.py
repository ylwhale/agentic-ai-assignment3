#!/usr/bin/env python3
"""Assignment 3 checker. Run it with `make check-03`.

It reads only the environment: your runs/ traces and call records, the tracker's
database, runs/_ping.jsonl, your tools/ definitions and reports/LAB_03.json. It
calls your registry and your planned way directly, with scripted models
(harness/scripted.py) and a tracker whose faults it chooses. It never asks a model
anything and never trusts a model's words.

Assignment 3 is scored out of 100. The checker awards the automated 82; staff read
the report for the other 18. It also prints integrity flags, which carry no points
and go to staff for review. Every format it reads is defined in checks/FORMATS.md,
section 11.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import sqlite3
import sys
import tempfile
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from harness import LAB  # noqa: E402
from harness import switches as switch_board  # noqa: E402
from harness.backends import REAL_BACKENDS  # noqa: E402
from harness.calls import CallRecord, read_calls  # noqa: E402
from harness.client import BudgetExceeded, ModelClient  # noqa: E402
from harness.formats import UNFINISHED, normalize_path, parse_triage_comment  # noqa: E402
from harness.pricing import PriceSheet  # noqa: E402
from harness.roles import ROLES, WRITES, roles  # noqa: E402
from harness.runner import WayContext, is_free, is_trace  # noqa: E402
from harness.scripted import UNKNOWN_TOOL, ScriptedTriage, UnknownToolCaller, role_call  # noqa: E402
from harness.settings import load_settings  # noqa: E402
from harness.trace import TraceWriter, read_trace  # noqa: E402
from harness.tracker import Tracker  # noqa: E402

ARMS = {"harness": {"flaky_writes": True, "readback_fail": False, "key_mode": "derived", "verify": "on"},
        "control": {"flaky_writes": True, "readback_fail": False, "key_mode": "uuid", "verify": "off"}}
RUNS_PER_ARM = 12                       # the six evaluation issues, twice each
WRITE_ISSUE, AGENT_ISSUE = 4192, 4191   # development issues: the scripted runs reveal nothing about evaluation
MISSING_PATH = "src/report.ts"
NEAR_MISSES = ("src/reports.ts", "src/Report.ts", "src/report.tsx")
NO_MATCH = "zqxwvkjh"
MALFORMED = (("an empty query", {"query": ""}), ("a whitespace-only query", {"query": "   "}),
             ("a 5,000-character query", {"query": "x" * 5000}), ("a path filter that is not a glob",
                                                                   {"path": "src/[unclosed"}))
GENERIC_CHARS = 200
# The shipped audit and its limits, hashed: a change to either is an integrity flag.
AUDIT_SHA = {"tools/audit_schemas.py": "5161efeb16d7872a", "tools/audit_limits.yaml": "35bf8fadbdbe4281"}


@dataclass
class Section:
    name: str
    possible: float
    earned: float = 0.0
    notes: list[str] = field(default_factory=list)
    manual: bool = False

    def note(self, text: str) -> None:
        self.notes.append(text)


def raised(what: str) -> str:
    return f"{what} raised:\n" + traceback.format_exc(limit=3)


def sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


# -- a registry, and a run, under the checker's control -------------------------------------------

class Watched:
    """Counts calls to each tool module's run(), so a call refused at the boundary can be
    told from one that ran and failed. The registry looks run up when it calls it."""

    def __init__(self):
        from tools import MODULES
        self.ran: list[str] = []
        self._saved = {}
        for name in MODULES:
            module = importlib.import_module(f"tools.{name}")
            original = module.run
            self._saved[name] = (module, original)

            def counted(*args, _name=name, _original=original, **kwargs):
                self.ran.append(_name)
                return _original(*args, **kwargs)
            module.run = counted

    def restore(self) -> None:
        for module, original in self._saved.values():
            module.run = original


def context(tmp: Path, label: str, *, faults=None, readback_fail=False, switches=None):
    """A fresh RunContext with its own tracker, call record and harness log."""
    from tools import RunContext
    run_id = f"check-{label}-{uuid.uuid4().hex[:6]}"
    tracker = Tracker(ROOT, state_dir=tmp / f"tracker-{label}", flaky=False, readback_fail=readback_fail,
                      faults=faults)
    ctx = RunContext(run_id=run_id, tracker=tracker, repo_root=ROOT / "fixtures" / "repo",
                     calls=CallRecord(tmp / f"{run_id}.calls.jsonl", run_id, ROOT),
                     harness_log=tmp / f"{run_id}.harness.log", switches={**switch_board.DEFAULTS, **(switches or {})})
    return ctx


def registry(tmp: Path, label: str, **kw):
    from tools import default_tools
    ctx = context(tmp, label, **kw)
    return default_tools(ctx), ctx


def written(ctx) -> tuple[list[dict], list[dict]]:
    """What the tracker holds for this context's run: (comments, every PR)."""
    reader = Tracker(ROOT, state_dir=ctx.tracker.state_dir, flaky=False, readback_fail=False)
    return reader.comments(ctx.run_id), reader.prs(ctx.run_id, state=None)


def scripted_run(backend, issue: int, tmp: Path, label: str, *, way: str = "planned", **kw) -> dict:
    """Your way, run with a scripted model and the checker's tracker. Nothing reaches runs/."""
    from tools import default_tools
    settings = load_settings(ROOT / "settings.yaml")
    ctx_tools = context(tmp, label, **kw)
    trace_path = tmp / f"{ctx_tools.run_id}.jsonl"
    trace = TraceWriter(trace_path, ctx_tools.run_id, {
        "lab": LAB, "way": way, "issue": issue, "sweep": 0, "backend": f"harness.scripted:{type(backend).__name__}",
        "critique": "off", "notes": "off", "switches": ctx_tools.switches})
    client = ModelClient(backend, PriceSheet(ROOT / "pricing.yaml"), session_ceiling=settings.session_token_ceiling)
    error = None
    try:
        tools = default_tools(ctx_tools)
        ctx = WayContext(ctx_tools.run_id, issue, client, tools, trace, ctx_tools.tracker, settings, ROOT,
                         runs_dir=tmp)
        importlib.import_module(f"ways.{way}").run(ctx)
    except NotImplementedError:
        error = "NotImplementedError"
    except BudgetExceeded:
        error = "BudgetExceeded"
    except Exception:
        error = traceback.format_exc(limit=3)
    trace.run_end(client.totals(), error)
    records = read_trace(trace_path)
    comments, prs = written(ctx_tools)
    log = Path(ctx_tools.harness_log)
    return {"records": records, "comments": comments, "prs": prs, "calls": read_calls(ctx_tools.calls.path),
            "log": log.read_text() if log.exists() else "", "error": error,
            "ends": [r.get("terminal_reason") for r in records if r.get("type") == "end"]}


def explain(error: str | None) -> str:
    if error == "NotImplementedError":
        return "ways/planned.py: run is not written yet"
    if error == "BudgetExceeded":
        return "the run went on until the harness's token ceiling stopped it"
    return "your way raised:\n" + (error or "")


# -- 1. Definitions --------------------------------------------------------------------------------

def provider_has_strict() -> bool:
    settings = load_settings(ROOT / "settings.yaml")
    if settings.provider == "anthropic":
        return True
    return settings.provider == "openai-compatible" and (not settings.base_url or "api.openai.com" in settings.base_url)


def ping_lines() -> list[dict]:
    path = ROOT / "runs" / "_ping.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def check_definitions(report: dict, tmp: Path) -> Section:
    s = Section("Definitions", 18)
    try:
        result = importlib.import_module("tools.audit_schemas").audit(ROOT)
    except Exception:
        s.note(raised("tools/audit_schemas.py"))
        return s
    problems = result["problems"]
    weights = (("names", 3, "names namespaced by one scheme"), ("closed", 2, "additionalProperties: false throughout"),
               ("limits", 3, "within both vendors' strict-mode limits"), ("strict", 1, "a strict field on every tool"),
               ("examples", 2, "two valid examples per tool"))
    for key, points, what in weights:
        if not problems.get(key) and not problems.get("roles"):
            s.earned += points
        else:
            s.note(f"{what}: " + "; ".join((problems.get("roles") or []) + (problems.get(key) or []))[:600])
    if not problems.get("result") and not problems.get("format") and not problems.get("roles"):
        s.earned += 2
    else:
        s.note("an emptiness sentence and a cap on every tool, and response_format on the search and read tools: "
               + "; ".join((problems.get("result") or []) + (problems.get("format") or []))[:600])
    counts = result["counts"]

    # The live request: the latest try of make ping with these definitions, accepted.
    try:
        from harness.ping import current_schemas, definitions_hash
        schemas = current_schemas()
        digest = definitions_hash(schemas)
    except Exception:
        s.note(raised("building your registry's schemas"))
        return s
    scripted = [p for p in ping_lines() if p.get("definitions") == digest and is_free(str(p.get("backend") or ""))]
    tries = [p for p in ping_lines() if p.get("definitions") == digest and not is_free(str(p.get("backend") or ""))]
    accepted = next((p for p in reversed(tries) if p.get("accepted")), None)
    all_strict = all(x.get("strict") is True for x in schemas)
    if accepted is None:
        refused = tries[-1] if tries else None
        s.note("no accepted request carrying your current five definitions in runs/_ping.jsonl: "
               + (f"the vendor refused them: {refused.get('error')}" if refused
                  else "run `make ping` after your last change to tools/"
                  + (" (a scripted model's ping tests the plumbing and counts for nothing)" if scripted else "")))
    elif provider_has_strict() and not all_strict:
        s.earned += 2
        s.note("your vendor accepted the five definitions, but it has a strict mode and not every definition "
               "asks for it (Part A, task 5)")
    else:
        s.earned += 4
        if accepted.get("backend") not in REAL_BACKENDS:
            s.note(f"the accepted request came from {accepted.get('backend')}, not a vendor: plumbing, not a measurement")

    strict = report.get("strict") if isinstance(report.get("strict"), dict) else {}
    want = {"tools": counts.get("strict_tools"), "optional_params": counts.get("optional_params"),
            "union_types": counts.get("union_types")}
    got = {k: strict.get(k) for k in want}
    compiles = strict.get("compiles", "missing")
    expect_compiles = (None if not any(x.get("strict") for x in schemas)
                       else bool(accepted) if tries else "unknown")
    if got == want and (compiles == expect_compiles or expect_compiles == "unknown"):
        s.earned += 1
    else:
        s.note(f"reports/LAB_03.json strict is {got} with compiles {compiles!r}; the audit counts {want}"
               + (f" and runs/_ping.jsonl says compiles {expect_compiles!r}" if expect_compiles != "unknown" else ""))
    return s


# -- 2. Emptiness ----------------------------------------------------------------------------------

def search_args(**values) -> tuple[str, dict]:
    name, args = role_call("search", **{k: v for k, v in values.items() if k == "query"})
    if "path" in values:
        args[roles()["search"]["args"]["path"]] = values["path"]
    return name, args


def check_emptiness(tmp: Path) -> Section:
    s = Section("Emptiness is observable", 7)
    try:
        tools, _ = registry(tmp, "empty")
        name, args = search_args(query=NO_MATCH)
        result = tools.call(name, args)
    except Exception:
        s.note(raised("a search for a term that matches nothing"))
        return s
    try:
        data = json.loads(result.content)
    except (TypeError, ValueError):
        data = None
    if not result.is_error and isinstance(data, dict) and data.get("count") == 0 and data.get("query_valid") is True:
        s.earned += 3
    else:
        s.note(f"a search for {NO_MATCH!r}, which matches nothing, came back is_error {result.is_error} with "
               f"{result.content[:160]!r}: want a success whose JSON carries count 0 and query_valid true")
    for what, values in MALFORMED:
        try:
            name, args = search_args(**values) if "path" not in values else search_args(query="export", **values)
            got = tools.call(name, args)
        except Exception:
            s.note(raised(f"a search with {what}"))
            continue
        if got.is_error:
            s.earned += 1
        else:
            s.note(f"a search with {what} came back as a success ({got.content[:120]!r}): a malformed query is an error")
    return s


# -- 3. Boundary validation ----------------------------------------------------------------------------

def mutations(definition: dict, args: dict) -> list[tuple[str, str, dict]]:
    """(what, the field it names, the bad arguments): a required field dropped, an unknown
    field added, one field of the wrong type."""
    schema = definition.get("input_schema") or {}
    props = schema.get("properties") or {}
    required = [r for r in schema.get("required") or [] if r in args]
    out = []
    if required:
        dropped = dict(args)
        dropped.pop(required[0])
        out.append((f"without {required[0]}", required[0], dropped))
    out.append(("with an unknown field zz_unexpected", "zz_unexpected", {**args, "zz_unexpected": 1}))
    for key, prop in props.items():
        kinds = prop.get("type") if isinstance(prop.get("type"), list) else [prop.get("type")]
        if "string" in kinds and "integer" not in kinds and key in args:
            out.append((f"with {key} an integer", key, {**args, key: 12345}))
            break
        if "integer" in kinds and "string" not in kinds and key in args:
            out.append((f"with {key} a string", key, {**args, key: "twelve"}))
            break
    return out


def check_boundary(tmp: Path) -> Section:
    s = Section("Boundary validation", 10)
    watched = Watched()
    passed = tried = 0
    try:
        table = roles()
        for role in ROLES:
            tools, ctx = registry(tmp, f"boundary-{role}")
            values = {"read_issue": {"number": WRITE_ISSUE}, "comment": {"number": WRITE_ISSUE, "body":
                      "label: bug\nfile: src/components/ExportButton.tsx"},
                      "open_pr": {"number": WRITE_ISSUE, "branch": f"repomind/issue-{WRITE_ISSUE}",
                                  "title": "A title", "body": "A body"},
                      "read_file": {"path": "README.md"}, "search": {"query": "export"}}[role]
            name, args = role_call(role, **values)
            definition = tools.definition(name)
            for what, key, bad in mutations(definition, args):
                tried += 1
                before = len(watched.ran)
                result = tools.call(name, bad)
                comments, prs = written(ctx)
                ran = len(watched.ran) > before
                if result.is_error and key.lower() in result.content.lower() and not ran and not comments and not prs:
                    passed += 1
                else:
                    why = ("it ran the tool" if ran else "a write reached the tracker" if comments or prs
                           else "it was not an error" if not result.is_error
                           else f"the error does not name {key}")
                    s.note(f"{name} {what}: {why} ({result.content[:100]!r})")
        if table:
            s.earned = round(10 * passed / max(tried, 1), 2)
    except Exception:
        s.note(raised("the boundary calls"))
    finally:
        watched.restore()
    if tried:
        s.notes.insert(0, f"{passed} of {tried} malformed calls refused before the tool ran, each naming its field")
    return s


# -- 4. Error contract -------------------------------------------------------------------------------

def check_errors(tmp: Path) -> Section:
    s = Section("Error contract", 10)
    try:
        tools, _ = registry(tmp, "nearmiss")
        name, args = role_call("read_file", path=MISSING_PATH)
        result = tools.call(name, args)
    except Exception:
        s.note(raised(f"reading {MISSING_PATH}"))
        result = None
    if result is not None:
        named = [p for p in NEAR_MISSES if p in result.content]
        if not result.is_error:
            s.note(f"reading {MISSING_PATH}, which does not exist, succeeded ({result.content[:80]!r}): compare the "
                   "path with the repository's file list, case included")
        elif named:
            s.earned += 2 + len(named)
        if result.is_error and len(named) < len(NEAR_MISSES):
            s.note(f"the error names {named or 'none'} of the three near misses {list(NEAR_MISSES)}: "
                   f"{result.content[:160]!r}")

    backend = UnknownToolCaller()
    try:
        run = scripted_run(backend, AGENT_ISSUE, tmp, "unknown", way="agent")
    except Exception:
        s.note(raised("the agent's run with a model that calls an unknown tool"))
        return s
    if UNKNOWN_TOOL in run["log"]:
        s.earned += 2
    else:
        s.note(f"the harness log (runs/<run_id>.harness.log) does not mention {UNKNOWN_TOOL}, the unknown tool")
    tools_named = set()
    try:
        tools_named = set(registry(tmp, "names")[0].names())
    except Exception:
        pass
    if backend.calls == 1 and run["ends"] == ["tool_error"]:
        s.earned += 3
    elif backend.calls >= 2:
        last = backend.requests[1]["messages"][-1]
        blocks = [b for b in (last.get("content") if isinstance(last.get("content"), list) else [])
                  if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == "toolu_unknown_0001"]
        text = str(blocks[0].get("content")) if blocks else ""
        leaks = [n for n in tools_named | {UNKNOWN_TOOL} if n in text]
        if blocks and blocks[0].get("is_error") and len(text) <= GENERIC_CHARS and not leaks:
            s.earned += 3
        else:
            s.note("after the unknown tool call, the next request carried "
                   + (f"{text[:120]!r}" if blocks else "no tool_result for it")
                   + (f", which names {sorted(leaks)}" if leaks else "")
                   + f": want an is_error result of at most {GENERIC_CHARS} characters with no protocol detail, "
                     "or the run ended tool_error")
    else:
        s.note(f"the agent's run ended {run['ends']} before calling the model"
               + (f"\n{explain(run['error'])}" if run["error"] else ""))
    return s


# -- 5 and 6. Writes happen once; the three exits ---------------------------------------------------------

def pr_lines(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c.get("role") == "open_pr" and c.get("outcome") in ("ok", "timeout")]


def order_hint(run: dict) -> str:
    """When the run's first write was its comment: the checker's faults, meant for the PR
    first, fell on the wrong writes."""
    first = next((c.get("role") for c in run.get("calls") or [] if c.get("role") in WRITES), None)
    return ("; your run wrote its comment before its PR, so the faults meant for the PR fell on the comment: open "
            "the PR first and post the comment last (Part C, task 1)") if first == "comment" else ""


def rejected(run: dict) -> str:
    """When your planned way rejected the checker's plan, so the scenario wrote nothing."""
    records = run.get("records") or []
    errors = [e for r in records if r.get("type") == "schema" for e in r.get("errors") or []]
    invalid = [str(r.get("failures"))[:200] for r in records if r.get("type") == "validation" and r.get("valid") is False]
    if not errors and not invalid:
        return ""
    return ("; your planned way rejected the checker's plan (read the issue, search, read a file, open the PR, post "
            f"the comment): {(errors or invalid)[0]}")


def scripted_scenarios(tmp: Path) -> dict:
    """The faults fall on the run's write attempts in order: the PR's, then the comment's.
    A: the PR times out before committing and its retry goes through; the comment
       commits then times out.
    B: the PR commits then times out, and nothing can be read back.
    C: both commit then time out, under the control's switches."""
    out = {}
    plans = {"A": {"faults": ["before_commit", None, "after_commit"]},
             "B": {"faults": ["after_commit"], "readback_fail": True},
             "C": {"faults": ["after_commit", "after_commit"], "switches": {"key_mode": "uuid", "verify": "off"}}}
    for name, kw in plans.items():
        try:
            out[name] = scripted_run(ScriptedTriage(), WRITE_ISSUE, tmp, f"writes-{name}", **kw)
        except Exception:
            out[name] = {"crash": raised(f"scenario {name}")}
    return out


def load_arms(answers: dict) -> tuple[dict, list[str]]:
    arms = {a: [] for a in ARMS}
    notes = []
    for path in sorted((ROOT / "runs").glob("*.jsonl")):
        if not is_trace(path):
            continue
        try:
            records = read_trace(path)
        except json.JSONDecodeError:
            notes.append(f"{path.name}: not valid JSON lines, skipped")
            continue
        start = records[0] if records and records[0].get("type") == "run_start" else None
        if not start or start.get("lab") != LAB or start.get("way") != "planned" or is_free(start.get("backend") or ""):
            continue
        if start.get("issue") not in answers:
            continue
        if records[-1].get("type") != "run_end":
            notes.append(f"{path.name}: no run_end record (the run did not finish), skipped")
            continue
        ends = [r.get("terminal_reason") for r in records if r.get("type") == "end"]
        if set(ends) & set(UNFINISHED):
            continue
        recorded = {**switch_board.DEFAULTS, **(start.get("switches") or {})}
        for arm, wanted in ARMS.items():
            if recorded == wanted:
                arms[arm].append({"run_id": start["run_id"], "issue": start["issue"], "end": ends[-1] if ends else None,
                                  "backend": start.get("backend")})
    return arms, notes


def tally(run: dict, db: sqlite3.Connection) -> dict:
    """What the tracker holds for one run: its comments on its issue and its PRs."""
    comments = [dict(r) for r in db.execute("SELECT * FROM comments WHERE run_id = ? AND issue = ? ORDER BY id",
                                            (run["run_id"], run["issue"]))]
    prs = [dict(r) for r in db.execute("SELECT * FROM prs WHERE run_id = ? ORDER BY id", (run["run_id"],))]
    file = parse_triage_comment(comments[0]["body"])[1] if comments else None
    branch = f"repomind/issue-{run['issue']}"
    right_prs = [p for p in prs if p["branch"] == branch and p["state"] == "open"]
    want = 1 if file not in (None, "none") else 0
    return {"comments": comments, "prs": prs, "file": file,
            "prs_ok": len(right_prs) == want and len(prs) == len(right_prs)}


def database() -> sqlite3.Connection | None:
    path = ROOT / "tracker" / "tracker.db"
    if not path.exists():
        return None
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def check_writes(scenarios: dict, arms: dict) -> Section:
    s = Section("Writes happen once", 20)
    a = scenarios.get("A", {})
    if "crash" in a:
        s.note(a["crash"])
    elif a.get("error"):
        s.note("scenario A: " + explain(a["error"]))
    else:
        if len(a["comments"]) == 1 and not set(a["ends"]) - {"completed"}:
            s.earned += 3
        elif len(a["comments"]) == 1:
            s.note(f"scenario A, a comment that committed and then timed out: the run ended {a['ends']}; want it to "
                   "read the comment back and finish")
        else:
            s.note(f"scenario A, a comment that committed and then timed out: the tracker holds {len(a['comments'])} "
                   "comments for the run; want 1" + rejected(a))
        open_prs = [p for p in a["prs"] if p["branch"] == f"repomind/issue-{WRITE_ISSUE}" and p["state"] == "open"]
        if len(open_prs) == 1 and len(a["prs"]) == 1:
            s.earned += 3
        else:
            s.note(f"scenario A, a PR that timed out before committing: the tracker holds {len(a['prs'])} PRs "
                   f"({len(open_prs)} open from repomind/issue-{WRITE_ISSUE}); want 1. It ended {a['ends']}"
                   + order_hint(a))
    c = scenarios.get("C", {})
    if "crash" in c or c.get("error"):
        s.note("scenario C: " + (c.get("crash") or explain(c.get("error"))))
    elif len(c["comments"]) >= 2:
        s.earned += 3
    else:
        s.note(f"scenario C, under KEY_MODE=uuid and VERIFY=off, a comment that committed and timed out left "
               f"{len(c['comments'])} comment(s): the control must retry blind with a fresh key (Part C, task 6)"
               + ("" if c["comments"] else f"; the run ended {c['ends']}" + rejected(c)))

    db = database()
    harness, control = arms["harness"], arms["control"]
    if db is None:
        s.note("no tracker database (tracker/tracker.db): commit it with your runs")
        return s
    good, bad = 0, []
    for run in harness:
        t = tally(run, db)
        if len(t["comments"]) == 1 and t["prs_ok"]:
            good += 1
        else:
            bad.append(f"{run['run_id']}: {len(t['comments'])} comments, {len(t['prs'])} PRs, file {t['file']}")
    if harness:
        s.earned += round(8 * good / max(RUNS_PER_ARM, len(harness)), 2)
        if len(harness) < RUNS_PER_ARM:
            s.note(f"{len(harness)} finished harness-arm runs; Part D asks for {RUNS_PER_ARM}")
        if bad:
            s.note(f"{len(bad)} harness-arm runs left the wrong writes, e.g. " + "; ".join(bad[:3]))
    else:
        s.note("no harness-arm runs: FLAKY_WRITES=1 python run.py --way planned --set eval --runs 2")
    dup = [r for r in control if len(tally(r, db)["comments"]) > 1]
    if len(control) >= RUNS_PER_ARM and dup:
        s.earned += 3
    elif not control:
        s.note("no control-arm runs: FLAKY_WRITES=1 KEY_MODE=uuid VERIFY=off python run.py --way planned "
               "--set eval --runs 2 --start-sweep 3")
    else:
        s.note(f"{len(control)} control-arm runs, {len(dup)} with a duplicate comment; want {RUNS_PER_ARM} runs "
               "and at least one duplicate")
    db.close()
    return s


def check_exits(scenarios: dict) -> Section:
    s = Section("Three exits", 7)
    b = scenarios.get("B", {})
    if "crash" in b or b.get("error"):
        s.note("scenario B: " + (b.get("crash") or explain(b.get("error"))))
    elif not b["prs"] and not b["comments"]:
        s.note(f"scenario B: the run ended {b['ends']} without writing anything, so the third exit was never "
               "reached" + rejected(b))
    elif b["ends"] != ["escalated"]:
        s.note(f"scenario B, a first write that timed out when nothing could be read back: the run ended {b['ends']}; "
               "want escalated (the third exit)")
    else:
        s.earned += 2
        if not b["prs"]:
            s.note("scenario B: the run's first write was its comment, and it could not be confirmed: open the PR "
                   "first and post the comment last, so that a failure before the comment costs nothing a reader "
                   "has seen (Part C, task 1)")
        else:
            if len(b["prs"]) == 1:
                s.earned += 1
            else:
                s.note(f"scenario B: the tracker holds {len(b['prs'])} PRs; want exactly the one that committed")
            if not b["comments"]:
                s.earned += 1
            else:
                s.note("scenario B: a comment posted after a PR that could not be confirmed")
    a = scenarios.get("A", {})
    if "crash" in a or a.get("error"):
        s.note("scenario A: " + (a.get("crash") or explain(a.get("error"))))
        return s
    lines = pr_lines(a["calls"])
    keys = {c.get("key") for c in lines}
    outcomes = [c.get("outcome") for c in lines]
    if len(lines) >= 2 and len(keys) == 1 and None not in keys and outcomes[0] == "timeout" and "ok" in outcomes:
        s.earned += 3
    else:
        s.note(f"scenario A, a PR that timed out before committing: its call-record lines are "
               f"{[(c.get('attempt'), c.get('outcome'), c.get('key')) for c in lines]}; want a timeout, then a retry "
               "with the same key that succeeds" + order_hint(a))
    return s


# -- 7. Auditable trace ---------------------------------------------------------------------------------

def check_audit(arms: dict, tmp: Path) -> Section:
    s = Section("Auditable trace", 10)
    db = database()
    runs = arms["harness"] + arms["control"]
    if db is None or not runs:
        s.note("no Part D runs and tracker database to match against call records")
    else:
        matched = total = 0
        missing = []
        for run in runs:
            calls = read_calls(ROOT / "runs" / f"{run['run_id']}.calls.jsonl")
            keys = {(c.get("role"), c.get("key")) for c in calls if c.get("key") and c.get("outcome") in ("ok", "timeout")}
            for table, role in (("comments", "comment"), ("prs", "open_pr")):
                for row in db.execute(f"SELECT key FROM {table} WHERE run_id = ?", (run["run_id"],)):
                    total += 1
                    if row["key"] and (role, row["key"]) in keys:
                        matched += 1
                    else:
                        missing.append(f"{run['run_id']}: a {table[:-1] if table == 'comments' else 'PR'} with key "
                                       f"{row['key']!r}")
        db.close()
        if total:
            s.earned += round(5 * matched / total, 2)
            if missing:
                s.note(f"{len(missing)} of {total} tracker writes have no call-record line with their key, e.g. "
                       + "; ".join(missing[:3]))
        else:
            s.note("the Part D runs wrote nothing to the tracker")

    for label, hook in (("raises", lambda name, tool_input: 1 / 0),
                        ("denies", lambda name, tool_input: "no writes during the checker's hook test")):
        try:
            tools, ctx = registry(tmp, f"hook-{label}")
            tools.hooks.append(hook)
            name, args = role_call("comment", number=WRITE_ISSUE, body="label: bug\nfile: README.md")
            result = tools.call(name, args)
        except ZeroDivisionError:
            s.note("a hook that raises: the exception reached the caller; catch it and deny the call (fail closed)")
            continue
        except Exception:
            s.note(raised(f"a call with a hook that {label}"))
            continue
        comments, prs = written(ctx)
        lines = read_calls(ctx.calls.path)
        if result.is_error and not comments and any(c.get("outcome") == "denied" for c in lines):
            s.earned += 2.5
        else:
            s.note(f"a hook that {label}: is_error {result.is_error}, {len(comments)} comments reached the tracker, "
                   f"call-record outcomes {[c.get('outcome') for c in lines]}; want a denied call that never ran")
    return s


# -- for staff: the report's numbers against the database ---------------------------------------------------

def writes_from_db(arms: dict, answers: dict) -> dict:
    db = database()
    out = {}
    if db is None:
        return out
    for arm, runs in arms.items():
        per, prs_ok, passes, escalated, committed = [], 0, 0, 0, 0
        for run in runs:
            t = tally(run, db)
            per.append(len(t["comments"]))
            prs_ok += t["prs_ok"]
            answer = answers[run["issue"]]
            if len(t["comments"]) == 1:
                label, file = parse_triage_comment(t["comments"][0]["body"])
                passes += label == answer["label"] and file == normalize_path(answer["file"])
            escalated += run["end"] == "escalated"
            committed += db.execute("SELECT COUNT(*) FROM attempts WHERE run_id = ? AND outcome = 'timeout_after_commit'",
                                    (run["run_id"],)).fetchone()[0]
        out[arm] = {"runs": len(runs), "comments_per_run_max": max(per, default=0),
                    "duplicate_comment_runs": sum(1 for n in per if n > 1), "prs_correct_runs": prs_ok,
                    "committed_timeouts": committed, "escalated": escalated, "triage_passes": passes}
    db.close()
    return out


def report_notes(report: dict, truth: dict) -> list[str]:
    notes = []
    writes = report.get("writes") if isinstance(report.get("writes"), dict) else {}
    for arm, fields in truth.items():
        theirs = writes.get(arm) if isinstance(writes.get(arm), dict) else {}
        diff = {k: (theirs.get(k), v) for k, v in fields.items() if theirs.get(k) != v}
        if diff:
            notes.append(f"reports/LAB_03.json writes.{arm} differs from the database (yours, the database's): {diff}")
    return notes


REF = re.compile(r"`([\w./-]+\.\w+):(\d+)`")


def architecture_notes() -> list[str]:
    """ARCHITECTURE.md's references (checks/FORMATS.md, section 8): each must open a line of
    code outside prompts/. No points: staff read the file with the report."""
    path = ROOT / "ARCHITECTURE.md"
    if not path.exists():
        return ["ARCHITECTURE.md is missing"]
    notes = []
    for file, line in REF.findall(path.read_text()):
        target = ROOT / file
        if file.startswith("prompts/"):
            notes.append(f"ARCHITECTURE.md cites `{file}:{line}`, a prompt: a layer held only by a prompt is EMPTY")
        elif not target.is_file():
            notes.append(f"ARCHITECTURE.md cites `{file}:{line}`: no such file")
        elif not 1 <= int(line) <= len(target.read_text().splitlines()):
            notes.append(f"ARCHITECTURE.md cites `{file}:{line}`: the file has no line {line}")
    return notes


def integrity_flags(answers: dict) -> list[str]:
    flags = []
    paths = {a["file"] for a in answers.values() if a["file"] != "none"}
    for d in ("prompts", "ways", "agent", "tools"):
        for place in sorted((ROOT / d).rglob("*")):
            if not place.is_file() or place.suffix not in (".py", ".md", ".json", ".yaml"):
                continue
            try:
                text = place.read_text()
            except UnicodeDecodeError:
                continue
            rel = place.relative_to(ROOT).as_posix()
            for path in sorted(paths):
                if path in text:
                    flags.append(f"`{rel}` contains the evaluation answer `{path}`")
            if "answers.yaml" in text:
                flags.append(f"`{rel}` reads the answer file; only eval/ should")
            if d in ("tools", "ways", "agent") and ("attempts" in text and ("table(" in text or "FROM attempts" in text)):
                flags.append(f"`{rel}` reads the tracker's attempts log, which is the tracker's, not your harness's")
    for rel, wanted in AUDIT_SHA.items():
        if sha(ROOT / rel) != wanted:
            flags.append(f"`{rel}` differs from the course's copy")
    return flags


def main() -> int:
    report_path = ROOT / "reports" / "LAB_03.json"
    try:
        report = json.loads(report_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"reports/LAB_03.json could not be read ({exc}); checking what can be checked without it.\n")
        report = {}
    if not isinstance(report, dict):
        report = {}
    answers = {int(k): v for k, v in yaml.safe_load((ROOT / "fixtures" / "answers.yaml").read_text())["eval"].items()}
    arms, load_notes = load_arms(answers)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        try:
            scenarios = scripted_scenarios(tmp)
        except Exception:
            scenarios = {k: {"crash": raised("the scripted runs")} for k in "ABC"}
        checks = [
            (lambda: check_definitions(report, tmp), "Definitions", 18),
            (lambda: check_emptiness(tmp), "Emptiness is observable", 7),
            (lambda: check_boundary(tmp), "Boundary validation", 10),
            (lambda: check_errors(tmp), "Error contract", 10),
            (lambda: check_writes(scenarios, arms), "Writes happen once", 20),
            (lambda: check_exits(scenarios), "Three exits", 7),
            (lambda: check_audit(arms, tmp), "Auditable trace", 10),
        ]
        sections = []
        for check, name, pts in checks:
            try:
                sections.append(check())
            except Exception:
                sections.append(Section(name, pts, notes=[raised("this check")]))
    sections.append(Section("Report", 18, manual=True, notes=["read by staff from reports/LAB_03.md"]))

    backends = sorted({r["backend"] for runs in arms.values() for r in runs})
    print(f"Assignment 3 check · runs read from runs/ · backends seen: {', '.join(backends) or 'none'}")
    print(f"  Part D arms: harness {len(arms['harness'])} runs, control {len(arms['control'])} runs "
          f"(want {RUNS_PER_ARM} each)")
    if any(b not in REAL_BACKENDS for b in backends):
        print("  note: runs from a scripted or simulated backend are not a measurement of a model")
    for note in load_notes[:5]:
        print(f"  note: {note}")
    print()
    auto_earned = auto_possible = 0.0
    for sec in sections:
        score = "staff" if sec.manual else f"{round(sec.earned, 2):>5g} / {sec.possible:g}"
        print(f"{sec.name:<28} {score}")
        for note in sec.notes:
            for i, line in enumerate(note.splitlines()):
                print(f"{'    - ' if i == 0 else '      '}{line}")
        if not sec.manual:
            auto_earned += sec.earned
            auto_possible += sec.possible
    print(f"\nAutomated: {round(auto_earned, 2):g} / {auto_possible:g}   (plus up to 18 from staff for the report)")
    for note in report_notes(report, writes_from_db(arms, answers)) + architecture_notes():
        print(f"  for staff: {note}")
    flags = integrity_flags(answers)
    print("\nIntegrity flags (no points; staff review): " + ("none" if not flags else ""))
    for flag in flags:
        print(f"    - Integrity flag: {flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
