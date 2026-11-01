# AGENTS.md — instructions for coding agents

Read this file, `README.md`, `CONTRIBUTING.md`, and `docs/architecture.md` before starting. Your issue is your specification; if it conflicts with the architecture document, stop and ask in the issue instead of guessing.

## Project rules

- **Python 3.12, managed with `uv`.** Add dependencies with `uv add` (runtime) or `uv add --dev` (tooling). Only AGPL-3.0-compatible licenses; justify each new runtime dependency in the PR.
- **Layout:** application code in `src/georg/`, tests in `tests/` mirroring the package path, design docs in `docs/`, ADRs in `docs/adr/`.
- **Module boundaries:** input, processor, and output modules (`georg/modules/...`) implement the interfaces in `georg.core.contracts` and must not import the database, job queue, web layer, or other modules. Only the core (`pipeline`, `jobs`, `review`, `publish`) touches persistence.
- **Do not change `georg.core.contracts` or the database schema** unless your issue says so. Propose interface changes in the issue or PR description.
- **Every source file** starts with `# SPDX-License-Identifier: AGPL-3.0-only`. Never add copyright notices.
- **Secrets** come only from environment variables or the configured secret store. Never commit credentials, real API keys, or private recordings. The reference scripts this project grew from contained plaintext credentials; never copy values from them.
- **Tests** must not use the network or paid APIs. Use recorded responses or fakes. Generate test audio with FFmpeg (`lavfi` sources) rather than committing large media files.
- **Style:** `ruff` formatting and lint rules, `mypy --strict`. Prefer small, typed, pure functions. No comments that merely restate code.

## Before you open a pull request

```sh
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
uv run python scripts/check_spdx.py
```

All must pass. The PR description must list: what changed, how it satisfies each acceptance criterion, what you did not do, and any open questions.

## Git

- Branch from `main` as `agent/<issue-number>-<short-slug>`. One issue per branch and PR.
- Never push to `main`, force-push shared branches, or merge your own PR.
- Commit messages: imperative mood, short subject line.

## Review

An independent integration reviewer will check your PR against the issue's acceptance criteria and the module contracts, run the tests, and may request changes. Answer review comments in the PR. The lead architect resolves design disagreements.
