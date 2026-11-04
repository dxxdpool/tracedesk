"""The database enforces the rules too, not just the API: a bad row written directly fails."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def insert_investigation(session: AsyncSession, status: str = "open") -> str:
    result = await session.execute(
        text(
            "INSERT INTO investigations (repo_url, problem, status) "
            "VALUES ('https://github.com/x/y', 'p', :status) RETURNING id"
        ),
        {"status": status},
    )
    return str(result.scalar_one())


async def test_database_rejects_unknown_status(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        with pytest.raises(IntegrityError, match="ck_investigations_status"):
            await insert_investigation(session, status="pending")


async def test_database_rejects_hypothesis_with_missing_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with pytest.raises(IntegrityError, match="fk_hypotheses_investigation_id_investigations"):
            await session.execute(
                text(
                    "INSERT INTO hypotheses (investigation_id, text, created_by) "
                    "VALUES (gen_random_uuid(), 't', 'a')"
                )
            )


async def test_database_rejects_reversed_line_range(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        inv_id = await insert_investigation(session)
        with pytest.raises(IntegrityError, match="ck_evidence_line_range_ordered"):
            await session.execute(
                text(
                    "INSERT INTO evidence (investigation_id, kind, source_type, line_start, line_end, created_by) "
                    "VALUES (:inv, 'supporting', 'code_ref', 9, 3, 'a')"
                ),
                {"inv": inv_id},
            )


async def test_deleting_investigation_removes_its_ledger(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        inv_id = await insert_investigation(session)
        await session.execute(
            text("INSERT INTO hypotheses (investigation_id, text, created_by) VALUES (:inv, 't', 'a')"),
            {"inv": inv_id},
        )
        await session.execute(text("DELETE FROM investigations WHERE id = :inv"), {"inv": inv_id})
        remaining = await session.execute(text("SELECT count(*) FROM hypotheses"))

        assert remaining.scalar_one() == 0
