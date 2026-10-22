"""RepoMind's tool registry: schema validation and safe tool dispatch.

Part A: examples in model schemas; validate inputs and output shapes.
Part B foundations: protocol errors, repair feedback and call records.
Part C: derived idempotency keys, check/readback/three-attempt writes, and controls.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from harness.formats import parse_triage_comment
from harness.schema import errors as schema_errors
from harness.tracker import ReadbackUnavailable, TrackerTimeout

TOOLS_DIR = Path(__file__).resolve().parent
MODULES = ("read_issue", "search_repo", "read_file", "post_comment", "open_pr")
SENT = ("name", "description", "input_schema", "strict")
MAX_WRITE_ATTEMPTS = 3
WRITES = {"post_comment", "open_pr"}


class ToolError(Exception):
    """A failure whose correction can be explained to the model."""


class ProtocolError(Exception):
    """An invalid tool protocol message or a malformed tool response."""


@dataclass
class ToolResult:
    content: str
    is_error: bool
    latency_s: float


@dataclass
class RunContext:
    run_id: str
    tracker: object
    repo_root: object
    calls: object = None
    harness_log: object = None
    switches: dict = field(default_factory=dict)


def load_definition(module: str) -> dict:
    return json.loads((TOOLS_DIR / f"{module}.json").read_text())


# Required fields and expected Python types for the two JSON-output tools.
# The other three tools currently return human-readable text.
RESULT_FIELDS = {
    "search_repo": {
        "query": str, "query_valid": bool, "count": int,
        "results": list, "truncated": bool, "index_age": str,
        "source": str, "response_format": str,
    },
    "read_file": {
        "path": str, "content": str, "total_chars": int,
        "truncated": bool, "response_format": str,
    },
}


class ToolRegistry:
    def __init__(self, ctx: RunContext, modules=MODULES):
        self.ctx = ctx
        self.hooks: list = []
        self._tools: dict[str, tuple[dict, object]] = {}
        for module in modules:
            definition = load_definition(module)
            self._tools[definition["name"]] = (
                definition, importlib.import_module(f"tools.{module}")
            )

    @property
    def schemas(self) -> list[dict]:
        """Build vendor-facing definitions without mutating saved definitions."""
        definitions = []
        for definition, _ in self._tools.values():
            sent = {k: definition[k] for k in SENT if k in definition}
            examples = definition.get("examples", [])
            if examples:
                sent["description"] = (
                    sent["description"] + "\n\nExamples (typical first):\n"
                    + json.dumps(examples, ensure_ascii=False)
                )
            definitions.append(sent)
        return definitions

    def names(self) -> list[str]:
        return list(self._tools)

    def definition(self, name: str) -> dict:
        return self._tools[name][0]

    def _record(self, name, args, plan_step, outcome, result, started,
                *, is_error, key=None, attempt=1):
        elapsed = time.monotonic() - started
        if self.ctx.calls is not None:
            self.ctx.calls.write(
                tool=name, args=args, plan_step=plan_step, key=key,
                attempt=attempt, outcome=outcome, is_error=is_error,
                result=str(result)[:300], latency_s=elapsed, cost_usd=0.0,
            )
        return ToolResult(str(result), is_error, elapsed)

    def _protocol(self, name, args, plan_step, detail, started):
        if self.ctx.harness_log is not None:
            log = Path(self.ctx.harness_log)
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as f:
                f.write(f"Protocol error: {detail}\n")
        self._record(name, args if isinstance(args, dict) else {},
                     plan_step, "protocol", "Protocol error", started,
                     is_error=True)
        # End the run rather than expose protocol details to the model.
        raise ProtocolError("Invalid tool protocol; run must stop")

    @staticmethod
    def _normalized_arguments(schema: dict, args: dict) -> dict:
        """Canonicalize case-insensitive enum values; keep original fields."""
        normalized = dict(args)
        for name, prop in schema.get("properties", {}).items():
            choices = prop.get("enum")
            value = normalized.get(name)
            if choices and isinstance(value, str):
                match = next((v for v in choices if isinstance(v, str)
                              and v.casefold() == value.casefold()), None)
                if match is not None:
                    normalized[name] = match
        return normalized

    @staticmethod
    def _content_errors(module_name: str, args: dict) -> list[str]:
        problems = []
        if module_name in ("read_issue", "post_comment", "open_pr"):
            if args["number"] <= 0:
                problems.append("number: must be a positive issue number")

        if module_name == "search_repo":
            from tools.search_repo import HARD_CAP, problem
            reason = problem(args["query"], args["path_glob"])
            if reason:
                field = "path_glob" if "glob" in reason else "query"
                problems.append(f"{field}: {reason}")
            if not 1 <= args["max_results"] <= HARD_CAP:
                problems.append(f"max_results: choose an integer from 1 to {HARD_CAP}")

        if module_name == "read_file":
            from harness.formats import normalize_path
            from tools.search_repo import EXCLUDED_DIRS
            path = args["path"]
            normalized = normalize_path(path)
            if normalized in (None, "none") or not path.strip():
                problems.append("path: use an existing non-empty repository-relative path")
            elif Path(path).is_absolute() or ".." in Path(path).parts:
                problems.append("path: must stay inside the repository")
            elif set(Path(normalized).parts) & EXCLUDED_DIRS:
                problems.append("path: excluded directory; choose a readable source file")

        if module_name in ("post_comment", "open_pr"):
            for key in (("body",) if module_name == "post_comment" else ("title", "body")):
                if not args[key].strip():
                    problems.append(f"{key}: must be non-empty")

        if module_name == "post_comment" and args["body"].strip():
            label, path = parse_triage_comment(args["body"])
            if label is None or path is None:
                problems.append("body: first lines must be 'label: ...' and 'file: ...'")

        if module_name == "open_pr":
            expected = f"repomind/issue-{args['number']}"
            if args["branch"] != expected:
                problems.append(f"branch: must be exactly {expected}")
        return problems

    @staticmethod
    def _result_error(module_name: str, content) -> str | None:
        """Check the declared shape before exposing the result to the model."""
        if not isinstance(content, str):
            return "tool result must be text"
        if module_name in RESULT_FIELDS:
            try:
                data = json.loads(content)
            except (ValueError, TypeError):
                return "result is not valid JSON"
            if not isinstance(data, dict):
                return "result must be a JSON object"
            for name, kind in RESULT_FIELDS[module_name].items():
                value = data.get(name)
                if type(value) is not kind:
                    return f"result.{name} must be {kind.__name__}"
            if module_name == "search_repo":
                for i, hit in enumerate(data["results"]):
                    if not isinstance(hit, dict):
                        return f"result.results[{i}] must be an object"
                    for name, kind in (("path", str), ("line", int), ("excerpt", str),
                                       ("excerpt_truncated", bool)):
                        if type(hit.get(name)) is not kind:
                            return f"result.results[{i}].{name} must be {kind.__name__}"
        else:
            expected = {
                "read_issue": ("#", ("Opened by", "Body truncated:")),
                "post_comment": ("Posted comment", ("on issue",)),
                "open_pr": ("Opened draft pull request", ("from", "for issue")),
            }[module_name]
            prefix, fragments = expected
            if not content.startswith(prefix) or any(x not in content for x in fragments):
                return "text result lacks the promised metadata or confirmation"
        return None

    def _run_hooks(self, name: str, args: dict, plan_step, started: float,
                   *, key=None, attempt=1) -> ToolResult | None:
        """Fail closed; each actual retry must pass policy again."""
        for hook in self.hooks:
            try:
                denial = hook(name, args)
            except Exception:
                denial = "a policy hook raised an exception"
            if denial is not None:
                return self._record(
                    name, args, plan_step, "denied",
                    "Tool call denied by policy; the tool was not executed",
                    started, is_error=True, key=key, attempt=attempt,
                )
        return None

    def _derived_write_key(self, plan_step, args: dict) -> str:
        """Derive a stable key from run, plan step, and immutable call arguments."""
        canonical = json.dumps(
            {"run_id": self.ctx.run_id, "step_id": plan_step, "arguments": args},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _existing_write(self, module_name: str, args: dict, key: str) -> dict | None:
        """Read effects from this run only; never inspect the tracker's attempts table."""
        tracker, run_id = self.ctx.tracker, self.ctx.run_id
        if module_name == "open_pr":
            # PRs have a natural identifier (the branch), even in the naive arm.
            existing = tracker.prs(run_id, branch=args["branch"], state="open")
            return next((pr for pr in existing if pr["issue"] == args["number"]), None)
        # Comments have no natural identifier: the stable key creates one.
        return tracker.comment_by_key(run_id, key)

    @staticmethod
    def _existing_confirmation(module_name: str, args: dict, existing: dict) -> str:
        """Same text contract as the write module, without writing a second time."""
        if module_name == "open_pr":
            return (f"Opened draft pull request {existing['id']} from "
                    f"{existing['branch']} for issue {args['number']}.")
        return f"Posted comment {existing['id']} on issue {args['number']}."

    def _safe_write(self, name: str, module_name: str, module, args: dict,
                    plan_step, started: float) -> ToolResult:
        """Check -> write -> verify. Never retry a possibly committed write blindly
        unless VERIFY=off deliberately selects the control arm.
        """
        # Freeze arguments once. Never ask the model to regenerate them on retry.
        args = json.loads(json.dumps(args, ensure_ascii=False))
        switches = self.ctx.switches or {}
        key_mode = switches.get("key_mode", "derived")
        verify = switches.get("verify", "on")
        derived_key = self._derived_write_key(plan_step, args)
        had_timeout = False

        for attempt in range(1, MAX_WRITE_ATTEMPTS + 1):
            attempt_start = time.monotonic()
            key = uuid.uuid4().hex if key_mode == "uuid" else derived_key

            if attempt > 1:
                denied = self._run_hooks(name, args, plan_step, attempt_start,
                                         key=key, attempt=attempt)
                if denied is not None:
                    return ToolResult(denied.content, True, time.monotonic() - started)

            # Required before EVERY attempt, even with VERIFY=off.
            try:
                existing = self._existing_write(module_name, args, key)
            except (ReadbackUnavailable, OSError) as exc:
                message = ("Write outcome cannot be checked; stop and escalate for "
                           f"human review. Tracker readback failed: {exc}")
                if not had_timeout:
                    self._record(name, args, plan_step, "error", message,
                                 attempt_start, is_error=True, key=key, attempt=attempt)
                return ToolResult(message, True, time.monotonic() - started)

            if existing is not None:
                confirmation = self._existing_confirmation(module_name, args, existing)
                # A precheck that finds an earlier write is a satisfied logical call.
                # Use its ACTUAL key, not a new key which the control would invent.
                self._record(name, args, plan_step, "ok", confirmation,
                             attempt_start, is_error=False, key=existing.get("key"),
                             attempt=attempt)
                return ToolResult(confirmation, False, time.monotonic() - started)

            try:
                content = module.run(self.ctx, **args, idempotency_key=key)
                mismatch = self._result_error(module_name, content)
                if mismatch:
                    self._protocol(name, args, plan_step,
                                   f"{name} produced invalid output: {mismatch}",
                                   attempt_start)
                self._record(name, args, plan_step, "ok", content, attempt_start,
                             is_error=False, key=key, attempt=attempt)
                return ToolResult(content, False, time.monotonic() - started)

            except TrackerTimeout as exc:
                had_timeout = True
                # Timeout is UNKNOWN, not a proven failure. Record every attempt.
                self._record(name, args, plan_step, "timeout", f"Write timed out: {exc}",
                             attempt_start, is_error=True, key=key, attempt=attempt)
                if verify == "off":
                    # Naive control: blind retry, but still run next pre-check.
                    continue
                try:
                    existing = self._existing_write(module_name, args, key)
                except (ReadbackUnavailable, OSError) as read_exc:
                    return ToolResult(
                        "Write outcome unknown: readback unavailable after timeout; "
                        f"escalate to human review. Details: {read_exc}",
                        True, time.monotonic() - started,
                    )
                if existing is not None:
                    return ToolResult(
                        self._existing_confirmation(module_name, args, existing),
                        False, time.monotonic() - started,
                    )
                # Verified absent: repeat the SAME frozen arguments/key in derived mode.
                continue

            except ToolError as exc:
                result = self._record(name, args, plan_step, "error", f"Error: {exc}",
                                      attempt_start, is_error=True, key=key, attempt=attempt)
                return ToolResult(result.content, True, time.monotonic() - started)
            except (TypeError, ValueError) as exc:
                result = self._record(name, args, plan_step, "error",
                                      f"Tool execution error: {exc}", attempt_start,
                                      is_error=True, key=key, attempt=attempt)
                return ToolResult(result.content, True, time.monotonic() - started)

        return ToolResult(
            f"Write not confirmed after {MAX_WRITE_ATTEMPTS} attempts; "
            "escalate to human review rather than risk a duplicate.",
            True, time.monotonic() - started,
        )

    def call(self, name: str, tool_input: dict, plan_step=None) -> ToolResult:
        started = time.monotonic()
        entry = self._tools.get(name)
        if entry is None:
            self._protocol(name, tool_input, plan_step,
                           f"unknown tool name: {name!r}", started)
        if not isinstance(tool_input, dict):
            self._protocol(name, tool_input, plan_step,
                           f"arguments for {name!r} must be an object", started)

        definition, module = entry
        args = self._normalized_arguments(definition["input_schema"], tool_input)

        # Keep pre-call hooks fail-closed (including direct calls).
        denied = self._run_hooks(name, args, plan_step, started)
        if denied is not None:
            return denied

        problems = schema_errors(args, definition["input_schema"])
        if not problems:
            problems += self._content_errors(module.__name__.split(".")[-1], args)
        if problems:
            return self._record(name, args, plan_step, "rejected",
                                "Invalid arguments: " + "; ".join(problems),
                                started, is_error=True)

        module_name = module.__name__.split(".")[-1]
        if module_name in WRITES:
            return self._safe_write(name, module_name, module, args, plan_step, started)

        try:
            content = module.run(self.ctx, **args)
            mismatch = self._result_error(module_name, content)
            if mismatch:
                self._protocol(name, args, plan_step,
                               f"{name} produced invalid output: {mismatch}", started)
            return self._record(name, args, plan_step, "ok",
                                content, started, is_error=False)
        except ToolError as exc:
            return self._record(name, args, plan_step, "error",
                                f"Error: {exc}", started, is_error=True)
        except (TypeError, ValueError) as exc:
            return self._record(name, args, plan_step, "error",
                                f"Tool execution error: {exc}", started, is_error=True)


def default_tools(ctx: RunContext) -> ToolRegistry:
    return ToolRegistry(ctx)
