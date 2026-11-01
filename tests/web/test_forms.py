# SPDX-License-Identifier: AGPL-3.0-only
import pytest

from georg.core.types import SermonMetadata, ServiceBoundaries
from georg.web.forms import FormError, form_values, format_hms, parse_edit, parse_hms

BOUNDS = ServiceBoundaries(
    service_start_ms=1_400,
    sermon_start_ms=1_200_000,
    sermon_end_ms=2_400_000,
    service_end_ms=3_723_999,
)
META = SermonMetadata(title="Saints", scripture=("Matthew 5:1-12", "Psalm 34"))


@pytest.mark.parametrize(
    ("text", "ms"), [("1:02:03", 3_723_000), ("2:03.5", 123_500), ("45", 45_000), (" 0:00:00 ", 0)]
)
def test_parse_hms(text: str, ms: int) -> None:
    assert parse_hms(text) == ms


@pytest.mark.parametrize("text", ["", "1:2:3:4", "0:61:00", "abc"])
def test_parse_hms_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_hms(text)


def test_format_hms_truncates_to_seconds() -> None:
    assert format_hms(3_723_999) == "1:02:03"


def test_unchanged_form_keeps_exact_values() -> None:
    form = form_values(BOUNDS, META, "text")
    edit = parse_edit(form, BOUNDS, META, "text")
    assert edit.boundaries == BOUNDS
    assert edit.metadata == META
    assert edit.changed == []
    assert not edit.boundaries_changed


def test_edits_are_reported_by_origin_key() -> None:
    form = {
        **form_values(BOUNDS, META, "text"),
        "service_end": "1:05:00",
        "scripture": "Matthew 5:1-12\r\n\r\n",
        "service_date": "2026-11-01",
        "transcript": "new text",
    }
    edit = parse_edit(form, BOUNDS, META, "text")
    assert edit.boundaries.service_end_ms == 3_900_000
    assert edit.boundaries.service_start_ms == 1_400
    assert edit.metadata.scripture == ("Matthew 5:1-12",)
    assert edit.changed == ["boundaries", "service_date", "scripture", "transcript"]


def test_invalid_values_are_all_reported() -> None:
    form = {**form_values(BOUNDS, META, None), "sermon_start": "x", "service_date": "11/01/26"}
    with pytest.raises(FormError) as exc:
        parse_edit(form, BOUNDS, META, None)
    assert len(exc.value.errors) == 2
