# ARCHITECTURE.md · RepoMind on the seven layers

For each layer: what does this job, and where is it enforced? Cite code as a file
path and line number joined by a colon, inside backticks. The checker opens every
reference (`checks/FORMATS.md`, section 8). A layer supported only by instructions
in `prompts/` is **EMPTY**, as is a layer without an implementation. Status is
`present`, `partial` (with the missing piece stated), or **EMPTY**.

The seven layers (Lecture 1, slides 42–44):

- **Model:** reasoning, choosing tools, generating text.
- **Harness:** running the loop, building prompts, and managing context and bounds.
- **Tools:** where the system reads from and acts on the world.
- **Memory & State:** what survives a step, a session, or a restart.
- **Control & Policy:** what the system may do and when to stop or ask.
- **Observability:** a step-by-step record of what happened.
- **Evaluation:** measuring task quality and system behavior.

The table describes the implementation after Assignment 3, including its strict
tool boundary, write recovery, planned execution, and search recovery. The code
references point to enforcement in Python, not just prompt instructions.

| Layer | What does this job | Where it is enforced | Status |
|---|---|---|---|
| Model | The configured model performs reasoning and generates plans, argument values, and text through the metered model client. The client records usage and cost. | `harness/client.py:120`, `harness/client.py:127` | present |
| Harness | The generic agent loop checks step, token, and wall-clock bounds in code. The planned workflow validates a plan before running only its approved steps; after an unsuccessful search, it can add a new source-search step through a validated revision. | `agent/loop.py:38`, `ways/planned.py:98`, `ways/planned.py:137`, `ways/planned.py:394`, `ways/planned.py:459` | present |
| Tools | Five renamed, strictly defined tools run through one registry. The registry validates arguments before dispatch, verifies result contracts, records calls, and gives writes stable idempotency keys with prechecks and readback recovery after timeouts. The tracker stores effects under the run ID. | `tools/__init__.py:73`, `tools/__init__.py:186`, `tools/__init__.py:237`, `tools/__init__.py:263`, `tools/__init__.py:357`, `harness/tracker.py:208` | present |
| Memory & State | The planned executor retains plan versions, step results, observed outputs, and the approved comment during a run. The notes variant writes a progress file and can compact older context, but does not automatically resume a new run from that file. | `ways/planned.py:120`, `ways/planned.py:124`, `ways/notes.py:67`, `ways/notes.py:82` | partial: per-run state and saved progress exist, but automatic cross-run restoration is not implemented |
| Control & Policy | Plan-schema checks and step enforcement prevent unapproved tool calls. The second pass approves or holds the comment before any write; the PR must precede the comment unless the approved file is `none`. Registry policy hooks fail closed, and unknown write outcomes escalate. | `ways/planned.py:80`, `ways/planned.py:144`, `ways/planned.py:487`, `ways/planned.py:526`, `tools/__init__.py:221`, `tools/__init__.py:263` | partial: enforced checks and escalation exist, but no interactive human-approval interface is implemented |
| Observability | The trace records model calls, offered tools, tool calls, plan versions, revisions, and terminal outcomes. An independent call record logs each tool attempt, including rejections, denials, keys, timeouts, and retries. | `harness/trace.py:57`, `harness/trace.py:79`, `harness/trace.py:97`, `harness/calls.py:47`, `tools/__init__.py:105` | partial: detailed traces and audit records exist, but automated monitoring and alerts are not implemented |
| Evaluation | The grounded checker compares the tracker-held comment with the expected label and file. The Assignment 3 checker tests strict definitions, malformed-call rejection, write idempotency, exit behavior, and trace consistency across evaluation arms. | `eval/check_triage.py:6`, `checks/check_03.py:521`, `checks/check_03.py:631`, `checks/check_03.py:685` | partial: offline checks and measured comparisons exist, but continuous production evaluation is not implemented |

**Empty layers: 0 of 7.** Model, Harness, and Tools are present; Memory & State,
Control & Policy, Observability, and Evaluation are partial as described above.
