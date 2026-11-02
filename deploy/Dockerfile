# SPDX-License-Identifier: AGPL-3.0-only
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Optional extras to install, space-separated, e.g. "classify whisper gdrive".
ARG GEORG_EXTRAS=""

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project $(for e in $GEORG_EXTRAS; do echo --extra $e; done)
COPY src ./src
RUN uv sync --locked --no-dev $(for e in $GEORG_EXTRAS; do echo --extra $e; done)

# Optionally bake a GGUF model for the `llama_cpp` formatter into the image (needs the
# `llm` extra), e.g. "unsloth/Qwen3-4B-Instruct-2507-GGUF Qwen3-4B-Instruct-2507-Q4_K_M.gguf".
# It lands in /opt/georg/models; point the formatter's `model_path` at the file.
ARG GEORG_BAKE_MODEL=""
RUN if [ -n "$GEORG_BAKE_MODEL" ]; then hf download $GEORG_BAKE_MODEL --local-dir /opt/georg/models; fi

RUN useradd --create-home --uid 1000 georg
USER georg
CMD ["georg", "--help"]
