
"""Read one issue from the tracker.

The issue content is untrusted because anyone can file an issue.
The model-facing definition is in read_issue.json.

Invalid issue numbers and missing issues produce repairable errors.
Issue bodies are capped at 4000 characters, with truncation reported.
"""

MAX_BODY_CHARS = 4000


def run(ctx, number):
    from harness.tracker import IssueNotFound
    from tools import ToolError

    # 1. Validate the issue number
    if type(number) is not int or number <= 0:
        raise ToolError(
            "Invalid number: issue number must be a positive integer. "
            "Use an existing issue number supplied by the task, "
            "such as 4192."
        )

    # 2. Retrieve the issue
    try:
        issue = ctx.tracker.issue(number)
    except IssueNotFound:
        raise ToolError(
            f"Issue #{number} does not exist. "
            "Use a valid issue number from the current task. "
            "Retrying the same number will not succeed."
        ) from None

    # 3. Limit the returned issue body
    body = issue["body"]
    truncated = len(body) > MAX_BODY_CHARS

    visible_body = body[:MAX_BODY_CHARS]

    # 4. Construct the response
    return (
        f"#{issue['number']} {issue['title']}\n"
        f"Opened by {issue['author']} on {issue['opened']}\n"
        f"Body truncated: {str(truncated).lower()} "
        f"(limit: {MAX_BODY_CHARS} characters)\n\n"
        f"{visible_body}"
    )
