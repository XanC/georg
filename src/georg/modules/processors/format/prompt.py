# SPDX-License-Identifier: AGPL-3.0-only
"""Prompt templates and output cleanup shared by the chat-model formatters."""

import hashlib
import re
from importlib import resources
from pathlib import Path
from string import Template

from georg.core.errors import ConfigError
from georg.modules.processors.format.words import tokenize

_FENCE = re.compile(r"^```[\w-]*\n(?P<body>.*)\n```$", re.DOTALL)
_HEADING = re.compile(r"^(?:#{1,6}[ \t]+[^\n]*|\*\*[^*\n]+\*\*|__[^_\n]+__)[ \t]*(?:\n|$)")
_THINK = re.compile(r"^\s*<think>.*?</think>", re.DOTALL)


def load_template(
    prompt_version: str, prompt_file: Path | None, stem: str = "format"
) -> tuple[Template, str]:
    """Load `prompts/<stem>_<version>.txt` or `prompt_file`; return it and a prompt id."""
    try:
        if prompt_file is not None:
            text = prompt_file.read_text(encoding="utf-8")
            prompt_id = "file-" + hashlib.sha256(text.encode()).hexdigest()[:12]
        else:
            name = f"{stem}_{prompt_version}.txt"
            text = resources.files(__package__).joinpath("prompts", name).read_text("utf-8")
            prompt_id = prompt_version
    except OSError as exc:
        raise ConfigError(f"formatter prompt template not found: {exc}") from exc
    if "$text" not in text:
        raise ConfigError("formatter prompt template must contain $text")
    return Template(text), prompt_id


def clean_output(output: str, source: str) -> str:
    """Drop a reasoning block, a code fence, and a heading the source doesn't start with."""
    text = _THINK.sub("", output, count=1).strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group("body").strip()
    heading = _HEADING.match(text)
    if heading:
        head = tokenize(heading.group(0))
        if tokenize(source)[: len(head)] != head:
            text = text[heading.end() :].strip()
    return text
