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

# The `llm` extra compiles llama.cpp; target AVX2 rather than the build host's CPU so the
# image also runs when the instance type changes.
ARG LLAMA_CMAKE_ARGS="-DGGML_NATIVE=OFF -DGGML_AVX=ON -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON"

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN case " $GEORG_EXTRAS " in *" llm "*) \
        apt-get update && apt-get install -y --no-install-recommends build-essential ;; \
    esac \
    && CMAKE_ARGS="$LLAMA_CMAKE_ARGS" uv sync --locked --no-dev --no-install-project \
        $(for e in $GEORG_EXTRAS; do echo --extra $e; done) \
    && if dpkg -s build-essential >/dev/null 2>&1; then \
        apt-get purge -y --auto-remove build-essential && rm -rf /var/lib/apt/lists/*; fi
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
