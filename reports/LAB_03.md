# Assignment 3

The purpose of this assignment was to make RepoMind's tool calls easier for a model to select correctly, more informative when they fail, and safe when a write times out after possibly committing. I redesigned five tool definitions, added strict validation and policy enforcement at the tool boundary, and implemented idempotency and readback-based write recovery.

The Part D comparison uses the latest 24 evaluation runs recorded in `runs/` and `tracker/tracker.db`: 12 harness runs and 12 control runs. During development, I also improved executor argument handling, PR consistency checks, and search recovery. Those changes were tested through unit tests, schema audits, scripted executions, and live Gemini development runs.

The final automated checker reported **82/82 points**, with no integrity flags. The remaining report sections explain the implementation decisions, measured behavior, and limitations.

---

## Part A — The five definitions

### Strict-mode audit and vendor acceptance

The redesigned interface contains five tools:

1. `tracker_read_issue`
2. `code_search_repo`
3. `code_read_file`
4. `tracker_post_comment`
5. `pulls_open_pr`

The schema audit reported:

- Five strict tool definitions.
- Zero optional parameters.
- One nullable or union-typed parameter.
- Namespaces represented through service-specific prefixes.
- No schema-audit errors.

All input object properties are required, and each input object specifies `additionalProperties: false`. The nullable parameter is `path_glob` in `code_search_repo`, which accepts either a string or `null`.

Constraints that may not be consistently supported by vendor strict-schema subsets, including length, numeric range, path safety, and content requirements, are enforced at the tool boundary.

I tested the definitions using `gemini-3.5-flash-lite` through Google's OpenAI-compatible endpoint. The recorded `make ping` response in `runs/_ping.jsonl` showed `accepted: true`, `error: null`, and `strict: true` for all five definitions. Therefore, the `compiles` result is `true`, based on an actual vendor response rather than the local audit alone.

### Consolidation decision

I retained the five separate tool roles instead of combining them. Search and file reading require evidence-dependent choices, while opening a draft PR and posting a public comment have different side effects and recovery requirements.

### Tool naming, descriptions, and examples

The names use prefixes such as `tracker_`, `code_`, and `pulls_` to make their responsibilities more distinguishable.

The `tools/roles.yaml` file maps these implementations to the five logical assignment roles.

Each redesigned definition describes:

- When the tool should be used.
- Required arguments and their meanings.
- Typical and more difficult usage examples.
- Meaningful empty results.
- Recoverable and nonrecoverable errors.
- Result limits and response formats.
- Side effects and relevant safety constraints.

The examples are incorporated into the vendor-facing descriptions by `ToolRegistry.schemas`.

### `search_repo`: before and after

| Before: starter interface | After: redesigned interface |
|---|---|
| Name: `search_repo` | Name: `code_search_repo` |
| Description: "Search the code." | Describes a literal, case-insensitive repository search with structured JSON output. |
| `query`: loosely described string | Required nonempty literal string, at most 200 characters. |
| Optional `filter` with unclear syntax | Required nullable `path_glob`, using a repository-relative glob or `null`. |
| Optional `max_results` | Required integer, validated between 1 and 20. |
| Optional `deep` | Required boolean distinguishing indexed and deeper filesystem search. |
| No response-format selection | Required `response_format`, either `concise` or `detailed`. |
| Limited guidance for empty results | Explicitly distinguishes valid zero-match results from invalid queries. |
| No strict schema | Strict schema with no unexpected properties. |

The redesigned search output includes fields such as:

- `query`
- `query_valid`
- `count`
- `results`
- `truncated`
- `index_age`
- `source`
- `response_format`

For example, a successful search with no matches returns `query_valid: true`, `count: 0`, and an empty `results` array. This is different from an invalid query or tool failure.

The other four tools received similar improvements.

`code_read_file` uses exact, case-sensitive paths and supports concise and detailed responses with different content limits. `tracker_read_issue` identifies the requested issue and returns bounded text. `tracker_post_comment` requires a valid issue number and a comment beginning with `label:` and `file:` headers. `pulls_open_pr` requires a valid issue-specific branch, title, and body identifying the proposed source file.

