# RepoMind

The course's running system, built one layer at a time. RepoMind triages issues
for **Ledgerline**, a small reporting and export service: it reads an issue,
searches Ledgerline's private codebase, posts a triage comment where the issue's
author can read it, and, from Assignment 3, opens a draft pull request for the
file it names.

That combination is deliberate. RepoMind reads text anyone can write, reaches
code nobody outside the team should see, and writes somewhere public. Lecture 1
calls it the lethal trifecta. You build it this way on purpose, and Lecture 8 is
when you close it.

## Start here

You have two things: this repository, and the assignment handout from your
instructor. The handout says what to build and how it is graded; this file says
how the repository works. For Assignment 3, read the handout's "Getting started"
first.

This repository holds Assignment 2's working solution in place of your own code:
the planned way with its validator, boundary, checkpoint and second pass, the
detectors, the diff and the notes way. Your Assignment 2 repository stays as it
is. Assignment 3's five tool definitions are weak on purpose, and the registry
that runs them checks nothing, keys nothing and records nothing yet. A fresh copy
runs, and `make check-03` awards it nothing, or 2 points once `make ping` has sent
the starter's definitions to your vendor. That is expected.

### What you need

- Python 3.10 or later, `git` and `make`.
- The model and API key you used in Assignment 2. Never commit the key and never
  share it.

### Set up, and use your Assignment 2 model

```bash
make setup                     # creates .venv and installs the three dependencies
source .venv/bin/activate      # in every new terminal
make test                      # the harness's own tests; they spend no tokens
```

From your Assignment 2 repository, copy the `provider`, `model`, `base_url` and
`api_key_env` lines of `settings.yaml` into this one, and your model's line in
`pricing.yaml`, if this sheet does not have it. Change nothing else in
`settings.yaml`. Two backends cover almost every vendor:

| Your model | In `settings.yaml` |
|---|---|
| Claude, through Anthropic's API | `provider: anthropic`, `model`; the key in `ANTHROPIC_API_KEY` |
| Anything with an OpenAI-compatible API: OpenAI, Google Gemini, Mistral, DeepSeek, Groq, OpenRouter, a local Ollama or vLLM server, and many more | `provider: openai-compatible`, `model`, `base_url` (none for OpenAI itself), and `api_key_env`, the variable that holds your key (default `OPENAI_API_KEY`) |

Then check it:

```bash
export OPENAI_API_KEY=...      # or the variable your api_key_env names
make ping                      # your key, your model, tool calling, its price line, and your five definitions
```

`make ping` makes three tiny calls. The third sends your five tool definitions in
one request, exactly as your registry sends them, and records whether your vendor
accepted them in `runs/_ping.jsonl`. That is the live test of Part A: run it again
whenever you change a definition. `python -m harness.ping --backend
harness.scripted:ScriptedTriage` tests the same plumbing for free, but the checker
counts only a ping from your model. `make test` and `make ping` must both pass before
you write any code. A definition marked `"strict": true` goes to your vendor as its
strict mode, if it has one.

### Where to start: Assignment 3

| Order | What you write | How you test it | Costs |
|---|---|---|---|
| Part A | `tools/*.json`, `tools/*.py`, `tools/roles.yaml`, and the boundary check in `tools/__init__.py`; the new names in your prompts | `make audit`, free; `make ping`; then `python run.py --way planned --set dev` | A few calls |
| Part B | Errors in `tools/` and in the code that runs the model's calls | Your registry, called from Python, free | Nothing |
| Part C | The PR step, right before the comment, and the comment drafted before either write, in `ways/planned.py`; keys, the check before a write, the three exits, the call record, a hook and the two control switches in `tools/` | `ScriptedTriage`, free; then `FLAKY_WRITES=1 python run.py --way planned --set dev --runs 3`, and with `READBACK_FAIL=1` | Tokens |
| Part D | The surface's price and the tracker's counts, each a script in `eval/`; your predictions; the 24 measured runs | `make reset`, then both arms, then `make check-03` | Most of the spend |

