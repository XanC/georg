# SPDX-License-Identifier: AGPL-3.0-only
"""Rule-based metadata generator, with optional Scripture detection."""

from georg.modules.processors.metadata.readings import (
    BulletinHeader,
    Reading,
    parse_header,
    parse_readings,
)
from georg.modules.processors.metadata.references import normalize_reference, parse_reference
from georg.modules.processors.metadata.rules import (
    DocumentHint,
    MetadataOptions,
    RulesMetadataGenerator,
    parse_document_name,
)
from georg.modules.processors.metadata.scripture import (
    ScriptureDetector,
    ScriptureOptions,
    ScriptureResult,
)

__all__ = [
    "BulletinHeader",
    "DocumentHint",
    "MetadataOptions",
    "Reading",
    "RulesMetadataGenerator",
    "ScriptureDetector",
    "ScriptureOptions",
    "ScriptureResult",
    "normalize_reference",
    "parse_document_name",
    "parse_header",
    "parse_readings",
    "parse_reference",
]
