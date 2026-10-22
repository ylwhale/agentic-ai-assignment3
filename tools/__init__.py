"""RepoMind's tools, and the registry that runs them.

Each tool is two files with one name: tools/<module>.json, its definition, the only
thing the model sees of it; and tools/<module>.py, the code that runs it. Rename a
tool inside its .json file and keep both file names. tools/roles.yaml says which
tool plays each of RepoMind's five roles (checks/FORMATS.md, section 11.2).

Assignment 3 starts with five definitions that are weak on purpose: generic names,
free-text filters, nothing said about an empty result, no cap on a response, no
examples. You rewrite them (Part A), and you make this registry check every call
at the boundary, return errors a model can repair from, key every write, verify a
write that failed, record every call and run a hook before each one (Parts A to C).

The registry's interface, which the runner, your ways and the checker use
(checks/FORMATS.md, section 11.3):

    registry = ToolRegistry(ctx)          # ctx is a RunContext, below
    registry.schemas                      # what the vendor gets: name, description, input_schema, strict
    registry.names()                      # the tools' names, in MODULES order
    registry.definition(name)             # the whole definition file, examples included
    registry.hooks.append(hook)           # hook(name, tool_input) runs before every call
    registry.call(name, tool_input, plan_step=None) -> ToolResult

A registry is bound to one run: every write is stamped with that run's ID. Build
one per run with `default_tools(ctx)`.
"""

from __future__ import annotations

import importlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent

# The five modules, in the order the registry offers them. Keep these file names.
MODULES = ("read_issue", "search_repo", "read_file", "post_comment", "open_pr")

# The fields of a definition the vendor gets. Everything else in the file (the
# examples, notes of your own) stays here.
SENT = ("name", "description", "input_schema", "strict")


class ToolError(Exception):
    """A tool failed in a way the model can be told about. Becomes an is_error result."""


@dataclass
class ToolResult:
    content: str
    is_error: bool
    latency_s: float


@dataclass
class RunContext:
    run_id: str
    tracker: object                    # harness.tracker.Tracker
    repo_root: object                  # pathlib.Path to fixtures/repo
    calls: object = None               # harness.calls.CallRecord: runs/<run_id>.calls.jsonl
    harness_log: object = None         # pathlib.Path: runs/<run_id>.harness.log, for protocol errors
    switches: dict = field(default_factory=dict)   # harness.switches.current(), as the runner read them


def load_definition(module: str) -> dict:
    return json.loads((TOOLS_DIR / f"{module}.json").read_text())


class ToolRegistry:
    def __init__(self, ctx: RunContext, modules=MODULES):
        self.ctx = ctx
        self.hooks: list = []
        self._tools: dict[str, tuple[dict, object]] = {}
        for module in modules:
            definition = load_definition(module)
            self._tools[definition["name"]] = (definition, importlib.import_module(f"tools.{module}"))

    @property
    def schemas(self) -> list[dict]:
        """The definitions as the vendor gets them, in MODULES order, ready for
        ModelClient.create(tools=...)."""
        return [{k: d[k] for k in SENT if k in d} for d, _ in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def definition(self, name: str) -> dict:
        return self._tools[name][0]

    def call(self, name: str, tool_input: dict, plan_step=None) -> ToolResult:
        """Run one tool. Expected failures come back as is_error results; anything
        else raises, and your loop decides what that means (terminal_reason tool_error).

        `plan_step` is the id of the plan step the call runs for, from ctx.call_tool,
        or None when the model chose the call (the agent's loop)."""
        start = time.monotonic()
        entry = self._tools.get(name)
        if entry is None:
            return ToolResult(f"Error: no tool named {name!r}.", True, time.monotonic() - start)
        _, module = entry
        try:
            content = module.run(self.ctx, **(tool_input or {}))
            return ToolResult(content, False, time.monotonic() - start)
        except (ToolError, TypeError, ValueError) as exc:
            return ToolResult(f"Error: {exc}", True, time.monotonic() - start)


def default_tools(ctx: RunContext) -> ToolRegistry:
    return ToolRegistry(ctx)
