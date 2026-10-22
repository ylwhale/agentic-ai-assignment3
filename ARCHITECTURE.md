# ARCHITECTURE.md · RepoMind on the seven layers

For each layer: what does this job, and where is it enforced. Cite code as a file
path and line number joined by a colon, inside backticks; a checker opens every
reference (`checks/FORMATS.md`, section 8). A layer held only by a sentence in a
prompt (any file under `prompts/`) is **EMPTY**, and so is a layer nothing does
yet. Status is one of: present, partial (say what is missing), or **EMPTY**. Each
assignment brings this file up to date.

What each layer is for (Lecture 1, slides 42 to 44):

- **Model**: reasoning, choosing tools, generating text.
- **Harness**: runs the loop, builds each prompt, trims old history.
- **Tools**: where the system acts on the world.
- **Memory & State**: what survives a step, a session, a restart.
- **Control & Policy**: what the system may do, and when it must ask.
- **Observability**: a step-by-step record of what it did.
- **Evaluation**: measuring quality, before and after release.

The table below is RepoMind as Assignment 2 left it. In Assignment 3, the Tools row
comes to cite the boundary check and the key, and any other row your work changes
changes with it.

| Layer | What does this job | Where it is enforced | Status |
|---|---|---|---|
| Model | The configured model, called only through the metered client | `harness/client.py:120` | present |
| Harness | The loop, with three bounds checked in code; a plan validated before it runs, and an executor that runs only the plan's steps | `agent/loop.py:38`, `ways/planned.py:102`, `ways/planned.py:186` | present |
| Tools | Five tools behind one registry (open_pr arrived with Assignment 3's starter); every write is stamped with the run_id | `tools/__init__.py:104`, `harness/tracker.py:208` | partial: nothing checks a call before it runs, and a write that fails is a guess |
| Memory & State | A run's progress file, outside the model's context | `ways/notes.py:67` | partial: nothing survives a run |
| Control & Policy | The second pass holds a draft that still fails; nothing else decides which writes need a person | `ways/planned.py:272` | partial |
| Observability | One trace line per model call and per tool call, and the plan as data before it runs | `agent/loop.py:50`, `ways/planned.py:76` | partial: traces, no alerts |
| Evaluation | The grounded check reads the tracker, never the agent | `eval/check_triage.py:7` | partial: offline only |

**Empty layers: 0 of 7**, and five are partial.
