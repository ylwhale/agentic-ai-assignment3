# CHANGES.md

Three lines per assignment: what it added, what it removed, what it constrained.
Removals are logged like additions.

## Assignment 1
- **Added:** a bounded tool loop; four ways to triage an issue (a direct call, a workflow, an agent, and a hybrid of the two); a grounded success check that reads the tracker; a results script
- **Removed:** nothing
- **Constrained:** every agent run to three bounds checked in code (steps, tokens, wall clock); success to what the tracker holds for the run's ID

## Assignment 2
- **Added:** a plan written as data before the issue is read; a validator with four checks; an executor that runs only the plan's steps; one checkpoint that may revise the plan; a second pass before the comment; detectors and a diff over traces; a progress file and a compaction
- **Removed:** the model's choice of the next tool, in the planned way
- **Constrained:** every tool call to a step of a validated plan; revisions to steps not yet done; the comment to drafts that pass the second pass

## Assignment 3
- **Added:** five redesigned strict tool definitions, structured error messages, boundary validation, stable idempotency keys, readback verification, auditable write records, improved planned execution and search recovery, and regression tests.
- **Removed:** permissive starter tool definitions, ambiguous error responses, and unsafe assumptions that a timed-out write did not commit.
- **Constrained:** tool arguments through strict schemas and semantic validation; execution to approved plan steps; retries through stable keys and verification; PR creation before comment posting; and escalation when write outcomes cannot be confirmed.
