"""Backups: a real dump and restore of the test database, and the nightly schedule."""

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from kairos_db.memories import MemoryStore
from kairos_domain.memory import MemoryKind, MemorySource, MemoryStatus
from kairos_domain.users import LOCAL_USER
from kairos_worker.backup import BackupError, backup, due, dumps, prune, restore

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)  # 04:00 in Toronto


def docker_exec(container: PostgresContainer, *command: str) -> list[str]:
    wrapped = container.get_wrapped_container()
    return ["docker", "exec", "-i", str(wrapped.id), *command]


async def test_a_dump_restores_deleted_data(
    engine: AsyncEngine, postgres: PostgresContainer, tmp_path: Path
) -> None:
    store = MemoryStore(engine)
    kept = await store.create(
        "Lives in Toronto",
        MemoryKind.PROFILE,
        source=MemorySource.USER,
        status=MemoryStatus.ACTIVE,
        owner=LOCAL_USER,
    )
    user, db = postgres.username, postgres.dbname
    dump = await backup(
        tmp_path, now=NOW, command=docker_exec(postgres, "pg_dump", "-U", user, "-Fc", db)
    )
    assert dump.name == "kairos-20261118-090000.dump"
    assert dump.stat().st_size > 1000
    assert not list(tmp_path.glob("*.partial"))  # noqa: ASYNC240  # a test assertion

    await store.delete(kept.id, owner=LOCAL_USER)
    assert await store.get(kept.id, owner=LOCAL_USER) is None
    await engine.dispose()  # let pg_restore drop and recreate tables
    await restore(
        dump,
        command=docker_exec(postgres, "pg_restore", "-U", user, "-d", db, "--clean", "--if-exists"),
    )
    restored = await store.get(kept.id, owner=LOCAL_USER)
    assert restored is not None
    assert restored.content == "Lives in Toronto"


async def test_a_failed_dump_leaves_no_file(tmp_path: Path) -> None:
    failing = [sys.executable, "-c", "import sys; sys.stderr.write('no such db'); sys.exit(2)"]
    with pytest.raises(BackupError, match="exited 2: no such db"):
        await backup(tmp_path, now=NOW, command=failing)
    assert list(tmp_path.iterdir()) == []  # noqa: ASYNC240  # a test assertion


def test_old_dumps_are_pruned_but_never_the_newest(tmp_path: Path) -> None:
    for stamp in ("20261101-090000", "20261110-090000", "20261117-090000"):
        (tmp_path / f"kairos-{stamp}.dump").write_bytes(b"x")
    removed = prune(tmp_path, now=NOW, keep_days=14)
    assert [p.name for p in removed] == ["kairos-20261101-090000.dump"]
    assert [p.name for p in dumps(tmp_path)] == [
        "kairos-20261110-090000.dump",
        "kairos-20261117-090000.dump",
    ]
    assert prune(tmp_path, now=datetime(2027, 6, 1, tzinfo=UTC), keep_days=14) != []
    assert len(dumps(tmp_path)) == 1  # the newest survives any retention


def test_the_nightly_backup_is_due_after_three_local_once_a_day(tmp_path: Path) -> None:
    two_am = datetime(2026, 11, 18, 7, 0, tzinfo=UTC)  # 02:00 in Toronto
    assert not due(tmp_path, now=two_am, timezone="America/Toronto")
    assert due(tmp_path, now=NOW, timezone="America/Toronto")
    (tmp_path / "kairos-20261118-081500.dump").write_bytes(b"x")
    assert not due(tmp_path, now=NOW, timezone="America/Toronto")
