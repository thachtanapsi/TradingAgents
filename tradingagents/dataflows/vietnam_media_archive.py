"""Encrypted point-in-time archive for Vietnamese editorial RSS evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat as stat_module
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

try:
    import fcntl
except ImportError:  # pragma: no cover - supported deployment is Unix/macOS.
    fcntl = None  # type: ignore[assignment]

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - guarded at construction time.
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment,misc]


ARCHIVE_SCHEMA_VERSION = 1
UTC = timezone.utc


class ArchiveConfigurationError(RuntimeError):
    """Raised when the media archive cannot be opened without weakening safety."""


class MediaArticleLike(Protocol):
    provider: str
    provider_article_id: str
    title: str
    summary: str
    canonical_url: str
    published_at: datetime
    first_seen_at: datetime
    retrieved_at: datetime
    category: str
    matched_tickers: list[str]
    relevance_reasons: list[str]
    content_hash: str


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


class VietnamMediaArchive:
    """SQLite archive whose raw RSS fields are always Fernet-encrypted."""

    def __init__(self, path: str | Path, encryption_key: str) -> None:
        if Fernet is None:
            raise ArchiveConfigurationError(
                "Vietnam media encryption support is not installed; "
                "install the 'vn-media' optional dependency"
            )
        if not encryption_key:
            raise ArchiveConfigurationError("VN_MEDIA_ARCHIVE_ENCRYPTION_KEY is required")
        try:
            self._fernet = Fernet(encryption_key.encode("ascii"))
        except (TypeError, ValueError):
            raise ArchiveConfigurationError(
                "VN_MEDIA_ARCHIVE_ENCRYPTION_KEY is not a valid Fernet key"
            ) from None
        raw = Path(os.path.abspath(str(Path(path).expanduser())))
        if os.path.lexists(raw) and raw.is_symlink():
            raise ArchiveConfigurationError("media archive path must not be a symlink")
        self.path = raw.parent.resolve(strict=False) / raw.name
        self._hmac_key = hashlib.sha256(encryption_key.encode("ascii")).digest()
        self.archive_id = ""
        self._prepare_path()
        self._initialize()

    def _prepare_path(self) -> None:
        parent = self.path.parent
        created_parent = not parent.exists()
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.path.lexists(self.path) and self.path.is_symlink():
            raise ArchiveConfigurationError("media archive path must not be a symlink")
        parent_stat = parent.stat()
        if not parent.is_dir() or (
            hasattr(os, "getuid") and parent_stat.st_uid != os.getuid()
        ):
            raise ArchiveConfigurationError("media archive directory is not app-owned")
        if created_parent:
            parent.chmod(0o700)
            parent_stat = parent.stat()
        if stat_module.S_IMODE(parent_stat.st_mode) & 0o077:
            raise ArchiveConfigurationError(
                "media archive directory permissions must be 0700"
            )
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        elif not self.path.is_file():
            raise ArchiveConfigurationError("media archive path is not a regular file")
        file_stat = self.path.stat()
        if (
            (hasattr(os, "getuid") and file_stat.st_uid != os.getuid())
            or file_stat.st_nlink != 1
        ):
            raise ArchiveConfigurationError("media archive file is not safely app-owned")
        if stat_module.S_IMODE(file_stat.st_mode) != 0o600:
            raise ArchiveConfigurationError("media archive file permissions must be 0600")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if os.path.lexists(self.path) and self.path.is_symlink():
            raise ArchiveConfigurationError("media archive path must not be a symlink")
        file_stat = self.path.stat()
        if (
            not stat_module.S_ISREG(file_stat.st_mode)
            or (hasattr(os, "getuid") and file_stat.st_uid != os.getuid())
            or file_stat.st_nlink != 1
            or stat_module.S_IMODE(file_stat.st_mode) != 0o600
        ):
            raise ArchiveConfigurationError("media archive file is not safely app-owned")
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def collection_lock(self, ticker: str) -> Iterator[bool]:
        if fcntl is None:
            yield False
            return
        safe = "".join(char for char in ticker.upper() if char.isalnum()) or "ALL"
        path = self.path.with_name(f".{self.path.name}.{safe}.collect.lock")
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
            raise ArchiveConfigurationError("media collector lock is not safely app-owned")
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
        verifier = hmac.new(self._hmac_key, b"vn-media-archive", hashlib.sha256).hexdigest()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA secure_delete=ON")
            with connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS archive_meta (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        schema_version INTEGER NOT NULL,
                        archive_id TEXT NOT NULL,
                        key_verifier TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS articles (
                        provider TEXT NOT NULL,
                        article_key TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        published_at TEXT NOT NULL,
                        current_content_hash TEXT NOT NULL,
                        PRIMARY KEY(provider, article_key)
                    );
                    CREATE TABLE IF NOT EXISTS article_versions (
                        provider TEXT NOT NULL,
                        article_key TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        published_at TEXT NOT NULL,
                        payload_ciphertext BLOB,
                        PRIMARY KEY(provider, article_key, content_hash),
                        FOREIGN KEY(provider, article_key)
                          REFERENCES articles(provider, article_key) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS article_tickers (
                        provider TEXT NOT NULL,
                        article_key TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        alias_policy_version TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        reasons_json TEXT NOT NULL,
                        PRIMARY KEY(provider, article_key, ticker, alias_policy_version),
                        FOREIGN KEY(provider, article_key)
                          REFERENCES articles(provider, article_key) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS feed_runs (
                        fetch_id TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        feed_url TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        http_status INTEGER,
                        article_count INTEGER NOT NULL,
                        request_succeeded INTEGER NOT NULL,
                        etag TEXT,
                        last_modified TEXT,
                        warnings_json TEXT NOT NULL,
                        PRIMARY KEY(fetch_id, feed_url)
                    );
                    CREATE TABLE IF NOT EXISTS feed_cache (
                        feed_url TEXT PRIMARY KEY,
                        etag TEXT,
                        last_modified TEXT,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS purged_identities (
                        identity_hmac TEXT PRIMARY KEY,
                        purged_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_versions_window
                      ON article_versions(published_at, first_seen_at);
                    CREATE INDEX IF NOT EXISTS idx_feed_runs_coverage
                      ON feed_runs(ticker, provider, completed_at);
                    """
                )
                row = connection.execute(
                    "SELECT schema_version,archive_id,key_verifier FROM archive_meta WHERE singleton=1"
                ).fetchone()
                if row is None:
                    self.archive_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO archive_meta VALUES(1,?,?,?)",
                        (ARCHIVE_SCHEMA_VERSION, self.archive_id, verifier),
                    )
                else:
                    if row["schema_version"] != ARCHIVE_SCHEMA_VERSION:
                        raise ArchiveConfigurationError("unsupported media archive schema version")
                    if not hmac.compare_digest(str(row["key_verifier"]), verifier):
                        raise ArchiveConfigurationError(
                            "media archive cannot be opened with the configured key"
                        )
                    self.archive_id = str(row["archive_id"])

    def _article_key(self, provider: str, article_id: str) -> str:
        return hmac.new(
            self._hmac_key, f"{provider}\0{article_id}".encode(), hashlib.sha256
        ).hexdigest()

    def _identity_hmac(self, provider: str, article_id: str, content_hash: str) -> str:
        return hmac.new(
            self._hmac_key,
            f"{provider}\0{article_id}\0{content_hash}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _encrypt(self, value: dict[str, Any]) -> bytes:
        return self._fernet.encrypt(
            json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )

    def _decrypt(self, value: bytes) -> dict[str, Any]:
        try:
            decoded = json.loads(self._fernet.decrypt(value).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError):
            raise ArchiveConfigurationError(
                "media archive content cannot be decrypted with the configured key"
            ) from None
        if not isinstance(decoded, dict):
            raise ArchiveConfigurationError("media archive contains malformed content")
        return decoded

    def feed_cache_headers(self, feed_url: str) -> dict[str, str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT etag,last_modified FROM feed_cache WHERE feed_url=?", (feed_url,)
            ).fetchone()
        if row is None:
            return {}
        headers = {}
        if row["etag"]:
            headers["If-None-Match"] = str(row["etag"])
        if row["last_modified"]:
            headers["If-Modified-Since"] = str(row["last_modified"])
        return headers

    def record_fetch(self, result: Any) -> dict[str, Any]:
        articles = list(getattr(result, "articles", None) or [])
        provider = str(result.provider)
        ticker = str(result.ticker).upper()
        feed_url = str(result.feed_url)
        inserted = 0
        retention_skips = 0
        with self._connect() as connection, connection:
            for article in articles:
                article_key = self._article_key(provider, str(article.provider_article_id))
                identity = self._identity_hmac(
                    provider, str(article.provider_article_id), str(article.content_hash)
                )
                if connection.execute(
                    "SELECT 1 FROM purged_identities WHERE identity_hmac=?", (identity,)
                ).fetchone():
                    retention_skips += 1
                    continue
                first_seen = _iso(article.first_seen_at)
                retrieved = _iso(article.retrieved_at)
                published = _iso(article.published_at)
                connection.execute(
                    """
                    INSERT INTO articles(provider,article_key,first_seen_at,last_seen_at,
                                         published_at,current_content_hash)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(provider,article_key) DO UPDATE SET
                      first_seen_at=MIN(articles.first_seen_at,excluded.first_seen_at),
                      last_seen_at=MAX(articles.last_seen_at,excluded.last_seen_at),
                      published_at=MIN(articles.published_at,excluded.published_at),
                      current_content_hash=CASE WHEN excluded.last_seen_at>=articles.last_seen_at
                        THEN excluded.current_content_hash ELSE articles.current_content_hash END
                    """,
                    (
                        provider,
                        article_key,
                        first_seen,
                        retrieved,
                        published,
                        article.content_hash,
                    ),
                )
                existed = connection.execute(
                    "SELECT 1 FROM article_versions WHERE provider=? AND article_key=? AND content_hash=?",
                    (provider, article_key, article.content_hash),
                ).fetchone()
                payload = {
                    "provider_article_id": article.provider_article_id,
                    "title": article.title,
                    "summary": article.summary,
                    "canonical_url": article.canonical_url,
                    "category": article.category,
                }
                connection.execute(
                    """
                    INSERT INTO article_versions(provider,article_key,content_hash,first_seen_at,
                      last_seen_at,published_at,payload_ciphertext)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(provider,article_key,content_hash) DO UPDATE SET
                      first_seen_at=MIN(article_versions.first_seen_at,excluded.first_seen_at),
                      last_seen_at=MAX(article_versions.last_seen_at,excluded.last_seen_at)
                    """,
                    (
                        provider,
                        article_key,
                        article.content_hash,
                        first_seen,
                        retrieved,
                        published,
                        self._encrypt(payload),
                    ),
                )
                inserted += int(existed is None)
                for symbol in article.matched_tickers:
                    connection.execute(
                        """
                        INSERT INTO article_tickers(provider,article_key,ticker,
                          alias_policy_version,first_seen_at,last_seen_at,reasons_json)
                        VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(provider,article_key,ticker,alias_policy_version)
                        DO UPDATE SET
                          first_seen_at=MIN(article_tickers.first_seen_at,excluded.first_seen_at),
                          last_seen_at=MAX(article_tickers.last_seen_at,excluded.last_seen_at),
                          reasons_json=excluded.reasons_json
                        """,
                        (
                            provider,
                            article_key,
                            symbol.upper(),
                            str(
                                getattr(
                                    result, "alias_policy_version", "vn-media-alias-v2"
                                )
                            ),
                            first_seen,
                            retrieved,
                            json.dumps(article.relevance_reasons, ensure_ascii=False),
                        ),
                    )
            etag = getattr(result, "etag", None)
            modified = getattr(result, "last_modified", None)
            connection.execute(
                """
                INSERT INTO feed_runs(fetch_id,ticker,provider,feed_url,started_at,completed_at,
                  status,http_status,article_count,request_succeeded,etag,last_modified,warnings_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    result.fetch_id,
                    ticker,
                    provider,
                    feed_url,
                    _iso(result.started_at),
                    _iso(result.completed_at),
                    getattr(result.status, "value", result.status),
                    result.http_status,
                    len(articles),
                    int(result.request_succeeded),
                    etag,
                    modified,
                    json.dumps([str(item)[:500] for item in result.warnings], ensure_ascii=False),
                ),
            )
            if result.request_succeeded:
                connection.execute(
                    """
                    INSERT INTO feed_cache(feed_url,etag,last_modified,updated_at) VALUES(?,?,?,?)
                    ON CONFLICT(feed_url) DO UPDATE SET etag=COALESCE(excluded.etag,feed_cache.etag),
                      last_modified=COALESCE(excluded.last_modified,feed_cache.last_modified),
                      updated_at=excluded.updated_at
                    """,
                    (feed_url, etag, modified, _iso(result.completed_at)),
                )
        return {
            "fetch_id": result.fetch_id,
            "ticker": ticker,
            "provider": provider,
            "feed_url": feed_url,
            "status": getattr(result.status, "value", result.status),
            "articles_seen": len(articles),
            "versions_inserted": inserted,
            "retention_skips": retention_skips,
            "warnings": list(result.warnings),
        }

    def articles_for_window(
        self,
        *,
        start: datetime,
        as_of: datetime,
        article_factory: Any,
        ticker: str | None = None,
        alias_policy_version: str = "vn-media-alias-v2",
        include_categories: set[str] | None = None,
    ) -> list[Any]:
        query = """
        WITH eligible AS (
          SELECT v.*, ROW_NUMBER() OVER (
            PARTITION BY v.provider,v.article_key
            ORDER BY v.first_seen_at DESC,v.content_hash DESC
          ) revision_rank
          FROM article_versions v
          WHERE v.published_at>=? AND v.published_at<=? AND v.first_seen_at<=?
            AND v.payload_ciphertext IS NOT NULL
        )
        SELECT * FROM eligible WHERE revision_rank=1
        ORDER BY published_at DESC,provider,article_key
        """
        with self._connect() as connection:
            rows = connection.execute(query, (_iso(start), _iso(as_of), _iso(as_of))).fetchall()
            mapping_rows = connection.execute(
                """
                SELECT provider,article_key,ticker,reasons_json
                FROM article_tickers
                WHERE alias_policy_version=? AND first_seen_at<=?
                ORDER BY provider,article_key,ticker
                """,
                (alias_policy_version, _iso(as_of)),
            ).fetchall()
        mappings: dict[tuple[str, str], list[tuple[str, list[str]]]] = {}
        for mapping in mapping_rows:
            mappings.setdefault(
                (str(mapping["provider"]), str(mapping["article_key"])), []
            ).append(
                (str(mapping["ticker"]), list(json.loads(mapping["reasons_json"])))
            )
        output = []
        for row in rows:
            payload = self._decrypt(row["payload_ciphertext"])
            mapped = mappings.get((str(row["provider"]), str(row["article_key"])), [])
            matched_tickers = [item[0] for item in mapped]
            category = str(payload["category"])
            requested_ticker = ticker.upper() if ticker else None
            direct_match = requested_ticker is None or requested_ticker in matched_tickers
            market_context = bool(include_categories and category in include_categories)
            if not direct_match and not market_context:
                continue
            reasons = [reason for _symbol, values in mapped for reason in values]
            if market_context and not direct_match:
                reasons.append("market_context_category")
            output.append(
                article_factory(
                    provider=row["provider"],
                    provider_article_id=str(payload["provider_article_id"]),
                    title=str(payload["title"]),
                    summary=str(payload["summary"]),
                    canonical_url=str(payload["canonical_url"]),
                    published_at=datetime.fromisoformat(row["published_at"]),
                    first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
                    retrieved_at=datetime.fromisoformat(row["last_seen_at"]),
                    category=category,
                    matched_tickers=matched_tickers,
                    relevance_reasons=list(dict.fromkeys(reasons)),
                    content_hash=row["content_hash"],
                )
            )
        return output

    def coverage_for_window(
        self,
        ticker: str,
        provider: str,
        *,
        start: datetime,
        as_of: datetime,
        poll_seconds: int,
        expected_feed_urls: list[str] | tuple[str, ...] | None = None,
    ) -> tuple[bool, list[str], str | None]:
        grace = poll_seconds * 3
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM feed_runs WHERE ticker=? AND provider=?
                  AND completed_at>=? AND completed_at<=? AND request_succeeded=1
                ORDER BY completed_at
                """,
                (ticker.upper(), provider, _iso(start - timedelta(seconds=grace)), _iso(as_of)),
            ).fetchall()
        if not rows:
            return False, [f"No successful {provider} collection covers this window."], None
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["feed_url"]), []).append(row)
        expected = list(expected_feed_urls or grouped)
        complete = True
        warnings: list[str] = []
        latest_row = rows[-1]
        for feed_url in expected:
            feed_rows = grouped.get(feed_url, [])
            if not feed_rows:
                complete = False
                warnings.append(f"{provider} has no successful collection for a configured feed.")
                continue
            times = [datetime.fromisoformat(row["completed_at"]) for row in feed_rows]
            if times[0] > start.astimezone(UTC) + timedelta(seconds=grace):
                complete = False
                warnings.append(f"{provider} collection began after the requested window.")
            if times[-1] < as_of.astimezone(UTC) - timedelta(seconds=grace):
                complete = False
                warnings.append(f"{provider} collection does not reach the as-of time.")
            if any(
                later - earlier > timedelta(seconds=grace)
                for earlier, later in zip(times, times[1:], strict=False)
            ):
                complete = False
                warnings.append(f"{provider} archive contains a polling gap.")
            if str(feed_rows[-1]["completed_at"]) > str(latest_row["completed_at"]):
                latest_row = feed_rows[-1]
        return complete, list(dict.fromkeys(warnings)), str(latest_row["fetch_id"])

    def purge_expired(
        self, *, retention_days: int, now: datetime | None = None
    ) -> dict[str, int]:
        current = now or datetime.now(UTC)
        cutoff = _iso(current - timedelta(days=retention_days))
        with self._connect() as connection, connection:
            rows = connection.execute(
                """
                SELECT v.provider,v.article_key,v.content_hash,v.payload_ciphertext
                FROM article_versions v WHERE v.last_seen_at<? AND v.payload_ciphertext IS NOT NULL
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                payload = self._decrypt(row["payload_ciphertext"])
                identity = self._identity_hmac(
                    row["provider"], str(payload["provider_article_id"]), row["content_hash"]
                )
                connection.execute(
                    "INSERT OR IGNORE INTO purged_identities VALUES(?,?)",
                    (identity, _iso(current)),
                )
            connection.execute(
                "DELETE FROM article_versions WHERE last_seen_at<?", (cutoff,)
            )
            connection.execute(
                "DELETE FROM articles WHERE NOT EXISTS(SELECT 1 FROM article_versions v WHERE "
                "v.provider=articles.provider AND v.article_key=articles.article_key)"
            )
        with self._connect() as connection:
            with suppress(sqlite3.DatabaseError):
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            with suppress(sqlite3.DatabaseError):
                connection.execute("VACUUM")
        return {"versions_purged": len(rows), "aggregate_runs_retained": self.fetch_run_count()}

    def fetch_run_count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM feed_runs").fetchone()[0])


__all__ = ["ARCHIVE_SCHEMA_VERSION", "ArchiveConfigurationError", "VietnamMediaArchive"]
