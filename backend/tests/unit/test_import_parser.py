import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.services.imports import COLUMNS, parse

DATA = Path(__file__).resolve().parents[3] / "data"
SLUGS = {"auth", "orders", "payment"}
GOOD = ",payment,checkout,50,2026-10-31T08:00:00Z"
EVENT_ID = "3f8b2c6e-5a1d-4e7f-9b2a-0c4d6e8f1a23"


def csv_file(*lines: str) -> bytes:
    return "\n".join([",".join(COLUMNS), *lines]).encode()


def json_file(*items: Any) -> bytes:
    return json.dumps(list(items)).encode()


def json_row(**overrides: Any) -> dict[str, Any]:
    return {
        "event_id": None,
        "service_slug": "payment",
        "title": "checkout",
        "value": 50,
        "observed_at": "2026-10-31T08:00:00Z",
        **overrides,
    }


def rejected(content: bytes, filename: str = "upload.csv") -> set[tuple[int | None, str | None]]:
    parsed = parse(filename, content, SLUGS)
    return {(e["row"], e["field"]) for e in parsed.errors}


@pytest.mark.parametrize("name", ["sample_traffic.csv", "sample_traffic.json"])
def test_the_sample_files_parse_without_errors(name: str) -> None:
    parsed = parse(name, (DATA / name).read_bytes(), SLUGS)

    assert parsed.errors == []
    assert len(parsed.rows) == parsed.total > 0


def test_a_valid_row_becomes_a_utc_record_with_its_csv_line() -> None:
    parsed = parse(
        "upload.csv", csv_file(GOOD, f"{EVENT_ID},auth,login,0,2026-10-31T16:00:04+08:00"), SLUGS
    )

    assert parsed.errors == []
    [(first_line, first), (second_line, second)] = parsed.rows
    assert (first_line, first.event_id, first.value) == (2, None, 50)
    assert (second_line, second.event_id) == (3, uuid.UUID(EVENT_ID))
    assert second.observed_at == datetime(2026, 10, 31, 8, 0, 4, tzinfo=UTC)


def test_an_error_names_the_csv_line_of_the_bad_row() -> None:
    content = csv_file(GOOD, GOOD, ",payment,checkout,abc,2026-10-31T08:00:00Z", GOOD)

    assert rejected(content) == {(4, "value")}


def test_every_bad_row_is_reported_not_only_the_first() -> None:
    content = csv_file(
        GOOD, ",payment,,50,2026-10-31T08:00:00Z", GOOD, ",nope,x,1,2026-10-31T08:00:00Z"
    )

    assert rejected(content) == {(3, "title"), (5, "service_slug")}


def test_a_duplicate_event_id_inside_the_file_names_both_rows() -> None:
    row = f"{EVENT_ID},payment,checkout,50,2026-10-31T08:00:00Z"
    parsed = parse(
        "upload.csv", csv_file(row, GOOD, row.replace(EVENT_ID, EVENT_ID.upper())), SLUGS
    )

    [error] = parsed.errors
    assert (error["row"], error["field"]) == (4, "event_id")
    assert "row 2" in error["message"]


def test_a_duplicate_event_id_is_named_even_when_its_first_row_is_invalid() -> None:
    first = f"{EVENT_ID},payment,checkout,-1,2026-10-31T08:00:00Z"
    second = f"{EVENT_ID.upper()},payment,checkout,50,2026-10-31T08:00:00Z"

    assert rejected(csv_file(first, second)) == {(2, "value"), (3, "event_id")}


def test_an_unknown_key_is_named_in_at_most_64_characters() -> None:
    parsed = parse("upload.json", json_file(json_row(**{"k" * 300: 1})), SLUGS)

    [error] = parsed.errors
    assert error["field"] == "k" * 64


def test_an_unknown_or_inactive_service_is_a_row_error() -> None:
    parsed = parse("upload.csv", csv_file(GOOD.replace("payment", "orders")), {"payment"})

    assert {(e["row"], e["field"]) for e in parsed.errors} == {(2, "service_slug")}


@pytest.mark.parametrize("observed_at", ["1761897600", "2026"])
def test_a_unix_time_observed_at_is_rejected(observed_at: str) -> None:
    assert rejected(csv_file(f",payment,checkout,50,{observed_at}")) == {(2, "observed_at")}
    json_value = int(observed_at)
    assert rejected(json_file(json_row(observed_at=json_value)), "u.json") == {(1, "observed_at")}


