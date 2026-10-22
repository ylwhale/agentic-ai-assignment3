
"""Read repository text files using exact, case-sensitive paths.

The model-facing interface is defined in read_file.json.
Results are bounded JSON objects. Invalid paths produce actionable
ToolError messages.
"""

from __future__ import annotations

import json
from difflib import get_close_matches
from pathlib import Path

CONCISE_CHARS = 4_000
DETAILED_CHARS = 20_000


def run(ctx, path, response_format="concise"):
    from harness.formats import normalize_path
    from tools import ToolError
    from tools.search_repo import EXCLUDED_DIRS

    # 1. Validate arguments
    if not isinstance(path, str):
        raise ToolError(
            "Invalid path: supply a repository-relative filename as a string."
        )

    if response_format not in ("concise", "detailed"):
        raise ToolError(
            "Invalid response_format: use 'concise' or 'detailed'."
        )

    rel = normalize_path(path)
    if rel in (None, "none"):
        raise ToolError(
            "Invalid path: supply an existing repository-relative filename."
        )

    # 2. Prevent access outside the repository
    root = Path(ctx.repo_root).resolve()
    target = (root / rel).resolve()

    if root != target and root not in target.parents:
        raise ToolError(
            f"Invalid path '{path}': file is outside the repository. "
            "Use a path under the repository root."
        )

    if (set(Path(rel).parts) & EXCLUDED_DIRS
            or set(target.relative_to(root).parts) & EXCLUDED_DIRS):
        raise ToolError(
            f"Invalid path '{path}': excluded directory. "
            "Choose a readable repository source file."
        )

    # 3. Build a list of exact existing filenames
    available = []

    for candidate in root.rglob("*"):
        candidate_rel = candidate.relative_to(root)

        if set(candidate_rel.parts) & EXCLUDED_DIRS:
            continue

        if not candidate.is_file():
            continue

        actual = candidate.resolve()

        if root != actual and root not in actual.parents:
            continue

        if set(actual.relative_to(root).parts) & EXCLUDED_DIRS:
            continue

        available.append(candidate_rel.as_posix())

    # 4. Check the requested filename, including capitalization
    if rel not in available:

        # Prioritize filenames differing only in capitalization
        closest_case = [
            name for name in available
            if name.casefold() == rel.casefold()
        ]

        # Find other similarly named files
        nearby = closest_case + [
            name for name in get_close_matches(
                rel, available, n=5, cutoff=0.6
            )
            if name not in closest_case
        ]

        if nearby:
            raise ToolError(
                f"File path '{rel}' does not exist with that exact spelling. "
                f"Similar existing paths: {', '.join(nearby[:5])}. "
                "Retry with one of the exact paths, including case."
            )

        raise ToolError(
            f"File path '{rel}' does not exist. "
            "Find a real filename with code_search_repo "
            "and retry using the exact path."
        )

    # 5. Read the file
    try:
        text = target.read_text(encoding="utf-8")

    except UnicodeDecodeError:
        raise ToolError(
            f"File '{rel}' is not UTF-8 text. "
            "Choose a text source file."
        ) from None

    except OSError:
        raise ToolError(
            f"File '{rel}' could not be read. "
            "Choose an accessible text file."
        ) from None

    # 6. Apply the response size limit
    cap = (
        CONCISE_CHARS
        if response_format == "concise"
        else DETAILED_CHARS
    )

    # 7. Return a structured JSON result
    return json.dumps({
        "path": rel,
        "content": text[:cap],
        "total_chars": len(text),
        "truncated": len(text) > cap,
        "response_format": response_format,
    }, ensure_ascii=False)

