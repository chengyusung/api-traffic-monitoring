"""IMP-01 on the real MariaDB: an accepted file commits its batch, rows and audit row together;
a rejected one rolls back and records a FAILED batch in a transaction of its own."""

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError, UnprocessableError
from app.core.security import hash_password
from app.models import (
    ApiService,
    ImportBatch,
    ImportStatus,
    RecordSource,
    Role,
    SystemLog,
    TrafficRecord,
    User,
)
from app.services import imports
from app.services.imports import import_file

SAMPLE = Path(__file__).resolve().parents[3] / "data" / "sample_traffic.csv"
SAMPLE_LINES = SAMPLE.read_text().splitlines()
SAMPLE_ROWS = len(SAMPLE_LINES) - 1


@pytest.fixture
async def uploader(
    session_factory: async_sessionmaker[AsyncSession], services: dict[str, int]
) -> User:
    async with session_factory() as session:
        user = User(
            email="importer@test.example",
            password_hash=await hash_password("x" * 12),
            role=Role.USER,
        )
        session.add(user)
        await session.commit()
        return user


async def run_import(
    session_factory: async_sessionmaker[AsyncSession], uploader: User, content: bytes
) -> ImportBatch:
    async with session_factory() as session:
        actor = await session.get(User, uploader.id)
        assert actor is not None
        return await import_file(session, "upload.csv", content, actor)


async def counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, list[ImportBatch]]:
    async with session_factory() as session:
        records = await session.scalar(select(func.count()).select_from(TrafficRecord))
        batches = (await session.scalars(select(ImportBatch).order_by(ImportBatch.id))).all()
        return records or 0, list(batches)


async def test_the_sample_file_imports_every_row_and_records_a_succeeded_batch(
    session_factory: async_sessionmaker[AsyncSession], uploader: User
) -> None:
    batch = await run_import(session_factory, uploader, SAMPLE.read_bytes())

    assert (batch.status, batch.total_count, batch.success_count, batch.failure_count) == (
        ImportStatus.SUCCEEDED,
        SAMPLE_ROWS,
        SAMPLE_ROWS,
        0,
    )
    async with session_factory() as session:
        rows = (await session.scalars(select(TrafficRecord))).all()
        audit = (await session.scalars(select(SystemLog.event_type))).all()
    assert len(rows) == SAMPLE_ROWS
    assert {(r.source, r.created_by, r.import_batch_id) for r in rows} == {
        (RecordSource.IMPORT, uploader.id, batch.id)
    }
    # Threshold snapshot at import time: payment 80 is normal, payment 81 is an anomaly.
    payment = {r.value: (r.threshold_applied, r.is_anomaly) for r in rows if r.value in (80, 81)}
    assert payment == {80: (80, False), 81: (80, True)}
    assert audit == ["import.succeeded"]


async def test_one_bad_row_in_the_middle_writes_nothing_and_records_the_line(
    session_factory: async_sessionmaker[AsyncSession], uploader: User
) -> None:
    await run_import(session_factory, uploader, "\n".join(SAMPLE_LINES[:3]).encode())
    records_before, _ = await counts(session_factory)
    broken = list(SAMPLE_LINES)
    broken[7] = broken[7].replace(",55,", ",-55,")
    assert broken != SAMPLE_LINES

    with pytest.raises(UnprocessableError) as raised:
        await run_import(session_factory, uploader, "\n".join(broken).encode())

    records_after, batches = await counts(session_factory)
    assert records_after == records_before == 2
    failed = batches[-1]
    assert (failed.status, failed.success_count, failed.failure_count) == (
        ImportStatus.FAILED,
        0,
        1,
    )
    assert [(e["row"], e["field"]) for e in failed.errors or []] == [(8, "value")]
    assert raised.value.details == failed.errors
    assert "row 8" in raised.value.message


async def test_an_event_id_already_in_the_database_rejects_the_whole_file(
    session_factory: async_sessionmaker[AsyncSession], uploader: User
) -> None:
    await run_import(session_factory, uploader, SAMPLE.read_bytes())
    records_before, _ = await counts(session_factory)

    with pytest.raises(ConflictError) as raised:
        await run_import(session_factory, uploader, SAMPLE.read_bytes())

    records_after, batches = await counts(session_factory)
    assert records_after == records_before
    assert batches[-1].status == ImportStatus.FAILED
    assert {e["row"] for e in raised.value.details or []} == {14, 15, 16}
    async with session_factory() as session:
        audit = (await session.scalars(select(SystemLog.event_type).order_by(SystemLog.id))).all()
    assert audit == ["import.succeeded", "import.failed"]


async def test_a_service_deactivated_mid_import_records_a_failed_batch(
    session_factory: async_sessionmaker[AsyncSession],
    uploader: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        auth = await session.scalar(select(ApiService).where(ApiService.slug == "auth"))
        assert auth is not None
        auth.is_active = False
        await session.commit()
    real_parse = imports.parse
    # parse() saw the service active: the deactivation committed between the two reads.
    monkeypatch.setattr(
        imports, "parse", lambda name, content, slugs: real_parse(name, content, {*slugs, "auth"})
    )

    with pytest.raises(UnprocessableError, match="auth"):
        await run_import(session_factory, uploader, "\n".join(SAMPLE_LINES[:2]).encode())

    records, batches = await counts(session_factory)
    assert records == 0
    assert [(b.status, b.success_count) for b in batches] == [(ImportStatus.FAILED, 0)]
    assert [e["field"] for e in batches[0].errors or []] == ["service_slug"]


async def test_an_event_id_inserted_after_the_pre_check_records_a_failed_batch(
    session_factory: async_sessionmaker[AsyncSession],
    uploader: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await run_import(session_factory, uploader, SAMPLE.read_bytes())
    records_before, _ = await counts(session_factory)

    async def nothing_yet(*_: object) -> set[str]:
        return set()

    # The rows land between the pre-check and the insert, so only the unique index sees them.
    monkeypatch.setattr(imports.record_rows, "existing_event_ids", nothing_yet)
    with pytest.raises(ConflictError) as raised:
        await run_import(session_factory, uploader, SAMPLE.read_bytes())

    records_after, batches = await counts(session_factory)
    assert records_after == records_before
    assert [(b.status, b.success_count) for b in batches] == [
        (ImportStatus.SUCCEEDED, SAMPLE_ROWS),
        (ImportStatus.FAILED, 0),
    ]
    assert [(e["row"], e["field"]) for e in raised.value.details or []] == [(None, "event_id")]


async def test_rows_batch_and_audit_row_commit_together_or_not_at_all(
    session_factory: async_sessionmaker[AsyncSession],
    uploader: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def audit_down(*_: object, **__: object) -> None:
        raise RuntimeError("audit row not added")

    monkeypatch.setattr(imports.audit, "record_event", audit_down)
    with pytest.raises(RuntimeError, match="audit row"):
        await run_import(session_factory, uploader, SAMPLE.read_bytes())

    async with session_factory() as session:
        unlinked = await session.scalar(
            select(func.count())
            .select_from(TrafficRecord)
            .where(TrafficRecord.import_batch_id.is_(None))
        )
    assert (await counts(session_factory), unlinked) == ((0, []), 0)
