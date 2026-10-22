"""read_file: return one file of the private codebase, whole.

Its definition is read_file.json. It reads the same files search_repo searches:
everything under fixtures/repo except config/. A path is relative to
fixtures/repo, as search results print it.

It asks the file system whether the path exists. On a case-insensitive disk, which
is macOS's default, src/report.ts then opens src/Report.ts and reports success
(Assignment 3, Part B, task 3).
"""

from pathlib import Path

MAX_CHARS = 20_000   # a longer file comes back cut, with a note saying so


def run(ctx, path):
    from harness.formats import normalize_path
    from tools import ToolError
    from tools.search_repo import EXCLUDED_DIRS

    rel = normalize_path(str(path))
    if rel in (None, "none"):
        raise ToolError("path is empty")
    root = Path(ctx.repo_root).resolve()
    target = (root / rel).resolve()
    if root != target and root not in target.parents:
        raise ToolError(f"{path} is outside the repository")
    parts = target.relative_to(root).parts
    if set(parts) & EXCLUDED_DIRS:
        raise ToolError(f"{path} is not readable")
    if not target.is_file():
        raise ToolError(f"no such file: {path}")
    try:
        text = target.read_text()
    except UnicodeDecodeError:
        raise ToolError(f"{path} is not a text file")
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + f"\n[cut at {MAX_CHARS:,} of {len(text):,} characters]"
    return text