The registry validates arguments before dispatch and checks returned tool results against the promised output contract.

Strict schemas reduce malformed tool calls, but they cannot guarantee that a syntactically valid query is useful or that a final triage answer is factually correct.

---

## Part B — Errors a model can repair from

### Error classification

I classified failures as transient, permanent, or ambiguous.

A **transient failure** may be retried when the temporary condition is resolved.

A **permanent failure** generally requires changing the arguments or target rather than repeating the same call.

An **ambiguous failure** occurs when a write may already have committed, but its response does not confirm the outcome.

| Tool role | Transient failure | Permanent failure | Ambiguous failure |
|---|---|---|---|
| `read_issue` | Temporary issue-store unavailability | Invalid issue number, nonexistent issue, malformed arguments | Not applicable to a read |
| `search` | Temporary index or repository I/O failure | Invalid query, malformed glob, out-of-range result limit, incorrect argument types | Not applicable to a read |
| `read_file` | Temporary filesystem read failure | Missing path, wrong case, repository escape, excluded directory, invalid response format | Not applicable to a read |
| `comment` | Timeout followed by verified absence of the comment | Invalid issue, malformed headers, empty body, policy denial | Timeout after a comment may already have been posted |
| `open_pr` | Timeout followed by verified absence of the PR | Invalid issue, wrong branch, missing title/body, policy denial | Timeout after PR creation may already have committed |

The important distinction is between a timeout whose effect is known to be absent and a timeout whose outcome remains unknown.

Retrying an ambiguous write without verification can create duplicate persistent records.

### Protocol-error policy

The selected protocol-error policy is `end_run`.

An unknown tool name or malformed tool-protocol message is treated differently from a recognized tool receiving invalid arguments.

For protocol violations, `ToolRegistry._protocol` records the problem and raises:

```python
ProtocolError("Invalid tool protocol; run must stop")
```

This prevents the agent from treating an invalid tool message as an ordinary successful tool result.

By comparison, a recognized tool with malformed arguments can return `is_error: true` and a repairable explanation. The executor can then correct the arguments within its authorized plan.

This separation avoids exposing unnecessary protocol internals to the model while still providing useful feedback for legitimate argument errors.

### Exact near-miss error

I tested `code_read_file` using:

```json
{
  "path": "src/report.ts",
  "response_format": "concise"
}
```

The tool returned `is_error: true` with this message:

```text
Error: File path 'src/report.ts' does not exist with that exact spelling. Similar existing paths: src/Report.ts, src/reports.ts, src/report.tsx, src/api/reports.ts, src/export/csv.ts. Retry with one of the exact paths, including case.
```

The error identifies the failed argument, explains why the call failed, and provides existing alternatives.

The path validation checks filenames against an inventory of repository paths instead of depending entirely on the operating system's path lookup.

This matters on macOS because case-insensitive filesystems may otherwise treat `src/report.ts` and `src/Report.ts` as equivalent, even when the repository's intended paths are different.

The redesigned error allows the model to select a real path without guessing or silently reading an unintended file.

---

## Part C — Writes that may already have happened

### Stable idempotency keys

The registry derives an idempotency key from the run identifier, the planned step identifier, and the frozen tool arguments.

The formula is:

```python
canonical = json.dumps(
    {
        "run_id": run_id,
        "step_id": plan_step,
        "arguments": args
    },
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False
)

key = hashlib.sha256(
    canonical.encode("utf-8")
).hexdigest()
```

Sorting JSON keys and using a canonical representation prevents irrelevant serialization differences from changing the key.

The model does not choose or regenerate the key.

The same logical write within the same run and plan step receives the same key, allowing the tracker to recognize repeated attempts.

The tracker enforces uniqueness for stored idempotency keys.

Before writing, the registry also checks for existing effects associated with the current run.

For a draft PR, the issue number and stable branch provide an additional way to identify an existing effect.

For a comment, the idempotency key is especially important because comments do not have an equivalent natural branch identifier.

### Three write outcomes

