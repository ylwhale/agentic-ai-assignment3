"""Regression for the issue-4192 documented-only search and weak plan revision.

No network access; no PR or comment is written.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.diff import step_args
from harness import ROOT
from harness.roles import tool_for
from tools import RunContext, default_tools
from tools.search_repo import run as search_repo
from ways import planned


def step(n, role, args, depends_on, status="pending"):
    return {"id": n, "tool": tool_for(role), "args_json": json.dumps(args),
            "depends_on": depends_on, "status": status,
            "action": role, "expected_result": "Results available"}


class StubTrace:
    def __init__(self):
        self.events = []
        self.plans = []
        self.endings = []

    def event(self, name, **kwargs):
        self.events.append((name, kwargs))

    def plan(self, version, p):
        self.plans.append((version, p))

    def end(self, reason, detail=""):
        self.endings.append((reason, detail))

    def model_call(self, *args, **kwargs):
        pass


class SourceSearchRecoveryTests(unittest.TestCase):
    def test_revised_plan_adds_an_on_plan_search_before_read_file(self):
        initial = {"meta": {"tools": [tool_for(r) for r in (
            "read_issue", "search", "read_file", "open_pr", "comment")]},
            "steps": [
                step(1, "read_issue", {"number": 4192}, []),
                step(2, "search", {"query": "$fill", "path_glob": None,
                                   "max_results": 10, "deep": False,
                                   "response_format": "concise"}, [1]),
                step(3, "read_file", {"path": "$fill", "response_format": "concise"}, [2]),
                step(4, "open_pr", {"number": 4192, "branch": "repomind/issue-4192",
                                     "title": "$fill", "body": "$fill"}, [3]),
                step(5, "comment", {"number": 4192, "body": "$fill"}, [4]),
            ]}
        trace = StubTrace()
        ctx = SimpleNamespace(issue_number=4192, trace=trace, client=None,
                              tools=default_tools(RunContext("recovery-test", None,
                                                             ROOT / "fixtures" / "repo")))
        executor = planned.Executor(ctx, initial)
        executor.ran.update(("1", "2"))
        executor.results = [
            {"id": 1, "tool": tool_for("read_issue"), "status": "done", "paths": []},
            {"id": 2, "tool": tool_for("search"), "status": "done",
             "paths": ["CHANGELOG.md", "README.md"], "count": 10},
        ]
        revision = copy.deepcopy(initial)
        for s in revision["steps"][:2]:
            s["status"] = "done"
        revision["steps"][2]["action"] = "Read the most relevant file found by the new search"
        with patch.object(planned, "revise_plan", return_value=revision):
            self.assertTrue(executor.checkpoint("after_first_search", False))
        steps = executor.plan["steps"]
        next_step = executor.next_step()
        self.assertEqual(next_step["tool"], tool_for("search"))
        self.assertEqual(step_args(next_step)["path_glob"], "src/**")
        self.assertEqual(step_args(next_step)["query"], "$fill")
        file_step = next(s for s in steps if s["tool"] == tool_for("read_file"))
        self.assertIn(next_step["id"], file_step["depends_on"])
        self.assertEqual([s["tool"] for s in steps[-2:]],
                         [tool_for("open_pr"), tool_for("comment")])
        self.assertEqual(trace.endings, [])
        self.assertTrue(any(t == "revision_recovery" for t, _ in trace.events))

        # Reproduce the same model-proposed search that was OFF-PLAN in the
        # original trace: now it is explicitly in the revised plan. Its result
        # must provide a real path for the next code_read_file step.
        ctx.client = SimpleNamespace(create=lambda **kw: SimpleNamespace(tool_calls=[
            SimpleNamespace(name=tool_for("search"), input={
                "query": "ExportButton", "path_glob": "src/**",
                "max_results": 10, "deep": False, "response_format": "concise",
            })
        ]))
        ctx.prompt = lambda name: "executor prompt"
        ctx.call_tool = lambda index, tool, args, plan_step: ctx.tools.call(
            tool, args, plan_step=plan_step)
        outcome = executor.run_step(next_step)
        self.assertEqual(outcome["status"], "done")
        self.assertIn("src/components/ExportButton.tsx", outcome["paths"])
        self.assertFalse(any(t == "off_plan" for t, _ in trace.events))

    def test_a_revised_failed_search_is_not_duplicated(self):
        old_search = step(2, "search", {"query": "Export", "path_glob": None,
                                        "max_results": 10, "deep": False,
                                        "response_format": "concise"}, [1], "failed")
        planned_read = step(3, "read_file", {"path": "$fill",
                                              "response_format": "concise"}, [2])
        active = {"steps": [step(1, "read_issue", {"number": 4192}, [], "done"),
                            old_search, planned_read]}
        ctx = SimpleNamespace(issue_number=4192, trace=StubTrace(), tools=SimpleNamespace(schemas=[]))
        executor = planned.Executor(ctx, active)
        executor.results = [{"id": 2, "tool": tool_for("search"),
                             "status": "failed", "paths": [], "count": 0}]
        executor.ran.add("2")
        revised = copy.deepcopy(active)
        revised["steps"][1]["args_json"] = json.dumps({
            "query": "ExportButton", "path_glob": "src/**", "max_results": 10,
            "deep": False, "response_format": "concise",
        })
        amended = executor.recover_source_search(revised)
        self.assertEqual(len(amended["steps"]), 3)
        self.assertEqual(amended["steps"][1]["id"], 2)
        self.assertFalse(ctx.trace.events)

    def test_focused_source_search_finds_export_button(self):
        ctx = SimpleNamespace(repo_root=ROOT / "fixtures" / "repo")
        result = json.loads(search_repo(ctx, "ExportButton", "src/**", 10, False, "concise"))
        paths = [r["path"] for r in result["results"]]
        self.assertIn("src/components/ExportButton.tsx", paths)
        self.assertTrue(all(p.startswith("src/") for p in paths))


if __name__ == "__main__":
    unittest.main()
