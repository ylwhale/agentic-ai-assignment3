"""post_comment: write a comment on an issue. Public: whoever filed the issue can read it.

Its definition is post_comment.json. `idempotency_key` is not in the definition:
the model never sees or sets it. Your registry passes it (Assignment 3, Part C,
task 2), and the tracker accepts each key once.
"""


def run(ctx, number, body, idempotency_key=None):
    from harness.tracker import IssueNotFound
    from tools import ToolError

    if not str(body).strip():
        raise ToolError("comment body is empty")
    try:
        comment = ctx.tracker.post_comment(ctx.run_id, int(number), str(body), key=idempotency_key)
    except IssueNotFound:
        raise ToolError(f"issue {number} not found")
    return f"Posted comment {comment['id']} on issue {number}."