A write timeout does not establish whether the operation succeeded.

The harness distinguishes three outcomes:

**1. Committed and verified**

The write may have committed before the response timed out. If readback finds the corresponding effect, the registry returns its confirmation instead of attempting another write.

**2. Not committed and verified absent**

If readback confirms that the effect does not exist, the registry may retry the same frozen arguments with the same idempotency key.

Retries are bounded, with a maximum of three write attempts. Policy checks remain active for each attempt.

**3. Outcome remains unknown**

If verification itself fails, the registry cannot safely conclude whether the write happened.

In that case, the run escalates rather than risking a duplicate side effect.

These outcomes make the retry decision depend on evidence rather than assuming every timeout represents failure.

### Control-arm behavior

The control configuration uses:

```text
KEY_MODE=uuid
VERIFY=off
```

Under this configuration, retry attempts receive fresh UUID keys, and the harness skips post-timeout verification.

Consequently, a comment that committed before a timeout may be posted again because a new key no longer identifies the previous write.

The control retains the pre-attempt checks, so draft PR duplication may still be avoided through the PR's stable branch.

This explains why the experiment can observe duplicate comments without necessarily observing duplicate PRs.

### PR-before-comment ordering

The planned workflow requires the final `pulls_open_pr` step to occur immediately before `tracker_post_comment`.

The executor first prepares the proposed comment and performs the second-pass critique before either write.

The approved comment must begin with a valid issue label and source-file header.

If the approved file is `none`, the executor records `pr_skipped` and proceeds directly to posting the comment.

If the approved draft names a source file, the PR must be successfully opened or verified before the comment is posted.

The PR description must reference the same file as the approved triage comment.

If PR creation fails or its outcome remains unknown, the executor does not post the comment.

This ordering is intentional: a draft PR can be inspected and revised, whereas publishing a misleading public comment is a more visible side effect.

### Actual write call-record example from the final evaluation

