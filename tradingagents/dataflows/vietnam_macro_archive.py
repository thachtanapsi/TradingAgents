"""Point-in-time SQLite archive for official Vietnamese macro observations.

The archive intentionally stores only normalized public numeric observations and
fetch metadata.  Raw SDMX, workbook and HTML response bodies are never persisted.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat as stat_module
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

try:
    import fcntl
except ImportError:  # pragma: no cover - supported GX deployments are Unix/macOS.
    fcntl = None  # type: ignore[assignment]


ARCHIVE_SCHEMA_VERSION = 1
UTC = timezone.utc


class ArchiveConfigurationError(RuntimeError):
    """Raised when the macro archive cannot be opened without weakening safety."""


class MacroObservationLike(Protocol):
    indicator_id: str
    value: str
    unit: str
    unit_multiplier: int
    frequency: str
    period_start: datetime
    period_end: datetime
    published_at: datetime
    first_seen_at: datetime
    retrieved_at: datetime
    source_provider: str
    source_series: str | None
    source_url: str
    provisional: bool | None
    point_in_time_quality: str
    derived_from: list[str]
    stale: bool
    warnings: list[str]


def _iso(value: datetime | str | None = None) -> str:
    if value is None:
        parsed = datetime.now(UTC)
    elif isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


class VietnamMacroArchive:
    """Append-versioned archive enforcing publication and first-seen cutoffs."""

    def __init__(self, path: str | Path) -> None:
        raw = Path(os.path.abspath(str(Path(path).expanduser())))
        if os.path.lexists(raw) and raw.is_symlink():
            raise ArchiveConfigurationError("macro archive path must not be a symlink")
        self.path = raw.parent.resolve(strict=False) / raw.name
        self.archive_id = ""
        self._prepare_path()
        self._initialize()

    def _prepare_path(self) -> None:
        parent = self.path.parent
        created_parent = not parent.exists()
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.path.lexists(self.path) and self.path.is_symlink():
            raise ArchiveConfigurationError("macro archive path must not be a symlink")
        parent_stat = parent.stat()
        if not parent.is_dir() or (hasattr(os, "getuid") and parent_stat.st_uid != os.getuid()):
            raise ArchiveConfigurationError("macro archive directory is not app-owned")
        if created_parent:
            parent.chmod(0o700)
            parent_stat = parent.stat()
        if stat_module.S_IMODE(parent_stat.st_mode) & 0o077:
            raise ArchiveConfigurationError("macro archive directory permissions must be 0700")
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        elif not self.path.is_file():
            raise ArchiveConfigurationError("macro archive path is not a regular file")
        file_stat = self.path.stat()
        if (hasattr(os, "getuid") and file_stat.st_uid != os.getuid()) or file_stat.st_nlink != 1:
            raise ArchiveConfigurationError("macro archive file is not safely app-owned")
        if stat_module.S_IMODE(file_stat.st_mode) != 0o600:
            raise ArchiveConfigurationError("macro archive file permissions must be 0600")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if os.path.lexists(self.path) and self.path.is_symlink():
            raise ArchiveConfigurationError("macro archive path must not be a symlink")
        file_stat = self.path.stat()
        if (
            not stat_module.S_ISREG(file_stat.st_mode)
            or (hasattr(os, "getuid") and file_stat.st_uid != os.getuid())
            or file_stat.st_nlink != 1
            or stat_module.S_IMODE(file_stat.st_mode) != 0o600
        ):
            raise ArchiveConfigurationError("macro archive file is not safely app-owned")
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def collection_lock(self, source: str = "all") -> Iterator[bool]:
        if fcntl is None:
            yield False
            return
        safe = "".join(char for char in source.lower() if char.isalnum() or char == "_")
        path = self.path.with_name(f".{self.path.name}.{safe or 'all'}.collect.lock")
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        lock_stat = os.fstat(descriptor)
        if (
            not stat_module.S_ISREG(lock_stat.st_mode)
            or (hasattr(os, "getuid") and lock_stat.st_uid != os.getuid())
            or lock_stat.st_nlink != 1
            or stat_module.S_IMODE(lock_stat.st_mode) != 0o600
        ):
            os.close(descriptor)
            raise ArchiveConfigurationError("macro collector lock is not safely app-owned")
        acquired = False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS archive_meta (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        schema_version INTEGER NOT NULL,
                        archive_id TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS observations (
                        observation_id TEXT PRIMARY KEY,
                        indicator_id TEXT NOT NULL,
                        frequency TEXT NOT NULL,
                        period_start TEXT NOT NULL,
                        period_end TEXT NOT NULL,
                        source_provider TEXT NOT NULL,
                        source_series TEXT,
                        source_url TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS observation_versions (
                        observation_id TEXT NOT NULL,
                        version_sequence INTEGER NOT NULL,
                        version_hash TEXT NOT NULL,
                        value TEXT NOT NULL,
                        unit TEXT NOT NULL,
                        unit_multiplier INTEGER NOT NULL,
                        published_at TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        retrieved_at TEXT NOT NULL,
                        provisional INTEGER,
                        point_in_time_quality TEXT NOT NULL,
                        derived_from_json TEXT NOT NULL,
                        stale INTEGER NOT NULL DEFAULT 0,
                        warnings_json TEXT NOT NULL,
                        PRIMARY KEY(observation_id,version_sequence),
                        FOREIGN KEY(observation_id) REFERENCES observations(observation_id)
                          ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS fetch_runs (
                        fetch_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        source_url TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        http_status INTEGER,
                        observation_count INTEGER NOT NULL,
                        request_succeeded INTEGER NOT NULL,
                        etag TEXT,
                        last_modified TEXT,
                        response_hash TEXT,
                        warnings_json TEXT NOT NULL,
                        PRIMARY KEY(fetch_id,source_id)
                    );
                    CREATE TABLE IF NOT EXISTS source_cache (
                        source_url TEXT PRIMARY KEY,
                        etag TEXT,
                        last_modified TEXT,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS release_calendar (
                        provider TEXT NOT NULL,
                        reference_period TEXT NOT NULL,
                        published_at TEXT NOT NULL,
                        release_sequence INTEGER NOT NULL,
                        next_release_at TEXT,
                        source_url TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        PRIMARY KEY(provider,reference_period,published_at,release_sequence)
                    );
                    CREATE INDEX IF NOT EXISTS idx_macro_versions_pit
                      ON observation_versions(published_at,first_seen_at);
                    CREATE INDEX IF NOT EXISTS idx_macro_observations_window
                      ON observations(period_end,indicator_id);
                    CREATE INDEX IF NOT EXISTS idx_macro_fetch_runs
                      ON fetch_runs(provider,completed_at,status);
                    """
                )
                row = connection.execute(
                    "SELECT schema_version,archive_id FROM archive_meta WHERE singleton=1"
                ).fetchone()
                if row is None:
                    self.archive_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO archive_meta VALUES(1,?,?)",
                        (ARCHIVE_SCHEMA_VERSION, self.archive_id),
                    )
                else:
                    if row["schema_version"] != ARCHIVE_SCHEMA_VERSION:
                        raise ArchiveConfigurationError(
                            "unsupported Vietnam macro archive schema version"
                        )
                    self.archive_id = str(row["archive_id"])

    def cache_headers(self, source_url: str) -> dict[str, str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT etag,last_modified FROM source_cache WHERE source_url=?",
                (source_url,),
            ).fetchone()
        if row is None:
            return {}
        headers: dict[str, str] = {}
        if row["etag"]:
            headers["If-None-Match"] = str(row["etag"])
        if row["last_modified"]:
            headers["If-Modified-Since"] = str(row["last_modified"])
        return headers

    def record_fetch(self, result: Any) -> dict[str, Any]:
        observations = list(getattr(result, "observations", None) or [])
        versions_inserted = 0
        with self._connect() as connection, connection:
            for observation in observations:
                observation_id = str(getattr(observation, "observation_id", ""))
                version_hash = str(getattr(observation, "version_hash", ""))
                if not observation_id or not version_hash:
                    raise ValueError("macro observation identity/hash is required")
                first_seen = _iso(observation.first_seen_at)
                retrieved = _iso(observation.retrieved_at)
                connection.execute(
                    """
                    INSERT INTO observations(observation_id,indicator_id,frequency,period_start,
                      period_end,source_provider,source_series,source_url,first_seen_at,last_seen_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(observation_id) DO UPDATE SET
                      first_seen_at=MIN(observations.first_seen_at,excluded.first_seen_at),
                      last_seen_at=MAX(observations.last_seen_at,excluded.last_seen_at),
                      source_url=excluded.source_url
                    """,
                    (
                        observation_id,
                        observation.indicator_id,
                        observation.frequency,
                        _iso(observation.period_start),
                        _iso(observation.period_end),
                        observation.source_provider,
                        observation.source_series,
                        observation.source_url,
                        first_seen,
                        retrieved,
                    ),
                )
                latest_version = connection.execute(
                    """
                    SELECT version_sequence,version_hash FROM observation_versions
                    WHERE observation_id=? ORDER BY version_sequence DESC LIMIT 1
                    """,
                    (observation_id,),
                ).fetchone()
                if latest_version is not None and latest_version["version_hash"] == version_hash:
                    connection.execute(
                        """
                        UPDATE observation_versions SET
                          first_seen_at=MIN(first_seen_at,?),retrieved_at=MIN(retrieved_at,?),
                          warnings_json=?
                        WHERE observation_id=? AND version_sequence=?
                        """,
                        (
                            first_seen,
                            retrieved,
                            json.dumps(
                                [str(item)[:500] for item in observation.warnings],
                                ensure_ascii=False,
                            ),
                            observation_id,
                            int(latest_version["version_sequence"]),
                        ),
                    )
                else:
                    version_sequence = (
                        int(latest_version["version_sequence"]) + 1
                        if latest_version is not None
                        else 1
                    )
                    connection.execute(
                        """
                        INSERT INTO observation_versions(observation_id,version_sequence,
                          version_hash,value,unit,unit_multiplier,published_at,first_seen_at,
                          retrieved_at,provisional,point_in_time_quality,derived_from_json,
                          stale,warnings_json)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            observation_id,
                            version_sequence,
                            version_hash,
                            observation.value,
                            observation.unit,
                            observation.unit_multiplier,
                            _iso(observation.published_at),
                            first_seen,
                            retrieved,
                            None
                            if observation.provisional is None
                            else int(observation.provisional),
                            observation.point_in_time_quality,
                            json.dumps(observation.derived_from, ensure_ascii=False),
                            int(bool(getattr(observation, "stale", False))),
                            json.dumps(
                                [str(item)[:500] for item in observation.warnings],
                                ensure_ascii=False,
                            ),
                        ),
                    )
                    versions_inserted += 1

            for release in list(getattr(result, "releases", None) or []):
                identity = (
                    release["provider"],
                    release["reference_period"],
                    _iso(release["published_at"]),
                )
                next_release = (
                    _iso(release["next_release_at"]) if release.get("next_release_at") else None
                )
                latest_release = connection.execute(
                    """
                    SELECT release_sequence,next_release_at,source_url FROM release_calendar
                    WHERE provider=? AND reference_period=? AND published_at=?
                    ORDER BY release_sequence DESC LIMIT 1
                    """,
                    identity,
                ).fetchone()
                if (
                    latest_release is None
                    or latest_release["next_release_at"] != next_release
                    or latest_release["source_url"] != release["source_url"]
                ):
                    sequence = (
                        int(latest_release["release_sequence"]) + 1
                        if latest_release is not None
                        else 1
                    )
                    connection.execute(
                        """
                        INSERT INTO release_calendar(provider,reference_period,published_at,
                          release_sequence,next_release_at,source_url,first_seen_at)
                        VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            *identity,
                            sequence,
                            next_release,
                            release["source_url"],
                            _iso(release.get("first_seen_at") or result.completed_at),
                        ),
                    )

            status = getattr(result.status, "value", result.status)
            connection.execute(
                """
                INSERT INTO fetch_runs(fetch_id,provider,source_id,source_url,started_at,
                  completed_at,status,http_status,observation_count,request_succeeded,etag,
                  last_modified,response_hash,warnings_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    result.fetch_id,
                    result.provider,
                    result.source_id,
                    result.source_url,
                    _iso(result.started_at),
                    _iso(result.completed_at),
                    status,
                    result.http_status,
                    len(observations),
                    int(result.request_succeeded),
                    result.etag,
                    result.last_modified,
                    result.response_hash,
                    json.dumps([str(item)[:500] for item in result.warnings], ensure_ascii=False),
                ),
            )
            if result.request_succeeded:
                connection.execute(
                    """
                    INSERT INTO source_cache(source_url,etag,last_modified,updated_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(source_url) DO UPDATE SET
                      etag=COALESCE(excluded.etag,source_cache.etag),
                      last_modified=COALESCE(excluded.last_modified,source_cache.last_modified),
                      updated_at=excluded.updated_at
                    """,
                    (
                        result.source_url,
                        result.etag,
                        result.last_modified,
                        _iso(result.completed_at),
                    ),
                )
        return {
            "fetch_id": result.fetch_id,
            "provider": result.provider,
            "source_id": result.source_id,
            "source_url": result.source_url,
            "status": status,
            "observations_seen": len(observations),
            "versions_inserted": versions_inserted,
            "warnings": list(result.warnings),
        }

    def observations_for_window(
        self,
        *,
        start: datetime,
        as_of: datetime,
        observation_factory: Any,
    ) -> list[Any]:
        """Return the newest eligible revision of every observation identity."""
        query = """
        WITH eligible AS (
          SELECT o.observation_id,o.indicator_id,o.frequency,o.period_start,o.period_end,
            o.source_provider,o.source_series,o.source_url,
            v.version_sequence,v.version_hash,v.value,v.unit,v.unit_multiplier,v.published_at,
            v.first_seen_at AS version_first_seen_at,
            v.retrieved_at AS version_retrieved_at,
            v.provisional,v.point_in_time_quality,v.derived_from_json,v.stale,
            v.warnings_json,ROW_NUMBER() OVER (
            PARTITION BY o.observation_id
            ORDER BY v.first_seen_at DESC,v.version_sequence DESC
          ) AS revision_rank
          FROM observations o
          JOIN observation_versions v USING(observation_id)
          WHERE o.period_end>=? AND o.period_end<=?
            AND v.published_at<=? AND v.first_seen_at<=?
        )
        SELECT * FROM eligible WHERE revision_rank=1
        ORDER BY period_end,indicator_id,source_provider
        """
        with self._connect() as connection:
            rows = connection.execute(
                query,
                (_iso(start), _iso(as_of), _iso(as_of), _iso(as_of)),
            ).fetchall()
        output = []
        for row in rows:
            provisional = row["provisional"]
            output.append(
                observation_factory(
                    indicator_id=str(row["indicator_id"]),
                    value=str(row["value"]),
                    unit=str(row["unit"]),
                    unit_multiplier=int(row["unit_multiplier"]),
                    frequency=str(row["frequency"]),
                    period_start=datetime.fromisoformat(row["period_start"]),
                    period_end=datetime.fromisoformat(row["period_end"]),
                    published_at=datetime.fromisoformat(row["published_at"]),
                    first_seen_at=datetime.fromisoformat(row["version_first_seen_at"]),
                    retrieved_at=datetime.fromisoformat(row["version_retrieved_at"]),
                    source_provider=str(row["source_provider"]),
                    source_series=str(row["source_series"])
                    if row["source_series"] is not None
                    else None,
                    source_url=str(row["source_url"]),
                    provisional=None if provisional is None else bool(provisional),
                    point_in_time_quality=str(row["point_in_time_quality"]),
                    derived_from=list(json.loads(row["derived_from_json"])),
                    stale=bool(row["stale"]),
                    warnings=list(json.loads(row["warnings_json"])),
                )
            )
        return output

    def source_results(
        self, providers: tuple[str, ...], *, as_of: datetime
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        with self._connect() as connection:
            for provider in providers:
                rows = connection.execute(
                    """
                    SELECT * FROM fetch_runs WHERE provider=? AND completed_at<=?
                    ORDER BY completed_at DESC
                    """,
                    (provider, _iso(as_of)),
                ).fetchall()
                if not rows:
                    results.append(
                        {
                            "provider": provider,
                            "source_provider": provider,
                            "status": "unavailable",
                            "fetch_id": None,
                            "fetch_ids": [],
                            "observation_count": 0,
                            "point_in_time_quality": "partial",
                            "warnings": [f"No archived {provider} collection is available."],
                        }
                    )
                    continue
                latest_by_source: dict[str, sqlite3.Row] = {}
                for row in rows:
                    latest_by_source.setdefault(str(row["source_id"]), row)
                selected = list(latest_by_source.values())
                evidence_rows: dict[str, sqlite3.Row] = {}
                for source_id, latest in latest_by_source.items():
                    baseline = next(
                        (
                            row
                            for row in rows
                            if str(row["source_id"]) == source_id
                            and str(row["source_url"]) == str(latest["source_url"])
                            and int(row["observation_count"]) > 0
                            and bool(row["request_succeeded"])
                        ),
                        None,
                    )
                    if baseline is not None:
                        evidence_rows[source_id] = baseline
                successes = [row for row in selected if bool(row["request_succeeded"])]
                warnings = [
                    str(item)
                    for row in [*selected, *evidence_rows.values()]
                    for item in json.loads(row["warnings_json"])
                ]
                if not successes and not evidence_rows:
                    status = "unavailable"
                elif (
                    len(successes) != len(selected)
                    or any(str(row["status"]) != "available" for row in selected)
                    or any(str(row["status"]) != "available" for row in evidence_rows.values())
                ):
                    status = "partial"
                else:
                    status = "available"
                results.append(
                    {
                        "provider": provider,
                        "source_provider": provider,
                        "status": status,
                        "fetch_id": str(selected[0]["fetch_id"]),
                        "fetch_ids": list(
                            dict.fromkeys(
                                str(row["fetch_id"]) for row in [*selected, *evidence_rows.values()]
                            )
                        ),
                        "last_completed_at": str(max(row["completed_at"] for row in selected)),
                        "last_successful_at": str(
                            max(
                                row["completed_at"]
                                for row in rows
                                if bool(row["request_succeeded"])
                            )
                        )
                        if any(bool(row["request_succeeded"]) for row in rows)
                        else None,
                        "observation_count": sum(
                            int(row["observation_count"]) for row in evidence_rows.values()
                        ),
                        "point_in_time_quality": "proxy" if status == "available" else "partial",
                        "warnings": list(dict.fromkeys(warnings)),
                    }
                )
        return results

    def source_health(self, provider: str, source_url: str, *, as_of: datetime) -> dict[str, Any]:
        """Return endpoint-specific fetch health as it was known at ``as_of``."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM fetch_runs WHERE provider=? AND source_url=?
                  AND completed_at<=? ORDER BY completed_at DESC
                """,
                (provider, source_url, _iso(as_of)),
            ).fetchall()
        if not rows:
            return {
                "provider": provider,
                "source_url": source_url,
                "status": "unavailable",
                "last_successful_at": None,
                "has_evidence": False,
                "access_blocked": False,
                "warnings": ["No endpoint-specific collection is archived."],
            }
        latest = rows[0]
        successful = next((row for row in rows if bool(row["request_succeeded"])), None)
        evidence = next(
            (
                row
                for row in rows
                if bool(row["request_succeeded"]) and int(row["observation_count"]) > 0
            ),
            None,
        )
        warning_rows = [latest]
        if evidence is not None and evidence is not latest:
            warning_rows.append(evidence)
        warnings = list(
            dict.fromkeys(
                str(warning) for row in warning_rows for warning in json.loads(row["warnings_json"])
            )
        )
        access_blocked = any(
            marker in warning
            for warning in warnings
            for marker in (
                "WAF/authorization",
                "HTTP 403",
                "HTTP 429",
                "retrieval failed",
            )
        )
        if bool(latest["request_succeeded"]):
            status = str(latest["status"])
        elif evidence is not None:
            status = "partial"
        else:
            status = "unavailable"
        return {
            "provider": provider,
            "source_url": source_url,
            "status": status,
            "fetch_id": str(latest["fetch_id"]),
            "last_completed_at": str(latest["completed_at"]),
            "last_successful_at": str(successful["completed_at"])
            if successful is not None
            else None,
            "has_evidence": evidence is not None,
            "access_blocked": access_blocked,
            "warnings": warnings,
        }

    def latest_release(self, provider: str, *, as_of: datetime) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM release_calendar WHERE provider=? AND published_at<=?
                  AND first_seen_at<=?
                ORDER BY published_at DESC,first_seen_at DESC,release_sequence DESC LIMIT 1
                """,
                (provider, _iso(as_of), _iso(as_of)),
            ).fetchone()
        return dict(row) if row is not None else None

    def release_for_period(
        self, provider: str, reference_period: str, *, as_of: datetime
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM release_calendar WHERE provider=? AND reference_period=?
                  AND published_at<=? AND first_seen_at<=?
                ORDER BY published_at DESC,first_seen_at DESC,release_sequence DESC LIMIT 1
                """,
                (provider, reference_period, _iso(as_of), _iso(as_of)),
            ).fetchone()
        return dict(row) if row is not None else None

    def fetch_run_count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM fetch_runs").fetchone()[0])


__all__ = ["ARCHIVE_SCHEMA_VERSION", "ArchiveConfigurationError", "VietnamMacroArchive"]
