# SPDX-License-Identifier: AGPL-3.0-only
"""Plain text from companion documents (bulletins), with no extra dependencies.

Only .docx (and .txt) are read; .docx is parsed with zipfile and ElementTree. Each
`w:p` paragraph becomes one line; `w:tab` becomes a tab and `w:br`/`w:cr` a newline.
"""

import logging
import zipfile
from pathlib import Path, PurePath
from xml.etree import ElementTree

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = frozenset({".docx", ".txt"})
MAX_XML_BYTES = 20_000_000
"""Refuse a `word/document.xml` larger than this when uncompressed."""

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_P, _T, _TAB, _BR, _CR, _NB_HYPHEN = (
    f"{_W}{tag}" for tag in ("p", "t", "tab", "br", "cr", "noBreakHyphen")
)


def is_supported(name: str) -> bool:
    return PurePath(name).suffix.lower() in SUPPORTED_SUFFIXES


def docx_text(path: Path) -> str:
    """The document body's paragraphs, one per line. Raises ValueError if unreadable."""
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_XML_BYTES:
                raise ValueError(f"{path.name}: document.xml is {info.file_size} bytes")
            root = ElementTree.fromstring(archive.read(info))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, OSError) as exc:
        raise ValueError(f"{path.name}: not a readable .docx ({exc})") from exc
    paragraphs: list[str] = []
    _walk(root, paragraphs, None)
    return "\n".join(paragraphs)


def _walk(element: ElementTree.Element, paragraphs: list[str], text: list[str] | None) -> None:
    for child in element:
        if child.tag == _P:
            own: list[str] = []
            _walk(child, paragraphs, own)
            paragraphs.append("".join(own))
        elif text is None:
            _walk(child, paragraphs, text)
        elif child.tag == _T:
            text.append(child.text or "")
        elif child.tag == _TAB:
            text.append("\t")
        elif child.tag in (_BR, _CR):
            text.append("\n")
        elif child.tag == _NB_HYPHEN:
            text.append("\u2011")
        else:
            _walk(child, paragraphs, text)


def extract_text(path: Path, name: str | None = None) -> str | None:
    """Text of a supported document, or None (logged) if unsupported or unreadable."""
    label = name or path.name
    suffix = PurePath(label).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        log.info("skipping document %r: only .docx and .txt are read", label)
        return None
    try:
        if suffix == ".txt":
            return path.read_text(encoding="utf-8", errors="replace")
        return docx_text(path)
    except (ValueError, OSError) as exc:
        log.warning("could not read document %r: %s", label, exc)
        return None