The following complete, single-line call record comes from `runs/planned-4201-s01-a4c55d.calls.jsonl` (the harness arm's issue 4201 run):

```json
{"run_id":"planned-4201-s01-a4c55d","t":6.242,"tool":"pulls_open_pr","role":"open_pr","args":{"body":"Proposed change in src/notifications/templates.ts: update BASE_URL to read from the application configuration (`config`) instead of process.env.PUBLIC_URL so that password reset email links point to the correct production domain.","number":4201,"branch":"repomind/issue-4201","title":"Fix issue 4201"},"plan_step":4,"key":"71efea03a95fb5e357ffa82b2a7745e7ffeaaf2fd932d5844df6383790dc8dc7","attempt":1,"outcome":"timeout","is_error":true,"result":"Write timed out: the tracker did not answer in time","latency_s":0.003,"cost_usd":0.0}
```

The final evaluation run `planned-4201-s01-a4c55d` provides a concrete example of ambiguous write outcomes. The first `pulls_open_pr` attempt returned a timeout, even though the tracker recorded the PR operation as `timeout_after_commit`. The subsequent `tracker_post_comment` operation also returned timeouts on attempts 1 and 2. Both comment attempts reused the same idempotency key, `ecf49d58137e16f71b3d6a900312e54627666cc5ebd816c37485eef9090ecac9`, while the PR used its separate stable key, `71efea03a95fb5e357ffa82b2a7745e7ffeaaf2fd932d5844df6383790dc8dc7`. This demonstrates that retries preserved the identity of each logical write rather than generating new identifiers.

I independently verified the final effects using `tracker/tracker.db`. The database contained exactly **one open PR** on branch `repomind/issue-4201` and **one posted comment**, with both stored idempotency keys matching their corresponding call records. No duplicate PR or comment was created. Although timeout responses alone cannot prove whether a particular write attempt committed, the tracker records establish that the final effects existed exactly once. This evidence supports the effectiveness of stable idempotency keys and readback verification in preventing duplicate writes under ambiguous timeout conditions.

---

## Part D — Measure

### 1. The price of the tool surface

The redesigned schemas contain substantially more descriptions, examples, and explicit contracts than the starter definitions.

To estimate the additional context cost, I measured the difference in input tokens between a fixed one-message model request with and without each definition.

The measurements used `gemini-3.5-flash-lite` and the vendor-facing schema descriptions.

The uncached input rate recorded in `pricing.yaml` was **$0.30 per million tokens**.

| Definition | Starter tokens | Redesigned tokens |
|---|---:|---:|
| `read_issue` / `tracker_read_issue` | 51 | 286 |
| `search_repo` / `code_search_repo` | 114 | 578 |
| `read_file` / `code_read_file` | 48 | 458 |
| `post_comment` / `tracker_post_comment` | 70 | 531 |
| `open_pr` / `pulls_open_pr` | 103 | 683 |
| **All five in one request** | **386** | **2,536** |
| Planner's textual tool list | 116 | 1,555 |

The `submit_plan` definition measured 327 tokens and did not change, so I excluded it from the before-and-after difference.

The redesigned five-tool surface adds:

```text
2,536 - 386 = 2,150 tokens
```

This is approximately 6.57 times the starter schema cost when all five tools are supplied together.

The planner's textual tool list increases by:

```text
1,555 - 116 = 1,439 tokens
```

These additional tokens pay for stronger field descriptions, two examples per tool, clearer empty-result semantics, error contracts, response caps, and warnings about persistent side effects.

The increased context cost creates a trade-off: better-defined interfaces can reduce ambiguity, but excessively long definitions also consume model context.

### Per-run surface-cost comparison using the final evaluation

The final harness evaluation contained **12 runs and 67 model calls**, averaging **5.583 calls per run**, with a range of **4–8 calls per run**.

The recorded call distribution was:

| Model-call purpose | Number of calls |
|---|---:|
| Initial planning | 12 |
| Plan revision | 4 |
| `code_search_repo` argument filling | 17 |
| `code_read_file` argument filling | 12 |
| `tracker_post_comment` argument filling | 12 |
| `pulls_open_pr` argument filling | 10 |
| **Total** | **67** |

The executor supplied the issue number directly, so no `tracker_read_issue` model fill was required. Initial planning requests included the textual five-tool list, while plan revisions used the `submit_plan` schema without repeating that list.

#### Measurement method

I estimated tool-surface input cost using the standalone schema-token measurements from Part D, Section 1, together with the actual tool offerings recorded in the final evaluation traces.

Each model call was charged for the tool definition it received. Planning calls were charged **327 tokens** for `submit_plan`, and the 12 initial planning calls were also charged for their textual tool list.

The configured uncached input rate was **$0.30 per million tokens**.

The cost formula was:

`Surface cost = surface input tokens × $0.30 / 1,000,000`

This calculation measures only tool definitions and the planner's tool-list text. It excludes other prompt content, model output, and cache effects. Therefore, **tool-surface cost must not be confused with the actual total cost of running the model**.

#### Final evaluation cost comparison

| Surface configuration | Mean input tokens/run | Uncached USD/run |
|---|---:|---:|
| Starter definitions, using the final call pattern | 917.33 | **$0.00027520** |
| Redesigned definitions, selectively supplied | 4,368.00 | **$0.00131040** |
| All five redesigned task-tool schemas on every call, excluding planning overhead | 14,159.33 | **$0.00424780** |
| All five redesigned task-tool schemas on every call, retaining equivalent planning overhead | 16,150.33 | **$0.00484510** |

The first two scenarios use the same observed 67-call pattern, allowing the effect of changing the definitions to be estimated while holding the tool-call distribution constant.

The third scenario measures the cost of exposing all five redesigned task-tool schemas on every model call. It deliberately excludes the separate planning overhead, making it a schema-only reference rather than a complete alternative execution configuration.

The fourth scenario adds that planning overhead back, providing a more directly comparable counterfactual.

#### Calculation for the starter definitions

Using the final recorded call distribution:

`16 × 327 + 12 × 116 + 17 × 114 + 12 × 48 + 12 × 70 + 10 × 103 = 11,008 tokens`

Across 12 runs:

`11,008 / 12 = 917.33 tokens per run`

Estimated uncached cost:

`917.33 × $0.30 / 1,000,000 = $0.00027520 per run`

This is a counterfactual estimate applying the starter definitions to the final workload, not an independently executed starter-model experiment.

#### Calculation for the redesigned definitions

The redesigned surface used:

- 16 `submit_plan` schema offers: `16 × 327 = 5,232` tokens.
- 12 initial planner tool lists: `12 × 1,555 = 18,660` tokens.
- 17 search schemas: `17 × 578 = 9,826` tokens.
- 12 file-read schemas: `12 × 458 = 5,496` tokens.
- 12 comment schemas: `12 × 531 = 6,372` tokens.
- 10 PR schemas: `10 × 683 = 6,830` tokens.

The total was:

`5,232 + 18,660 + 9,826 + 5,496 + 6,372 + 6,830 = 52,416 tokens`

Across 12 runs:

`52,416 / 12 = 4,368.00 tokens per run`

Estimated uncached cost:

`4,368.00 × $0.30 / 1,000,000 = $0.00131040 per run`

The redesigned surface required more input tokens than the starter definitions because it included stronger parameter explanations, usage examples, error contracts, and warnings about persistent side effects.

These additional tokens represent an intentional trade-off for a more explicit tool interface.

#### Comparison with exposing all five tools

If all five redesigned task-tool schemas were supplied on every one of the 67 model calls, their combined schema cost would be:

`67 × 2,536 = 169,912 tokens`

Across 12 runs:

`169,912 / 12 = 14,159.33 tokens per run`

Estimated uncached cost:

`14,159.33 × $0.30 / 1,000,000 = $0.00424780 per run`

However, this reference does not include the `submit_plan` schema or textual planning list that the selective execution configuration actually required.

To compare two configurations retaining the same planning functionality, I also calculated the all-five scenario with that overhead preserved:

`169,912 + (16 × 327) + (12 × 1,555) = 193,804 tokens`

Across 12 runs:

`193,804 / 12 = 16,150.33 tokens per run`

Estimated uncached cost:

`16,150.33 × $0.30 / 1,000,000 = $0.00484510 per run`

Compared with the selectively supplied redesigned surface, this equivalent-overhead counterfactual has a difference of:

`16,150.33 − 4,368.00 = 11,782.33 tokens per run`

Its corresponding estimated cost difference is:

`$0.00484510 − $0.00131040 = $0.00353470 per run`

This represents an approximately **73.0% reduction in estimated tool-surface input tokens** relative to that all-five-tools counterfactual.

The calculation illustrates the potential context savings from restricting tool availability to the current plan step. It does not establish an observed reduction in total API spending, because changing which tools are available may also change the model's behavior, the number of calls, and the overall prompt and response lengths.

#### Concrete application: longest final harness run

To connect the cost model to an actual execution, I examined the longest final harness run, `planned-4206-s01-f01ba1`.

The run made **eight model calls**:

| Call purpose | Count |
|---|---:|
| Initial plan | 1 |
| Plan revision | 1 |
| Search argument filling | 3 |
| File-read argument filling | 1 |
| Comment argument filling | 1 |
| PR argument filling | 1 |
| **Total** | **8** |

Its initial search returned no matches, so the executor needed a revised plan and additional focused searches before proceeding to a source-file read.

The trace recorded these actual model-usage measurements:

| Actual run metric | Value |
|---|---:|
| Prompt tokens | 18,530 |
| Output tokens | 1,319 |
| Total model tokens | **19,849** |
| Actual model cost | **$0.008857** |

I then applied the measured schema-token costs to this run's eight-call pattern.

For the selectively supplied redesigned surface:

`2 × 327 + 1,555 + 3 × 578 + 458 + 531 + 683 = 5,615 tokens`

Estimated uncached surface cost:

`5,615 × $0.30 / 1,000,000 = $0.00168450`

For the all-five task-tool schema reference, excluding planning overhead:

`8 × 2,536 = 20,288 tokens`

Estimated uncached surface cost:

`20,288 × $0.30 / 1,000,000 = $0.00608640`

When the same planning overhead is retained:

`20,288 + (2 × 327) + 1,555 = 22,497 tokens`

Estimated uncached surface cost:

`22,497 × $0.30 / 1,000,000 = $0.00674910`

The comparable difference between the equivalent-overhead configurations is:

`22,497 − 5,615 = 16,882 input tokens`

At the stated input rate, this equals:

`16,882 × $0.30 / 1,000,000 = $0.00506460`

These figures are **counterfactual tool-surface estimates**, not observed differences in total model cost. The actual run cost of $0.008857 includes model processing beyond the tool definitions, including other prompt content and output generation. It should not be interpreted as the cost of the tool surface alone.

Importantly, this run completed its write workflow but **failed the grounded triage check**. The additional search steps and greater model expenditure did not guarantee that the agent selected the correct source file. This illustrates that tool-use cost, workflow completion, and semantic correctness must be evaluated separately.

#### Overall interpretation

Across the final 12 harness runs, total model tokens ranged from **9,240 to 19,849**, and actual total run costs ranged from **$0.003997 to $0.008857**, with a mean of **$0.005791** and a combined cost of **$0.069492**.

The surface-token measurements show that redesigned strict definitions are more expensive than the original permissive definitions. However, selectively exposing tools according to the execution plan can substantially reduce the context cost compared with presenting every tool on every call.

These results do not prove that a larger tool surface causes more failures or that selective exposure directly reduces the observed API bill. They quantify the input-token overhead under explicitly stated assumptions.

The first three surface-cost values are recorded in `reports/LAB_03.json` using the final 67-call profile. The additional equivalent-overhead comparison is calculated from the same verified tool-offering counts to make the hypothetical comparison more consistent.

### 2. Tracker-grounded comparison

I evaluated six issues twice per configuration, producing 12 harness runs and 12 control runs.

Both configurations used `FLAKY_WRITES=1` and the same planned workflow.

The harness configuration used stable derived keys and readback verification:

```text
KEY_MODE=derived
VERIFY=on
```

The control configuration used fresh UUID keys and disabled verification:

```text
KEY_MODE=uuid
VERIFY=off
```

The latest checker compared these runs against the tracker database and reported the following measurements:

| Measure | Harness (12 runs) | Control (12 runs) |
|---|---:|---:|
| Maximum comments per run | 1 | 3 |
| Runs with duplicate comments | 0 | 4 |
| Runs satisfying the conditional PR rule | 12/12 | 12/12 |
| Committed write timeouts | 18 | 10 |
| Escalated runs | 0 | 2 |
| Grounded triage passes | 7/12 (58.3%) | 3/12 (25.0%) |

The **comment-count distribution** provides the spread behind the duplicate totals: all 12 harness runs left exactly one comment; among control runs, one left zero comments, seven left one, one left two, and three left three. Model calls ranged from 4 to 8 per run in both arms (67 calls for the harness; 66 for the control). Actual full-run costs ranged from $0.003997 to $0.008857 in the harness and from $0.004046 to $0.009441 in the control. These are total model costs, not isolated note, schema, or readback costs.

The most important result is the difference in duplicate comments.

The harness produced **zero duplicate-comment runs**, despite recording 18 committed write timeouts.

The control produced duplicate comments in **four of its twelve runs**, with a maximum of three comments in one run.

This is consistent with the expected behavior of stable idempotency keys and readback verification. When an earlier comment has already committed, the harness can identify the effect and avoid issuing another write.

In the control configuration, retries may receive different UUID keys, making it possible to repeat a write whose response previously timed out.

The committed-timeout counts represent write attempts or events, not distinct runs. Therefore, 18 committed timeouts across 12 harness runs is possible.

Both arms satisfied the conditional PR requirement in all 12 runs. This does not necessarily mean that each arm created 12 PRs. Instead, the checker confirmed that PR behavior was consistent with the approved triage file: open a PR when a source file is named, and skip PR creation when the file is `none`.

The harness also completed with zero escalations and seven grounded triage passes. The control had two escalations and three grounded passes.

The grounded pass rate increased from 25.0% in the control to 58.3% in the harness, a difference of 33.3 percentage points.

These results support the usefulness of reliable write handling, although the experiment is relatively small and live model behavior can vary between runs. The two configurations differ primarily in write recovery, but this comparison alone should not be interpreted as proof that write verification caused every difference in final triage correctness.

The distinction between workflow completion and semantic correctness remains important. Even though all 12 harness runs avoided escalation, only seven passed the grounded triage check.

Reliable side effects do not automatically guarantee a correct label or source file.

### 3. The four shapes of tool misuse

The tool redesign addresses four different misuse patterns: choosing the correct tool with incorrect arguments, choosing the wrong tool, misinterpreting a result, and failing to make a required call.

The redesigned interface reduces several of these risks, but no single schema change eliminates all four.

**Final-trace counts and evidence boundary.** Across the latest 24 measured runs there were **133 model calls** (67 harness, 66 control). All 133 model-call records contained a tool call; none produced a text-only/no-tool response. The call records contained **zero `rejected`, `denied`, or `protocol` attempts**, and neither arm recorded an `off_plan` event. These are counts of **observable boundary/protocol violations**, not proof that every valid search was semantically useful. The development examples below explain failure mechanisms that the code was changed to handle, but they must not be read as measured frequencies from the final 24 runs.

| Misuse category | Mitigation | Evidence and remaining limitation |
|---|---|---|
| **Right tool, wrong arguments** | Strict required fields, type checking, semantic validation, and repairable error messages | Earlier development runs exposed missing issue numbers, missing search fields, unsupported argument names, and unresolved placeholders. These were rejected or corrected before execution. |
| **Wrong tool, valid call** | Service-specific names, step-specific tool exposure, and off-plan refusal | During development, a file-read step received two `code_search_repo` calls. Both were recorded as off-plan and were not executed. |
| **Correct call, misread result** | Structured results distinguishing valid empty searches, truncation, and invalid requests | An empty result is explicitly identified as a valid search with zero matches. This improves interpretability but cannot prove that the model correctly reasons about the result. |
| **No call at all** | Plan validation, checkpoints, recovery steps, and escalation when required actions are missing | An earlier plan revision failed to insert a needed search, leaving the executor without a grounded file path. Search-recovery logic was subsequently improved. |

#### Right tool, wrong arguments

During development, some plans supplied empty argument objects or omitted required fields.

For example, `tracker_read_issue` could receive `{}` instead of an object containing the issue number. Other calls used unsupported names such as `issue_number` instead of `number`.

The executor was updated to identify missing required arguments, supply deterministic values where appropriate, and request model-generated values when evidence was needed.

The registry's strict validation remained active.

This separation matters because argument repair should not weaken the final tool boundary.

#### Wrong tool, valid call

An earlier development run, `planned-4192-s01-f04ae1`, demonstrated a different failure.

Step 3 required `code_read_file`, but the model attempted `code_search_repo` twice.

Both search requests were valid in isolation. However, they were invalid for the currently authorized plan step.

The executor correctly recorded the requests as `off_plan` and refused to execute them.

Investigation showed that the preceding search had not identified a useful source file. A plan revision changed the description of the completed search but failed to add an executable recovery search.

I improved the recovery logic so an unsuccessful search could lead to a new search step in the recorded plan before the file-read step resumed.

The new regression tests covered this behavior. Later development runs completed without off-plan calls.

This example illustrates that valid tool arguments are not sufficient: tool use must also satisfy the current plan and execution state.

#### Correct call, misread result

The redesigned search response distinguishes zero matches from invalid input.

For example, an earlier search for `Ledgerline export` returned:

```json
{
  "query_valid": true,
  "count": 0,
  "results": []
}
```

The tool result clearly indicates that the request was valid but produced no matching results.

However, a structured response only makes the distinction observable. It does not guarantee that the agent interprets the evidence correctly.

A zero-match result may require a broader query, a different identifier, or a revised search strategy.

Likewise, a truncated result should not be treated as the complete repository.

The grounded triage checker remains necessary because syntactically valid tool calls and well-structured results do not establish that the final answer is correct.

#### No call at all

The **no-call** failure category describes situations in which an agent fails to invoke a tool when an action is required. However, a model producing no tool call and an agent omitting a necessary downstream action are related but distinct failure modes. They should not be counted interchangeably.

During development, I observed a missing-action problem in the search-recovery workflow. After an initial search failed to identify a relevant source file, the revised plan did not add the additional search needed to continue. The model subsequently attempted `code_search_repo` while the current plan required `code_read_file`. The executor rejected these off-plan calls and escalated. This was a **plan-revision and missing-required-action failure**, not a literal no-call response, because the model did attempt tool calls.

In the final 24-run evaluation, all **133 model calls contained at least one tool call**, so no literal no-call responses were observed. For example, control run `planned-4204-s03-cd407f` escalated after executing on-plan searches and encountering an invalid follow-up plan revision. This should be classified as a plan-revision failure rather than a no-call event.

The harness completed all 12 runs without escalation, but only **7 of 12 runs passed the grounded triage check**. This demonstrates that completing the planned workflow does not guarantee semantic correctness. An agent may execute valid tool calls and finish successfully while still selecting an incorrect issue label or source file.

Overall, strict schemas and plan enforcement help prevent malformed or unauthorized actions, while recovery logic helps address missing workflow steps. Nevertheless, these safeguards cannot independently guarantee that the agent recognizes every necessary action or reaches the correct conclusion. Evaluating tool-use reliability therefore requires examining both execution traces and the correctness of the final results.

### 4. Predictions compared with observation

I recorded my predictions in `reports/PREDICTIONS_03.md` before running the evaluation.

The predictions concerned duplicate comments, duplicate PRs, and which tool would be most frequently misused.

| Prediction | Latest observation | Assessment |
|---|---|---|
| Harness: zero duplicate-comment runs | 0/12 | Matched |
| Control: two duplicate-comment runs | 4/12 | Underestimated by two runs |
| Harness: zero duplicate PRs | No duplicate PRs identified in the checked outcomes | Matched |
| Control: zero duplicate PRs | No duplicate PRs identified in the checked outcomes | Matched |
| `code_search_repo` would be the most frequently misused tool | In the final 24 runs, no tool had a rejected/denied/protocol call or an off-plan call; a semantic usefulness ranking was not measured | **Not established** (no ranking supported) |

The clearest prediction error was the number of duplicate-comment runs in the control.

I expected two control runs to create duplicates, but the latest evaluation recorded four.

This suggests that committed-but-timed-out writes combined with fresh idempotency keys created more duplication opportunities than I anticipated.

The harness prediction was accurate: none of its twelve runs produced duplicate comments.

The absence of duplicate PRs is also consistent with the use of issue-specific branches and existing-PR checks, which can identify an earlier PR even when a retry uses a different idempotency key.

I expected `code_search_repo` to be the most frequently misused tool because repository searches involve ambiguous query selection, path filters, response limits, and decisions about how broadly to search.

Earlier development traces did contain search-related argument failures and off-plan searches. The final evaluation, however, logged **zero rejected/denied/protocol tool attempts and zero off-plan calls for every tool**. Therefore, no tool ranked above the others on these observable misuse measures. Poor search choices can still be semantic misuse, but measuring that would require a separate review of the search results and task context. I treat the prediction as **unestablished**, not as a success; I do not conflate an incorrect final answer with an observed invalid tool call.

Overall, the results support the importance of stable idempotency keys and verification when agents execute persistent writes.

They also reveal that preventing duplicate actions is not equivalent to guaranteeing grounded task completion.

---

## AI assistance

I completed this assignment with help from ChatGPT. The assistance included reviewing tool definitions, debugging argument handling and search recovery, discussing idempotency and escalation cases, and preparing the written explanation. I checked the implementation through the unit tests and schema audit, reviewed scripted and live Gemini development-run outcomes, and verified the reported write counts against the SQLite tracker and call records.