def test_a_naive_observed_at_is_rejected() -> None:
    assert rejected(csv_file(",payment,checkout,50,2026-10-31T08:00:00")) == {(2, "observed_at")}


@pytest.mark.parametrize("value", ["-1", "5.0", "", "1e3", "2147483648", "9" * 5000])
def test_value_must_be_a_non_negative_integer(value: str) -> None:
    content = csv_file(f",payment,checkout,{value},2026-10-31T08:00:00Z")

    assert rejected(content) == {(2, "value")}


def test_event_id_must_be_empty_or_a_uuid() -> None:
    assert rejected(csv_file(f"not-a-uuid{GOOD}")) == {(2, "event_id")}


@pytest.mark.parametrize(
    "header",
    [
        "service_slug,event_id,title,value,observed_at",
        "event_id,service_slug,title,value",
        "event_id,service_slug,title,value,observed_at,source",
    ],
)
def test_a_header_mismatch_rejects_the_file_at_line_1(header: str) -> None:
    parsed = parse("upload.csv", f"{header}\n{GOOD}".encode(), SLUGS)

    assert (parsed.total, parsed.rows) == (0, [])
    assert [(e["row"], e["field"]) for e in parsed.errors] == [(1, None)]


def test_a_row_with_the_wrong_number_of_fields_is_a_row_error() -> None:
    assert rejected(csv_file(GOOD, ",payment,checkout,50")) == {(3, None)}


def test_blank_lines_and_a_utf8_bom_are_tolerated() -> None:
    parsed = parse("upload.csv", b"\xef\xbb\xbf" + csv_file(GOOD, "", GOOD), SLUGS)

    assert parsed.errors == []
    assert [line for line, _ in parsed.rows] == [2, 4]


def test_json_rows_are_numbered_by_array_position() -> None:
    content = json_file(json_row(), json_row(value=-1), "not an object", json_row(source="IMPORT"))

    assert rejected(content, "upload.json") == {(2, "value"), (3, None), (4, "source")}


@pytest.mark.parametrize("value", ["5", 5.0, True])
def test_json_value_must_be_a_json_integer(value: Any) -> None:
    assert rejected(json_file(json_row(value=value)), "upload.json") == {(1, "value")}


@pytest.mark.parametrize("event_id", [None, ""])
def test_an_empty_json_event_id_is_left_for_the_server(event_id: str | None) -> None:
    parsed = parse("upload.json", json_file(json_row(event_id=event_id)), SLUGS)

    assert parsed.errors == []
    assert parsed.rows[0][1].event_id is None


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("upload.json", b'{"event_id": null}'),
        ("upload.json", b"[{"),
        ("upload.txt", csv_file(GOOD)),
        ("upload.csv", "\n".join([",".join(COLUMNS), GOOD]).encode("utf-16")),
        ("upload.json", b"[" * 200_000),
        ("upload.csv", csv_file()),
        ("upload.csv", csv_file("", "")),
        ("upload.json", b"[]"),
    ],
    ids=[
        "json object",
        "truncated json",
        "txt",
        "utf-16",
        "deep json",
        "header only",
        "blank rows",
        "empty array",
    ],
)
def test_an_unreadable_file_is_one_file_level_error(filename: str, content: bytes) -> None:
    parsed = parse(filename, content, SLUGS)

    assert (parsed.total, parsed.rows) == (0, [])
    assert [(e["row"], e["field"]) for e in parsed.errors] == [(None, None)]


def test_row_and_byte_limits_reject_the_whole_file() -> None:
    three_rows = csv_file(GOOD, GOOD, GOOD)

    assert parse("upload.csv", three_rows, SLUGS, max_rows=3).errors == []
    assert [e["row"] for e in parse("upload.csv", three_rows, SLUGS, max_rows=2).errors] == [None]
    too_big = parse("upload.csv", three_rows, SLUGS, max_bytes=len(three_rows) - 1)
    assert [e["row"] for e in too_big.errors] == [None]
    json_rows = json_file(json_row(), json_row(), json_row())
    assert [e["row"] for e in parse("u.json", json_rows, SLUGS, max_rows=2).errors] == [None]
