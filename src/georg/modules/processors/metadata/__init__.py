# SPDX-License-Identifier: AGPL-3.0-only
"""Rule-based metadata generator (no LLM)."""

from georg.modules.processors.metadata.rules import (
    DocumentHint,
    MetadataOptions,
    RulesMetadataGenerator,
    parse_document_name,
)

__all__ = ["DocumentHint", "MetadataOptions", "RulesMetadataGenerator", "parse_document_name"]
