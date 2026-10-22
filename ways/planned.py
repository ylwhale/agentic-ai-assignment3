"""Assignment 2's working solution, Parts B, C and D: plan, validate, execute inside the
boundary, revise at one checkpoint, and a second pass before posting.
It ships in Assignment 3's starter in place of your own Assignment 2 code, changed in
two ways only: it finds each tool by its role in tools/roles.yaml (harness.roles), so
a tool you rename still runs, and it reads a search result as text or as JSON
(harness.formats.result_paths). The plan rule is still Assignment 2's, the comment
last; a PR step runs like any other step, unskipped, until Part C.

The boundary: the executor runs the plan's steps in order and nothing else. Where a
step's arguments are "$fill", one model call fills them, offered that step's tool
alone; a call to any other tool is refused, traced as off_plan, and never runs.
Two tries without an on-plan call halt the run as escalated.

The planner's output is checked against agent/plan_schema.json (harness.schema)
before the four checks, and so is the rule that its last step is the comment: a
vendor can accept "strict" and not enforce it, and a plan with no comment step
passes all four checks and posts nothing. A plan that breaks either is rejected
like one that fails a check.

The checkpoint, a check in code: after the first search step, and after any search
step a revision added or changed, until MAX_REVISIONS is spent, the search returned
at least one source file: a .ts or .tsx file under src/, not only documentation
(README.md, CHANGELOG.md, docs/) or translations (src/i18n/*.json). A revision never
changes a step that is done; it may change a failed step, which then runs again.

Part G's ways/notes.py extends this file without changing it: it subclasses
Executor and overrides its hooks, record() (after every step), context() (what
every executor call sees) and the class attribute critique, and asks its questions
once run() has returned, however the run ended.

The second pass, before the comment, in ctx.critique's mode: external runs
agent.critique.verify, code; self asks the model with agent.critique.self_critique.
A failing draft with a round left is rewritten by one call; a draft still failing
in the last round is held, and the run escalates. One comment, after the last round.
"""

from __future__ import annotations

import copy
import json
import re

from agent.critique import self_critique, verify
from agent.diff import step_args
from agent.plan import SCHEMA, make_plan, revise_plan
from agent.validator import validate
from harness.formats import FILL, format_triage_comment, parse_triage_comment, result_paths
from harness.roles import ROLES, arg, tool_for
from harness.schema import errors as schema_errors

MAX_REPLANS = 2       # three planner calls in all; a fourth on the same request is the same coin, so a person looks
MAX_REVISIONS = 2     # a repair of the first search, and one more if the repair also finds nothing
MAX_ROUNDS = 2        # check, rewrite once, check again: slide 43's limit, the same in both modes
MAX_FILL_TRIES = 2    # a step the model will not fill on plan, twice, halts the run
CHECKPOINT = "after_first_search"
SOURCE = re.compile(r"^src/(?!i18n/).+\.tsx?$")   # code, not documentation or translations
CONTEXT_CHARS = 6000  # of each earlier result, in a fill call


def role(name: str | None) -> str | None:
    """The role a step's tool plays (tools/roles.yaml), or None."""
    return next((r for r in ROLES if tool_for(r) == name), None)


def allows_null(spec: dict) -> bool:
    """Whether a property's schema takes null: "type": [..., "null"], or a null branch."""
    kind = spec.get("type")
    return ("null" in kind if isinstance(kind, list) else kind == "null") or any(
        isinstance(b, dict) and allows_null(b) for b in spec.get("anyOf") or spec.get("oneOf") or [])


def run(ctx) -> None:
    plan = plan_until_valid(ctx)
    if plan is None:
        return
    ctx.trace.plan(1, plan)
    Executor(ctx, plan).run()


def schema_check(ctx, plan: dict, version: int) -> list[dict]:
    """The plan as the planner's tool returned it, against agent/plan_schema.json. A
    failure is written as a schema line, and comes back as failures for a re-plan."""
    problems = schema_errors({k: v for k, v in plan.items() if k != "meta"}, SCHEMA)
    steps = plan.get("steps") or []
    if not steps or not isinstance(steps[-1], dict) or role(steps[-1].get("tool")) != "comment":
        problems.append(f"the plan's last step is not the comment tool, {tool_for('comment')}: the comment "
                        "comes last")
    if problems:
        ctx.trace.event("schema", version=version, errors=problems[:20])
    return [{"step": None, "check": "schema", "error": e} for e in problems[:5]]


def plan_until_valid(ctx) -> dict | None:
    """The reject exit: re-plan up to MAX_REPLANS times, then escalate. Never execute a rejected plan."""
    failures = None
    for attempt in range(1, MAX_REPLANS + 2):
        plan = make_plan(ctx.client, ctx.issue_number, ctx.tools.schemas, trace=ctx.trace, failures=failures)
        broken = schema_check(ctx, plan, 1)
        if broken:                                # rejected before the four checks: it never runs
            failures = broken
            continue
        verdict = validate(plan)
        ctx.trace.event("validation", version=1, attempt=attempt, valid=verdict["valid"],
                        failures=verdict["failures"])
        if verdict["valid"]:
            return plan
        failures = verdict["failures"]
    ctx.trace.end("escalated", detail=f"no valid plan in {MAX_REPLANS + 1} attempts; last failures {failures}")
    return None


