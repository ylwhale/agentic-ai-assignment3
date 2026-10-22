"""Literal, case-insensitive search over the private codebase.

Reads a prebuilt index or searches files directly when deep=True.

Malformed queries produce ToolError with actionable feedback.
Valid searches return JSON containing the echoed query, matching lines,
result count, query validity, truncation status, and index age.

The response_format parameter selects concise or detailed excerpts.
Results are limited to at most HARD_CAP matching lines.
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

EXCLUDED_DIRS = {"config", "node_modules", ".git"}
HARD_CAP = 20
MAX_QUERY_CHARS = 200
INDEX = "fixtures/search_index/index.json"
GLOB_CHARS = re.compile(r"^[A-Za-z0-9_.\-/*?\[\]{},]+$")


def glob_problem(pattern: str) -> str | None:
    """Why `pattern` is not a valid path glob, or None when it is one."""
    if pattern.startswith("/"):
        return "a glob is relative to the repository root; it cannot start with /"
    if ".." in pattern.split("/"):
        return "a glob cannot leave the repository (..)"
    if not GLOB_CHARS.match(pattern):
        return "a glob uses letters, digits, / . _ - and the wildcards * ? [ ] { }; no spaces, colons or quotes"
    depth = 0
    for ch in pattern:
        depth += {"[": 1, "]": -1}.get(ch, 0)
        if depth not in (0, 1):
            return "a glob's [ and ] must pair up"
    if depth:
        return "a glob's [ and ] must pair up"
    if pattern.count("{") != pattern.count("}"):
        return "a glob's { and } must pair up"
    return None



def problem(query, path_glob=None) -> str | None:
    """Return an actionable reason if search inputs are malformed."""
    if not isinstance(query, str):
        return "query must be a string"

    if not query.strip():
        return "query is empty or whitespace-only; use a non-empty code term"

    if len(query) > MAX_QUERY_CHARS:
        return f"query is {len(query)} characters long; maximum is {MAX_QUERY_CHARS}"

    if path_glob is not None:
        if not isinstance(path_glob, str):
            return "path_glob must be a repository-relative glob string or null"
        if not path_glob:
            return "path_glob is empty; use null to search all paths"

        invalid = glob_problem(path_glob)
        if invalid is not None:
            return f"invalid path_glob: {invalid}"

    return None



def _expand(pattern: str) -> list[str]:
    """{a,b} alternatives, which fnmatch does not know."""
    match = re.search(r"\{([^{}]*)\}", pattern)
    if not match:
        return [pattern]
    return [p for option in match.group(1).split(",")
            for p in _expand(pattern[:match.start()] + option + pattern[match.end():])]


def _matches(path: str, pattern: str | None) -> bool:
    if not pattern:
        return True
    return any(fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(path, p.replace("**/", ""))
               for p in _expand(pattern))


def load_index(root: Path) -> dict:
    return json.loads((Path(root) / INDEX).read_text())


def _disk(repo_root: Path) -> dict[str, list[str]]:
    files = {}
    for path in sorted(Path(repo_root).rglob("*")):
        rel = path.relative_to(repo_root)
        if path.is_file() and not (set(rel.parts) & EXCLUDED_DIRS):
            try:
                files[rel.as_posix()] = path.read_text().splitlines()
            except UnicodeDecodeError:
                continue
    return files


def age(built_at: str, now: float | None = None) -> str:
    built = datetime.strptime(built_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    days = int(((now or time.time()) - built.timestamp()) // 86400)
    return "today" if days < 1 else f"{days} day{'s' if days != 1 else ''} ago"



def run(ctx, query, path_glob=None, max_results=10, deep=False,
        response_format="concise"):
    """Search the index or disk and return a bounded, machine-readable result."""
    from tools import ToolError

    # 1. Validate the query and path filter
    error = problem(query, path_glob)
    if error:
        raise ToolError(
            f"Invalid search: {error}. Correct the named argument and retry."
        )

    # 2. Validate the remaining arguments
    if type(max_results) is not int or not 1 <= max_results <= HARD_CAP:
        raise ToolError(
            f"max_results must be an integer from 1 to {HARD_CAP}; "
            "use 10 for a normal search"
        )

    if type(deep) is not bool:
        raise ToolError(
            "deep must be true or false; use false for the indexed search"
        )

    if response_format not in ("concise", "detailed"):
        raise ToolError(
            "response_format must be concise or detailed; use concise first"
        )

    # 3. Choose the search source
    repo_root = Path(ctx.repo_root)
    index = load_index(repo_root.parent.parent)

    files = _disk(repo_root) if deep else index["files"]
    source = "disk" if deep else "index"

    # 4. Choose the excerpt size
    excerpt_cap = 160 if response_format == "concise" else 500
    needle = query.strip().lower()

    # 5. Search matching lines
    hits = []
    matched = 0
    clipped_excerpt = False

    for rel, lines in sorted(files.items()):
        if not _matches(rel, path_glob):
            continue

        for line_number, line in enumerate(lines, 1):
            if needle not in line.lower():
                continue

            matched += 1

            if len(hits) >= max_results:
                continue

            snippet = line.strip()
            clipped = len(snippet) > excerpt_cap
            clipped_excerpt = clipped_excerpt or clipped

            hits.append({
                "path": rel,
                "line": line_number,
                "excerpt": snippet[:excerpt_cap],
                "excerpt_truncated": clipped,
            })

    # 6. Return structured JSON
    return json.dumps({
        "query": query,
        "query_valid": True,
        "count": matched,
        "results": hits,
        "truncated": matched > len(hits) or clipped_excerpt,
        "index_age": age(index["built_at"]),
        "source": source,
        "response_format": response_format,
    }, ensure_ascii=False)

