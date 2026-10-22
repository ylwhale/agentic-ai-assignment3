"""open_pr: open a draft pull request on the mock remote. New in Assignment 3.

Its definition is open_pr.json. The mock remote needs no commits: a PR is a branch,
a title and a body, stored in the tracker's database. `idempotency_key` is not in
the definition: your registry passes it, as for post_comment.
"""


def run(ctx, number, branch, title, body, idempotency_key=None):
    from harness.tracker import IssueNotFound
    from tools import ToolError

    try:
        pr = ctx.tracker.open_pr(ctx.run_id, int(number), str(branch), str(title), str(body), key=idempotency_key)
    except IssueNotFound:
        raise ToolError(f"issue {number} not found")
    return f"Opened draft pull request {pr['id']} from {branch} for issue {number}."