`make check-03` works at any point and shows what is still missing; it is the same
checker staff run.

## What is here, and what is yours

| Path | What it is | Yours to edit? |
|---|---|---|
| `tools/*.json` | The five definitions: `read_issue`, `search_repo`, `read_file`, `post_comment` and the new `open_pr`, weak on purpose. Rename a tool inside its file; keep the file names | **Yes** |
| `tools/*.py`, `tools/__init__.py` | The five tools' code, and the registry that runs them | **Yes** |
| `tools/roles.yaml` | Which of your tools plays each role, and what it calls its arguments | **Yes**: keep it up to date as you rename |
| `tools/audit_schemas.py`, `tools/audit_limits.yaml` | The course's schema audit and the limits it holds you to (`make audit`) | No |
| `harness/` | The model client, tracker, trace writer, runner, prices, scripted models. New for Assignment 3: the tracker is a SQLite database that holds pull requests as well as comments, scopes every read to one run, accepts each idempotency key once, and can lie (`FLAKY_WRITES`, `READBACK_FAIL`); `harness/switches.py`; `harness/roles.py`; the call record, `harness/calls.py`; `make ping`'s third call; `UnknownToolCaller` in `harness/scripted.py`; and `tools` on every `model_call` line, the tools that call carried | No |
| `fixtures/issues/`, `fixtures/repo/`, `fixtures/answers.yaml` | The same six evaluation and two development issues, Ledgerline's codebase, and the answers. Three files with near-miss names are new: `src/reports.ts`, `src/Report.ts`, `src/report.tsx` | **Never** |
| `fixtures/search_index/` | The prebuilt index the search tool reads, with the time it was built | **Never** |
| `fixtures/plans/`, `fixtures/traces/` | Assignment 2's plans and traces | **Never** |
| `checks/` | `check_03.py`, the Assignment 3 checker, and `FORMATS.md`, which defines every format it reads (section 11 is new) | No |
| `tests/` | The harness's own tests. They find your tools by role, so they keep passing after you rename | No |
| `settings.yaml`, `pricing.yaml` | Your model, the ceilings, the dated price sheet | Once, in setup: your model and its price line; never the ceilings |
| `run.py` | Runs a way over a set of issues, one trace per run | No |
| `agent/`, `ways/`, `prompts/` | Assignment 2's working solution, whose planned way finds each tool by its role in `tools/roles.yaml`, so a renamed tool still runs; and Assignment 1's ways, loop and prompts. The direct and workflow ways still name the starter's tools, and this assignment does not run them | **Yes**: Part A changes the prompts, Part C the planned way |
| `eval/` | The grounded check, Assignment 1's and 2's scripts; your Part D counting script | **Yes** |
| `reports/`, `ARCHITECTURE.md`, `CHANGES.md` | Your write-ups; `reports/` starts with the templates for `LAB_03.md` and `LAB_03.json` | **Yes** |
| `runs/`, `tracker/` | Made by your first run: traces, call records, the run ledger, the tracker's database | Written by the harness; **commit them** |

`checks/FORMATS.md`, section 11, fixes every format: the definition files, the
roles, the registry's interface (the hooks and how the plan step reaches it), the
call record, the tracker's tables, `make ping`'s record, and each field of
`reports/LAB_03.json`.

## Running

