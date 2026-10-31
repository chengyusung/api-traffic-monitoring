"""CSV / JSON import, all or nothing (architecture.md §5.3)."""

import csv
import io
import json
import re
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ConflictError, NotFoundError, UnprocessableError
from app.models import AuditEvent, ImportBatch, ImportStatus, RecordSource, Role, User
from app.repositories import api_services
from app.repositories import records as record_rows
from app.schemas.imports import RowError
from app.schemas.records import RecordCreate
from app.services import audit, records

COLUMNS = ["event_id", "service_slug", "title", "value", "observed_at"]
# Bounds what a rejected 10,000-row file stores and returns; failure_count still counts every row.
REPORTED_ERRORS = 100
_INTEGER = re.compile(r"-?[0-9]+")

# A raw row is a dict of fields, or a message when the row has no usable shape.
RawRows = list[tuple[int, dict[str, Any] | str]]


class _Rejected(Exception):
    """The file as a whole cannot be read, so no row is validated."""

    def __init__(self, row: int | None, message: str) -> None:
        super().__init__(message)
        self.row = row


@dataclass
class ParsedFile:
    total: int = 0
    rows: list[tuple[int, RecordCreate]] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)


def _csv_rows(text: str, max_rows: int) -> RawRows:
    reader = csv.reader(io.StringIO(text, newline=""))
    rows: RawRows = []
    try:
        if next(reader, None) != COLUMNS:
            raise _Rejected(1, f"header must be {','.join(COLUMNS)}")
        for fields in reader:
            if not fields:
                continue
            if len(rows) == max_rows:
                raise _Rejected(None, f"file has more than {max_rows} rows")
            if len(fields) != len(COLUMNS):
                rows.append((reader.line_num, f"expected {len(COLUMNS)} fields, got {len(fields)}"))
                continue
            raw = dict(zip(COLUMNS, fields, strict=True))
            # CSV has no number type; only a plain integer becomes one, so "5.0" stays invalid.
            if _INTEGER.fullmatch(raw["value"]):
                raw["value"] = int(raw["value"])
            rows.append((reader.line_num, raw))
    except csv.Error as exc:
        raise _Rejected(reader.line_num, f"malformed CSV: {exc}") from exc
    return rows


def _json_rows(text: str, max_rows: int) -> RawRows:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _Rejected(None, f"invalid JSON at line {exc.lineno} column {exc.colno}") from exc
    if not isinstance(data, list):
        raise _Rejected(None, "JSON must be an array of objects")
    if len(data) > max_rows:
        raise _Rejected(None, f"file has more than {max_rows} rows")
    return [
        (row, item if isinstance(item, dict) else "must be a JSON object")
        for row, item in enumerate(data, start=1)
    ]


READERS: dict[str, Callable[[str, int], RawRows]] = {".csv": _csv_rows, ".json": _json_rows}


def _read(filename: str, content: bytes, max_rows: int, max_bytes: int) -> RawRows:
    if len(content) > max_bytes:
        raise _Rejected(None, f"file is larger than {max_bytes} bytes")
    reader = READERS.get(PurePath(filename).suffix.lower())
    if reader is None:
        raise _Rejected(None, "file must be .csv or .json")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _Rejected(None, "file is not UTF-8 text") from exc
    return reader(text, max_rows)


def _validate(
    row: int,
    raw: dict[str, Any] | str,
    active_slugs: Collection[str],
    seen: dict[uuid.UUID, int],
) -> RecordCreate | list[RowError]:
    if isinstance(raw, str):
        return [RowError(row=row, field=None, message=raw)]
    if raw.get("event_id") == "":
        raw = {**raw, "event_id": None}
    try:
        item = RecordCreate.model_validate(raw)
    except ValidationError as exc:
        # The reason only, never the `input` that pydantic also reports.
        return [
            RowError(row=row, field=".".join(map(str, e["loc"])) or None, message=e["msg"])
            for e in exc.errors()
        ]
    errors: list[RowError] = []
    if item.service_slug not in active_slugs:
        errors.append(
            RowError(row=row, field="service_slug", message="unknown or inactive service")
        )
    if item.event_id is not None and (first := seen.setdefault(item.event_id, row)) != row:
        errors.append(RowError(row=row, field="event_id", message=f"duplicates row {first}"))
    return errors or item


