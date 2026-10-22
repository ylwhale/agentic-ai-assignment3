
# Assignment 3 — Predictions

These predictions were written before the Part D measured
evaluation runs.

## 1. Duplicate comments

Harness: I predict 0 duplicate-comment runs because
stable idempotency keys and readback verification prevent
unnecessary retries.

Control: I predict 2 duplicate-comment runs because
fresh UUID keys and disabled verification allow duplicates
when a write commits but its response times out.

## 2. Duplicate pull requests

Harness: I predict 0 duplicate PRs.

Control: I predict 0 duplicate PRs because the registry
still checks for an existing PR before creating another.

## 3. Most likely tool misuse

I predict code_search_repo will be the most frequently
misused tool because poorly chosen search terms or path
filters can miss relevant source files.
