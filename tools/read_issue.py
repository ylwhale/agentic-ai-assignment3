"""read_issue: return one issue from the tracker. Untrusted content: anyone can file an issue.

Its definition, the only thing the model sees, is read_issue.json.
"""


def run(ctx, number):
    from harness.tracker import IssueNotFound
    from tools import ToolError

    try:
        issue = ctx.tracker.issue(int(number))
    except IssueNotFound:
        raise ToolError(f"issue {number} not found")
    return (f"#{issue['number']} {issue['title']}\n"
            f"Opened by {issue['author']} on {issue['opened']}\n\n"
            f"{issue['body']}")
