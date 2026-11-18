"""Nightly local backups of the Postgres database (ADR-015).

``pg_dump`` runs inside the Postgres container by default (``docker compose exec``),
so no Postgres client tools are needed on the host. Dumps use the custom format
(compressed, restorable with ``pg_restore``), are written to a temporary file and
renamed only when complete, and are kept for ``backup_keep_days``. The directory
defaults to ``~/Backups/kairos``: outside the repository, which may be iCloud-synced.

In AWS (M8) database snapshots replace this.
"""

import asyncio
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog

log = structlog.get_logger(__name__)

COMPOSE = ["docker", "compose", "-f", "infra/compose.yaml", "exec", "-T", "postgres"]
DEFAULT_DUMP = [*COMPOSE, "pg_dump", "-U", "kairos", "-Fc", "kairos"]
DEFAULT_RESTORE = [*COMPOSE, "pg_restore", "-U", "kairos", "-d", "kairos", "--clean", "--if-exists"]
PREFIX = "kairos-"
SUFFIX = ".dump"
NIGHTLY_AFTER = time(3, 0)


class BackupError(Exception):
    pass


def default_dir() -> Path:
    return Path.home() / "Backups" / "kairos"


async def _run(command: Sequence[str], *, stdin: bytes | None = None) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate(stdin)
    if process.returncode != 0:
        detail = err.decode(errors="replace").strip()[:500]
        raise BackupError(f"{command[0]} exited {process.returncode}: {detail}")
    return out


async def backup(
    directory: Path, *, now: datetime, command: Sequence[str] = DEFAULT_DUMP, keep_days: int = 14
) -> Path:
    """Dump the database to ``directory`` and prune old dumps. Returns the new file."""
    target = directory / f"{PREFIX}{now:%Y%m%d-%H%M%S}{SUFFIX}"
    data = await _run(command)
    if not data:
        raise BackupError("pg_dump produced no output")
    await asyncio.to_thread(_write_atomically, target, data)
    pruned = await asyncio.to_thread(prune, directory, now=now, keep_days=keep_days)
    for old in pruned:
        log.info("backup_pruned", file=old.name)
    log.info("backup_written", file=str(target), bytes=len(data))
    return target


async def restore(dump: Path, *, command: Sequence[str] = DEFAULT_RESTORE) -> None:
    """Restore a dump over the current database (``--clean``: existing objects are
    dropped and recreated). Stop the API and worker first."""
    data = await asyncio.to_thread(_read, dump)
    await _run(command, stdin=data)


def _write_atomically(target: Path, data: bytes) -> None:
    """Write next to the target, then rename: a crash never leaves a truncated dump."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".partial")
    partial.write_bytes(data)
    partial.replace(target)


def _read(dump: Path) -> bytes:
    if not dump.is_file():
        raise BackupError(f"no such dump: {dump}")
    return dump.read_bytes()


def dumps(directory: Path) -> list[Path]:
    """Completed dumps, oldest first."""
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{PREFIX}*{SUFFIX}"))


def prune(directory: Path, *, now: datetime, keep_days: int) -> list[Path]:
    """Delete dumps older than ``keep_days`` (by the time in their name). Never the newest."""
    cutoff = now - timedelta(days=keep_days)
    removed: list[Path] = []
    for path in dumps(directory)[:-1]:
        stamp = path.name.removeprefix(PREFIX).removesuffix(SUFFIX)
        try:
            taken = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=now.tzinfo)
        except ValueError:
            continue  # not ours
        if taken < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def due(directory: Path, *, now: datetime, timezone: str) -> bool:
    """A nightly backup is due once it is past 03:00 local and none exists for today."""
    local = now.astimezone(ZoneInfo(timezone))
    if local.time() < NIGHTLY_AFTER:
        return False
    today = f"{PREFIX}{local.astimezone(now.tzinfo):%Y%m%d}"
    return not any(path.name.startswith(today) for path in dumps(directory))


async def nightly(
    directory: Path,
    stop: asyncio.Event,
    *,
    now: Callable[[], datetime],
    timezone: str,
    command: Sequence[str] = DEFAULT_DUMP,
    keep_days: int = 14,
    check_seconds: float = 3600.0,
) -> None:
    """Check hourly; take the day's backup when due. A machine that was asleep at 03:00
    catches up when it wakes. Failures are logged and retried at the next check."""
    while not stop.is_set():
        if await asyncio.to_thread(due, directory, now=now(), timezone=timezone):
            try:
                await backup(directory, now=now(), command=command, keep_days=keep_days)
            except (BackupError, OSError) as error:
                log.warning("backup_failed", error=str(error))
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=check_seconds)
