# Contributing to Georg

Thank you for helping. Georg is a small project; the process is deliberately light.

## License of contributions

By submitting a contribution, you agree that it is licensed under the **GNU Affero General Public License, version 3 only** (`AGPL-3.0-only`), to the extent you hold rights in it. You keep any copyright you have; no copyright assignment or CLA is required.

Only submit code you have the right to contribute. Do not copy substantial code from other projects unless its license is compatible with AGPL-3.0 and you include the required attribution; mention it in the pull request.

## Workflow

1. Work is tracked in GitHub issues. Each implementation issue states its interface, acceptance criteria, and what is out of scope.
2. Create a branch from `main` (`<you>/<issue>-<short-slug>`; agents use `agent/<issue>-<short-slug>`).
3. Keep pull requests focused on one issue. Include tests.
4. Before opening a PR, run:
   ```sh
   uv run ruff check . && uv run ruff format --check .
   uv run mypy
   uv run pytest
   uv run python scripts/check_spdx.py
   ```
5. CI must pass, and the PR needs a review before merging. `main` must always work.
6. Significant design changes need an Architecture Decision Record in `docs/adr/`.

## Source file headers

Every source file starts with:

```python
# SPDX-License-Identifier: AGPL-3.0-only
```

Do not add copyright lines to files.

## Dependencies

New runtime dependencies must have AGPL-3.0-compatible licenses (MIT, BSD, Apache-2.0, ISC, MPL-2.0, LGPL, GPL-3.0, and similar). CI checks this. Record notable dependencies and their license obligations in `docs/dependencies.md`.