class Executor:
    critique = True   # the notes way turns the second pass off

    def __init__(self, ctx, plan: dict):
        self.ctx = ctx
        self.plan = copy.deepcopy(plan)
        self.version = 1
        self.results: list[dict] = []    # step_results (checks/FORMATS.md, section 10.3), in the order steps ran
        self.outputs: dict[str, str] = {}  # each step's tool output, for later calls; never shown to the reviser
        self.calls = 0                   # execute and critique calls so far: the step number of the next lines
        self.revisions = 0
        self.revised: set[str] = set()   # ids of steps a revision added or changed: the checkpoint follows them
        self.ran: set[str] = set()       # ids of steps that ran and are not to run again

    # -- the plan, in order ------------------------------------------------------------
    def next_step(self) -> dict | None:
        return next((s for s in self.plan.get("steps") or [] if str(s.get("id")) not in self.ran), None)

    def run(self) -> None:
        while True:
            step = self.next_step()
            if step is None:
                return                            # the plan is finished; the runner writes completed
            if role(step.get("tool")) == "comment":
                if not self.comment(step):
                    return
                continue
            first = not any(role(r["tool"]) == "search" for r in self.results)
            result = self.run_step(step)
            if result is None:
                return                            # halted, and ended escalated
            if role(step.get("tool")) == "search" and (first or str(step.get("id")) in self.revised):
                self.checkpoint(CHECKPOINT, any(SOURCE.match(p) for p in result.get("paths") or []))

    # -- one step ------------------------------------------------------------------------
    def run_step(self, step: dict) -> dict | None:
        tool, args = step.get("tool"), step_args(step)
        if not tool:
            return self.record(step, args, None, "no_tool")
        args = self.filled(step, args)
        if args is None:
            return None
        result = self.ctx.call_tool(self.calls, tool, args, plan_step=step.get("id"))
        return self.record(step, args, result)

    def filled(self, step: dict, args: dict) -> dict | None:
        to_fill = [k for k, v in args.items() if v == FILL]
        if not to_fill:
            return args
        filled = self.fill(step, args, to_fill)
        if filled is None:
            self.record(step, args, None, "off_plan")
            self.ctx.trace.end("escalated", detail=f"step {step.get('id')}: no on-plan call in {MAX_FILL_TRIES} tries")
        return filled

    def fill(self, step: dict, args: dict, keys: list[str], *, system: str = "executor.md",
             purpose: str = "execute", note: str = "") -> dict | None:
        """One model call fills a step's "$fill" arguments, offered the step's tool alone. It
        keeps every argument the model gave that the schema knows, the ones the plan never
        mentioned included, and a null where the schema allows one (Part A's required and
        nullable fields); the plan's own values win."""
        schema = next((t for t in self.ctx.tools.schemas if t["name"] == step["tool"]), None)
        if schema is None:
            return None
        fixed = {k: v for k, v in args.items() if v != FILL}
        props = (schema.get("input_schema") or {}).get("properties") or {}
        nullable = {k for k, s in props.items() if isinstance(s, dict) and allows_null(s)}
        for _ in range(MAX_FILL_TRIES):
            self.calls += 1
            response = self.ctx.client.create(system=self.ctx.prompt(system), tools=[schema],
                                              messages=[{"role": "user", "content": self.fill_request(step, fixed, keys, note)}])
            self.ctx.trace.model_call(self.calls, response, purpose=purpose)
            for call in response.tool_calls:
                if call.name != step["tool"]:
                    self.refuse(step, call)       # the boundary: never run, always traced
                    continue
                filled = {**{k: v for k, v in call.input.items() if k in props}, **fixed}   # the plan's values win
                if any(k not in filled or filled[k] in ("", FILL) or (filled[k] is None and k not in nullable)
                       for k in keys):
                    continue
                if role(step["tool"]) == "comment":
                    body = arg("comment", "body")
                    label, file = parse_triage_comment(str(filled[body]))
                    filled[body] = format_triage_comment(label or "bug", file)
                return filled
        return None

    def refuse(self, step: dict, call) -> None:
        self.ctx.trace.event("off_plan", step=step.get("id"), tool=call.name, input=call.input, version=self.version)

    def context(self) -> list[str]:
        """What the run has gathered so far, one block per step, for a fill or a critique."""
        return [f"## Step {r['id']}, {r['tool']}, {r['status']}\n{self.outputs.get(str(r['id']), '')[:CONTEXT_CHARS]}"
                for r in self.results]

    def fill_request(self, step: dict, fixed: dict, keys: list[str], note: str = "") -> str:
        parts = [f"Triage issue #{self.ctx.issue_number}. You are running step {step.get('id')} of a fixed "
                 f"plan: {step.get('action')}. Call {step['tool']} once. Fill in: {', '.join(keys)}. "
                 f"The plan fixes: {json.dumps(fixed)}."]
        if role(step["tool"]) == "comment":
            parts.append("The comment's first two lines must be:\nlabel: <bug | feature | question | docs>\n"
                         "file: <path relative to the repository root, or none>")
        if note:
            parts.append(note)
        return "\n\n".join(parts + self.context())

    def record(self, step: dict, args: dict, result, error: str | None = None) -> dict:
        tool = step.get("tool")
        text = result.content if result is not None else ""
        count, paths = None, []
        if result is not None and role(tool) in ("search", "read_file"):
            paths = result_paths(tool, args, text, result.is_error)
            count = len(paths) if role(tool) == "search" and not result.is_error else None
        if error is None and result is not None and result.is_error:
            error = "not_found" if "no such file" in text else "tool_error"
        if error is None and role(tool) == "search" and not count:
            error = "no_matches"
        entry = {"id": step.get("id"), "tool": tool, "status": "failed" if error else "done", "count": count,
                 "paths": paths, "error_code": error, "args": args}
        self.results.append(entry)
        self.ran.add(str(step.get("id")))
        self.outputs[str(step.get("id"))] = text
        for s in self.plan.get("steps") or []:
            if str(s.get("id")) == str(step.get("id")):
                s["status"] = entry["status"]
        return entry

    # -- revision, at the checkpoint whose check failed --------------------------------------
    def checkpoint(self, name: str, ok: bool) -> bool:
        """True when a new version is now active."""
        if ok or self.revisions >= MAX_REVISIONS:
            return False
        self.revisions += 1
        proposed = revise_plan(self.ctx.client, self.plan, self.results, trace=self.ctx.trace)
        broken = schema_check(self.ctx, proposed, self.version + 1)
        verdict = {"valid": False, "failures": broken} if broken else validate(proposed)
        self.ctx.trace.event("revision", checkpoint=name, version=self.version + 1, valid=verdict["valid"],
                             failures=verdict["failures"])
        if not verdict["valid"]:
            return False                          # rejected: it never runs, and it counted against the bound
        before = {str(s.get("id")): {k: v for k, v in s.items() if k != "status"} for s in self.plan.get("steps") or []}
        changed = {str(s.get("id")) for s in proposed.get("steps") or []
                   if before.get(str(s.get("id"))) != {k: v for k, v in s.items() if k != "status"}}
        self.revised |= changed
        failed = {str(r["id"]) for r in self.results if r["status"] == "failed"}
        done = {str(r["id"]) for r in self.results if r["status"] == "done"}
        self.ran -= (changed & failed) - done      # a failed step the revision changed runs again
        self.version += 1
        self.plan = proposed
        self.ctx.trace.plan(self.version, proposed)
        return True

    # -- the comment, and the second pass before it ----------------------------------------------
    def comment(self, step: dict) -> bool:
        """Draft, run the second pass, then post once. False when the run has ended."""
        draft = self.filled(step, step_args(step))
        if draft is None:
            return False
        final = self.second_pass(step, draft) if self.critique else draft
        if final is None:
            self.record(step, draft, None, "held")
            self.ctx.trace.end("escalated", detail=f"the draft still failed the second pass after {MAX_ROUNDS} rounds")
            return False
        self.record(step, final, self.ctx.call_tool(self.calls, step["tool"], final, plan_step=step.get("id")))
        return True

    def evidence(self) -> dict:
        issue = next((self.outputs.get(str(r["id"]), "") for r in self.results if role(r["tool"]) == "read_issue"), "")
        return {"issue": issue, "results": self.context()}

    def second_pass(self, step: dict, args: dict) -> dict | None:
        mode = self.ctx.critique
        if mode not in ("self", "external"):
            return args
        body = arg("comment", "body")
        for round_ in range(1, MAX_ROUNDS + 1):
            draft = args[body]
            if mode == "external":
                verdict = verify(draft, self.ctx)
            else:
                self.calls += 1
                v = self_critique(self.ctx.client, draft, self.evidence(), self.ctx.trace, step=self.calls)
                verdict = {"passed": v["decision"] == "accept", "reasons": [v["reason"]] if v.get("reason") else []}
            reason = "; ".join(verdict["reasons"]) or None
            changed = False
            if not verdict["passed"] and round_ < MAX_ROUNDS:
                rewritten = self.rewrite(step, args, reason or "the critic asked for a revision")
                if rewritten is not None:
                    changed = rewritten[body] != draft
                    args = rewritten
            self.ctx.trace.event("critique", round=round_, mode=mode, result="pass" if verdict["passed"] else "fail",
                                 reason=reason, changed=changed, draft=draft)
            if verdict["passed"]:
                return args
        return None                               # still failing in the last round: held

    def rewrite(self, step: dict, args: dict, reason: str) -> dict | None:
        """One model call rewrites the draft with the reason, offered the comment tool alone."""
        body = arg("comment", "body")
        note = f"Rewrite the draft. The draft:\n\n{args[body]}\n\nWhy it must change: {reason}"
        return self.fill(step, {**args, body: FILL}, [body], system="rewrite.md", purpose="critique", note=note)