def parse(
    filename: str,
    content: bytes,
    active_slugs: Collection[str],
    max_rows: int = settings.import_max_rows,
    max_bytes: int = settings.import_max_bytes,
) -> ParsedFile:
    """Validate every row before anything is written; pure, so the rules test without a DB."""
    try:
        raw_rows = _read(filename, content, max_rows, max_bytes)
    except _Rejected as exc:
        return ParsedFile(errors=[RowError(row=exc.row, field=None, message=str(exc))])
    parsed = ParsedFile(total=len(raw_rows))
    seen: dict[uuid.UUID, int] = {}
    for row, raw in raw_rows:
        result = _validate(row, raw, active_slugs, seen)
        if isinstance(result, list):
            parsed.errors.extend(result)
        else:
            parsed.rows.append((row, result))
    return parsed


async def _conflicts(session: AsyncSession, rows: list[tuple[int, RecordCreate]]) -> list[RowError]:
    given = {str(item.event_id): row for row, item in rows if item.event_id is not None}
    existing = await record_rows.existing_event_ids(session, given.keys())
    return [
        RowError(row=given[event_id], field="event_id", message="event_id already exists")
        for event_id in sorted(existing, key=given.__getitem__)
    ]


def _describe(errors: list[RowError], failed_rows: int) -> str:
    shown = [
        f"{'row ' + str(e['row']) if e['row'] is not None else 'file'}"
        f"{' ' + e['field'] if e['field'] else ''}: {e['message']}"
        for e in errors[:5]
    ]
    more = f"; and {len(errors) - len(shown)} more" if len(errors) > len(shown) else ""
    rows = f" {failed_rows} row(s) rejected." if failed_rows else ""
    return f"Import failed; nothing was imported.{rows} {'; '.join(shown)}{more}"


async def import_file(
    session: AsyncSession, filename: str, content: bytes, actor: User
) -> ImportBatch:
    # A rollback below expires every loaded row, the request's User included.
    actor_id = actor.id
    services = await api_services.list_all(session)
    parsed = parse(filename, content, {s.slug for s in services if s.is_active})
    errors, error_type = parsed.errors, UnprocessableError
    if not errors:
        errors, error_type = await _conflicts(session, parsed.rows), ConflictError
    created = []
    if not errors:
        try:
            created = await records.create_records(
                session, [item for _, item in parsed.rows], actor, RecordSource.IMPORT
            )
        except ConflictError:
            errors = [
                RowError(
                    row=None, field="event_id", message="an event_id was inserted concurrently"
                )
            ]
    if not created:
        await session.rollback()

    # The batch row is its own transaction, so a rejected file is still recorded.
    # Known gap: if this commit fails after the data committed, the client sees 503 and a retry
    # duplicates rows whose event_id was blank; the window is two back-to-back commits.
    failed_rows = len({e["row"] for e in errors if e["row"] is not None})
    batch = ImportBatch(
        filename=filename[:255],
        uploaded_by=actor_id,
        status=ImportStatus.FAILED if errors else ImportStatus.SUCCEEDED,
        total_count=parsed.total,
        success_count=len(created),
        failure_count=failed_rows,
        errors=errors[:REPORTED_ERRORS] or None,
    )
    session.add(batch)
    await session.flush()
    if created:
        await record_rows.link_import_batch(session, [r.id for r in created], batch.id)
    audit.record_event(
        session,
        AuditEvent.IMPORT_FAILED if errors else AuditEvent.IMPORT_SUCCEEDED,
        f"import {batch.id} {batch.status}: {batch.success_count}/{batch.total_count} rows",
        level="WARNING" if errors else "INFO",
        actor_id=actor_id,
        target_type="import_batch",
        target_id=batch.id,
        context={
            "filename": batch.filename,
            "total_count": batch.total_count,
            "success_count": batch.success_count,
            "failure_count": batch.failure_count,
        },
    )
    await session.commit()
    if errors:
        raise error_type(_describe(errors, failed_rows), details=batch.errors)
    return batch


async def get_batch(session: AsyncSession, batch_id: int, user: User) -> ImportBatch:
    batch = await session.get(ImportBatch, batch_id)
    # Same rule as records: a refusal never confirms that the batch exists.
    if batch is None or not (user.role == Role.ADMIN or batch.uploaded_by == user.id):
        raise NotFoundError("Import not found.")
    return batch