```bash
make audit                                               # Part A's audit of tools/*.json; free
make ping                                                # your model, and your five definitions in one request
python run.py --way planned --set dev                    # your planned way on the two dev issues
python run.py --way planned --backend harness.scripted:ScriptedTriage --set dev   # the plumbing; free
FLAKY_WRITES=1 python run.py --way planned --set dev --runs 3                     # Part C: writes that time out
FLAKY_WRITES=1 READBACK_FAIL=1 python run.py --way planned --set dev --runs 3     # Part C: the third exit
FLAKY_WRITES=1 READBACK_FAIL=1 python run.py --way planned --backend harness.scripted:ScriptedTriage --set dev --runs 3   # free

make reset                                               # before Part D: archive the tracker, set earlier eval runs aside
FLAKY_WRITES=1 python run.py --way planned --set eval --runs 2                    # Part D: your harness, 12 runs
FLAKY_WRITES=1 KEY_MODE=uuid VERIFY=off \
  python run.py --way planned --set eval --runs 2 --start-sweep 3                 # Part D: the control, 12 runs
make check-03                                            # the Assignment 3 checker
```

The four switches are environment variables, set for one command
(`harness/switches.py`): `FLAKY_WRITES` and `READBACK_FAIL` make the tracker lie;
`KEY_MODE` and `VERIFY` turn your harness into Part D's naive control, and only your
registry obeys them. Leave all four unset for ordinary runs.

Each run writes `runs/<run_id>.jsonl`, its call record
`runs/<run_id>.calls.jsonl`, and, when it has a protocol error to report,
`runs/<run_id>.harness.log`, and appends a line to `runs/_ledger.jsonl`. The runner
stamps a fresh `run_id` on every run, and the tracker stamps it on every comment and
PR that run writes, so each run is checked on its own writes and nothing else.
Every run records `lab: "03"` and the four switches in its first line, and
`make check-03` reads only this assignment's runs, sorted into Part D's two arms by
their switches. The scripted models cost nothing and are left out of the checker's
results. On a free tier, or if you hit rate limits, add `--jobs 1`. `python run.py
--help` lists every option.

## On a free tier: splitting the work over days

A free tier limits requests a day as well as a minute. Running the same command
again resumes it: on the evaluation issues it skips the runs that configuration
(way, `--critique`, `--notes`, the four switches, model) has already finished, so
Part D's two arms can run on different days. When your vendor's daily quota is
spent, the run it interrupted ends `quota`, which never counts as finished, and the
sweep stops; run the same command again when it resets. A per-minute limit, or an
overloaded model, is waited out as long as the vendor asks.

Every arm's runs must come from one version of your code. If you change the code
after some runs, discard them with the same switches set:
`FLAKY_WRITES=1 python run.py --discard --way planned --set eval` moves your
harness arm's evaluation runs, with their call records, to `runs/discarded/`, and
their rows in the tracker to `runs/discarded/tracker_rows.jsonl`; their tokens stay
in the ledger. Then run it again. `make reset` sets every earlier evaluation run of
this assignment aside the same way, whatever its switches, and archives the
tracker's database.

## The rules the harness enforces

- **Success is read from the tracker, never from the agent.** And Part D's counts
  of writes are read from the tracker's database, never from your traces.
- **Every read of the tracker is scoped to one run**, and every write is stamped
  with it.
- **Every run has a hard ceiling** (`session_token_ceiling`), and **every
  assignment has one** (`lab_token_ceiling`). The runner stops starting runs at the
  second. Ask before you pass `--over-budget`.
- **The plan is logged before anything runs**, as in Assignment 2.
- **Every number carries its run count and its spread.**
- **Prompts live in `prompts/`**, and a layer held only by a prompt is empty.
- **Disclose AI assistance** in each report: which tool, what it wrote, what you
  checked.

## Submitting an assignment

Your copy of this repository is private. When you create it, add the course
staff's GitHub account as a collaborator (**Settings**, then **Collaborators**)
and post its URL on the assignment in Canvas: that is how staff find and read
your work.

Run `make check-03` first; it is the same checker staff run. Commit everything,
including `runs/` and `tracker/` (the checker reads both), then tag the commit
and push both the commit and the tag. A tag you have not pushed has not been
submitted.

```bash
make check-03
git add -A
git commit -m "Assignment 3"
git tag lab-03
git push
git push origin lab-03
```
