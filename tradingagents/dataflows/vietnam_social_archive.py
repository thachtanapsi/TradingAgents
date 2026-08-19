"""Encrypted, append-only SQLite archive for Vietnam retail-social evidence.

Only ciphertext is persisted for raw post text and provider author fields.  The
remaining columns are operational metadata needed for point-in-time filtering,
deduplication, coverage checks, and anonymous daily snapshots.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows is not a supported deployment target yet.
    fcntl = None  # type: ignore[assignment]

try:  # Optional dependency; importing TradingAgents must remain lightweight.
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - exercised through the guarded factory
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment,misc]


ARCHIVE_SCHEMA_VERSION = 2
UTC = timezone.utc


class ArchiveConfigurationError(RuntimeError):
    """Raised when the encrypted archive cannot be opened safely."""


class SocialPostLike(Protocol):
    provider: str
    provider_post_id: str
    ticker: str
    text: str
    published_at: datetime
    first_seen_at: datetime
    provider_sentiment: int | None
    engagement: dict[str, Any]
    author: dict[str, Any]
    tagged_symbols: list[str]
    content_hash: str
    is_ai_generated: bool


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: str
    ticker: str
    analysis_date: str
    status: str
    report_status: str
    signal_payload: dict[str, Any]
    report_payload: dict[str, Any]
    model_profile: str
    prompt_version: str
    fingerprint: str
    profile_fingerprint: str
    statistics: dict[str, Any]
    created_at: str
    created: bool = True


@dataclass(frozen=True)
class SnapshotClaim:
    acquired: bool
    owner_id: str
    ticker: str
    analysis_date: str
    prompt_version: str
    profile_fingerprint: str


def _iso_utc(value: datetime | str | None = None) -> str:
    if value is None:
        parsed = datetime.now(UTC)
    elif isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    if isinstance(value, datetime):
        return _iso_utc(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


_SENSITIVE_SNAPSHOT_KEYS = {
    "author",
    "author_id",
    "author_key",
    "bio",
    "content",
    "full_name",
    "name",
    "post_id",
    "posts",
    "provider_post_id",
    "raw_content",
    "raw_posts",
    "text",
    "user",
    "user_id",
    "username",
}


def _redact_snapshot_payload(value: Any) -> Any:
    """Drop structured raw-content/identity fields from durable aggregates.

    This is a defence in depth, not a DLP system: free-form narrative can still
    contain a name or quote supplied by an LLM. Callers must continue to avoid
    putting raw posts or identities in snapshot narratives.
    """
    normalized = _jsonable(value)
    if isinstance(normalized, dict):
        return {
            str(key): _redact_snapshot_payload(item)
            for key, item in normalized.items()
            if str(key).strip().lower() not in _SENSITIVE_SNAPSHOT_KEYS
        }
    if isinstance(normalized, list):
        return [_redact_snapshot_payload(item) for item in normalized]
    return normalized


class VietnamSocialArchive:
    """Small encrypted SQLite store with immutable observation timestamps."""

    def __init__(self, path: str | Path, encryption_key: str) -> None:
        if Fernet is None:
            raise ArchiveConfigurationError(
                "Vietnam social encryption support is not installed; "
                "install the 'fireant' optional dependency"
            )
        if not encryption_key:
            raise ArchiveConfigurationError("FIREANT_ARCHIVE_ENCRYPTION_KEY is required")
        try:
            self._fernet = Fernet(encryption_key.encode("ascii"))
        except (ValueError, TypeError):
            raise ArchiveConfigurationError(
                "FIREANT_ARCHIVE_ENCRYPTION_KEY is not a valid Fernet key"
            ) from None

        # ``resolve()`` would follow a malicious archive symlink before we can
        # reject it. ``abspath`` normalizes ``..`` without dereferencing links.
        raw_path = Path(os.path.abspath(str(Path(path).expanduser())))
        # Resolve only the parent and then keep the final filename literal. This
        # accepts legitimate platform/user directory aliases while preventing
        # subsequent SQLite opens from repeatedly traversing mutable symlinks.
        # The archive target itself is checked before and after canonicalization.
        if os.path.lexists(raw_path) and raw_path.is_symlink():
            raise ArchiveConfigurationError("social archive path must not be a symlink")
        self.path = raw_path.parent.resolve(strict=False) / raw_path.name
        self._hmac_key = hashlib.sha256(encryption_key.encode("ascii")).digest()
        self.archive_id = ""
        self._prepare_path()
        self._initialize()

    def _prepare_path(self) -> None:
        parent = self.path.parent
        parent_existed = parent.exists()
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Reject the archive target itself (including a dangling symlink). The
        # canonical parent must be an app-owned directory; existing broad parent
        # permissions are validated but never silently changed.
        if os.path.lexists(self.path) and self.path.is_symlink():
            raise ArchiveConfigurationError("social archive path must not be a symlink")
        parent_stat = parent.stat()
        if not parent.is_dir() or (
            hasattr(os, "getuid") and parent_stat.st_uid != os.getuid()
        ):
            raise ArchiveConfigurationError("social archive directory is not app-owned")
        if not parent_existed:
            try:
                parent.chmod(0o700)
            except OSError:
                raise ArchiveConfigurationError("cannot secure social archive directory") from None

        if not self.path.exists():
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            descriptor = os.open(self.path, flags, 0o600)
            os.close(descriptor)
        elif not self.path.is_file():
            raise ArchiveConfigurationError("social archive path is not a regular file")
        archive_stat = self.path.stat()
        if (
            (hasattr(os, "getuid") and archive_stat.st_uid != os.getuid())
            or archive_stat.st_nlink != 1
        ):
            raise ArchiveConfigurationError("social archive file is not safely app-owned")
        self.path.chmod(0o600)

    @contextmanager
    def collection_lock(self, ticker: str) -> Iterator[bool]:
        """Acquire a nonblocking process-wide collector lease for one ticker."""
        if fcntl is None:
            yield False
            return
        safe_ticker = "".join(char for char in ticker.upper() if char.isalnum())
        lock_path = self.path.with_name(f".{self.path.name}.{safe_ticker}.collect.lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
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

    def claim_snapshot(
        self,
        ticker: str,
        analysis_date: str,
        *,
        prompt_version: str,
        profile_fingerprint: str,
        lease_seconds: int = 900,
        owner_id: str | None = None,
    ) -> SnapshotClaim:
        """Atomically claim expensive snapshot work across scheduler processes."""
        if not 30 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 30 and 3600")
        owner = owner_id or uuid.uuid4().hex
        now = datetime.now(UTC)
        now_iso = _iso_utc(now)
        expires = _iso_utc(now + timedelta(seconds=lease_seconds))
        parameters = (ticker.upper(), analysis_date, prompt_version, profile_fingerprint)
        with self._connect() as connection, connection:
            connection.execute(
                "DELETE FROM snapshot_claims WHERE expires_at<=?",
                (now_iso,),
            )
            if connection.execute(
                """
                SELECT 1 FROM sentiment_snapshots
                WHERE ticker=? AND analysis_date=? AND prompt_version=?
                  AND profile_fingerprint=?
                """,
                parameters,
            ).fetchone():
                acquired = False
            else:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO snapshot_claims(
                        ticker, analysis_date, prompt_version, profile_fingerprint,
                        owner_id, claimed_at, expires_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*parameters, owner, now_iso, expires),
                )
                acquired = cursor.rowcount == 1
        return SnapshotClaim(
            acquired=acquired,
            owner_id=owner,
            ticker=ticker.upper(),
            analysis_date=analysis_date,
            prompt_version=prompt_version,
            profile_fingerprint=profile_fingerprint,
        )

    def release_snapshot_claim(self, claim: SnapshotClaim) -> bool:
        with self._connect() as connection, connection:
            cursor = connection.execute(
                """
                DELETE FROM snapshot_claims
                WHERE ticker=? AND analysis_date=? AND prompt_version=?
                  AND profile_fingerprint=? AND owner_id=?
                """,
                (
                    claim.ticker,
                    claim.analysis_date,
                    claim.prompt_version,
                    claim.profile_fingerprint,
                    claim.owner_id,
                ),
            )
        return cursor.rowcount == 1

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self.path.is_symlink():
            raise ArchiveConfigurationError("social archive path must not be a symlink")
        archive_stat = self.path.stat()
        if (
            (hasattr(os, "getuid") and archive_stat.st_uid != os.getuid())
            or archive_stat.st_nlink != 1
        ):
            raise ArchiveConfigurationError("social archive file is not safely app-owned")
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA secure_delete=ON")
            yield connection
        finally:
            connection.close()
            self._secure_sidecars()

    def _secure_sidecars(self) -> None:
        for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if candidate.exists() and not candidate.is_symlink():
                with suppress(OSError):
                    candidate.chmod(0o600)

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS archive_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS posts (
            provider TEXT NOT NULL,
            provider_post_id TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            published_at TEXT NOT NULL,
            current_content_hash TEXT NOT NULL,
            author_key TEXT NOT NULL,
            is_ai_generated INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (provider, provider_post_id)
        );

        CREATE TABLE IF NOT EXISTS post_versions (
            provider TEXT NOT NULL,
            provider_post_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            published_at TEXT NOT NULL,
            text_ciphertext BLOB,
            author_ciphertext BLOB,
            author_key TEXT NOT NULL,
            provider_sentiment INTEGER,
            engagement_json TEXT NOT NULL,
            tagged_symbols_json TEXT NOT NULL,
            is_ai_generated INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (provider, provider_post_id, content_hash),
            FOREIGN KEY (provider, provider_post_id)
                REFERENCES posts(provider, provider_post_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS post_observations (
            provider TEXT NOT NULL,
            provider_post_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            provider_sentiment INTEGER,
            engagement_json TEXT NOT NULL,
            PRIMARY KEY (provider, provider_post_id, content_hash, observed_at),
            FOREIGN KEY (provider, provider_post_id, content_hash)
                REFERENCES post_versions(provider, provider_post_id, content_hash)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS post_symbols (
            provider TEXT NOT NULL,
            provider_post_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            PRIMARY KEY (provider, provider_post_id, ticker),
            FOREIGN KEY (provider, provider_post_id)
                REFERENCES posts(provider, provider_post_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS purged_identities (
            identity_hmac TEXT PRIMARY KEY,
            purged_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS fetch_runs (
            fetch_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            status TEXT NOT NULL,
            pages INTEGER NOT NULL,
            post_count INTEGER NOT NULL,
            truncated INTEGER NOT NULL DEFAULT 0,
            ordering_violated INTEGER NOT NULL DEFAULT 0,
            watermark_stopped INTEGER NOT NULL DEFAULT 0,
            request_succeeded INTEGER NOT NULL DEFAULT 0,
            warnings_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS fetch_runs_ticker_time
            ON fetch_runs(ticker, completed_at);
        CREATE INDEX IF NOT EXISTS post_versions_published
            ON post_versions(published_at, first_seen_at);
        CREATE INDEX IF NOT EXISTS post_observations_cutoff
            ON post_observations(provider, provider_post_id, content_hash, observed_at);
        CREATE INDEX IF NOT EXISTS post_symbols_ticker
            ON post_symbols(ticker, provider, provider_post_id);

        CREATE TABLE IF NOT EXISTS sentiment_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            analysis_date TEXT NOT NULL,
            status TEXT NOT NULL,
            report_status TEXT NOT NULL,
            signal_json TEXT NOT NULL,
            report_json TEXT NOT NULL,
            model_profile TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            profile_fingerprint TEXT NOT NULL,
            statistics_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (ticker, analysis_date, prompt_version, profile_fingerprint)
        );

        CREATE TABLE IF NOT EXISTS snapshot_claims (
            ticker TEXT NOT NULL,
            analysis_date TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            profile_fingerprint TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (ticker, analysis_date, prompt_version, profile_fingerprint)
        );
        """
        with self._connect() as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS archive_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            existing = connection.execute(
                "SELECT value FROM archive_meta WHERE key='schema_version'"
            ).fetchone()
            existing_version = int(existing["value"]) if existing is not None else 0
            if existing_version > ARCHIVE_SCHEMA_VERSION:
                raise ArchiveConfigurationError("unsupported Vietnam social archive schema version")
            self._verify_archive_key(connection)
            connection.executescript(schema)
            self._migrate_schema(connection, existing_version)
            archive_id = connection.execute(
                "SELECT value FROM archive_meta WHERE key='archive_id'"
            ).fetchone()
            if archive_id is None:
                self.archive_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO archive_meta(key, value) VALUES('archive_id', ?)",
                    (self.archive_id,),
                )
            else:
                self.archive_id = str(archive_id["value"])
            connection.execute(
                "INSERT INTO archive_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(ARCHIVE_SCHEMA_VERSION),),
            )

    def _verify_archive_key(self, connection: sqlite3.Connection) -> None:
        verifier = connection.execute(
            "SELECT value FROM archive_meta WHERE key='key_verifier'"
        ).fetchone()
        if verifier is not None:
            try:
                plaintext = self._fernet.decrypt(str(verifier["value"]).encode("ascii"))
            except (InvalidToken, ValueError, UnicodeEncodeError):
                raise ArchiveConfigurationError(
                    "social archive encryption key does not match this archive"
                ) from None
            if plaintext != b"tradingagents-vn-social-archive":
                raise ArchiveConfigurationError("social archive key verifier is invalid")
            return

        # V1 had no verifier. If encrypted content exists, validate the key
        # against one row before blessing it for all future opens.
        has_versions = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='post_versions'"
        ).fetchone()
        if has_versions:
            sample = connection.execute(
                "SELECT text_ciphertext FROM post_versions "
                "WHERE text_ciphertext IS NOT NULL LIMIT 1"
            ).fetchone()
            if sample is not None:
                self._decrypt_json(sample["text_ciphertext"])
        token = self._fernet.encrypt(b"tradingagents-vn-social-archive").decode("ascii")
        connection.execute(
            "INSERT INTO archive_meta(key, value) VALUES('key_verifier', ?)",
            (token,),
        )

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _migrate_schema(self, connection: sqlite3.Connection, version: int) -> None:
        """Migrate the short-lived v1 archive without discarding encrypted raw data."""
        fetch_columns = self._columns(connection, "fetch_runs")
        if "watermark_stopped" not in fetch_columns:
            connection.execute(
                "ALTER TABLE fetch_runs ADD COLUMN watermark_stopped "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "request_succeeded" not in fetch_columns:
            connection.execute(
                "ALTER TABLE fetch_runs ADD COLUMN request_succeeded "
                "INTEGER NOT NULL DEFAULT 0"
            )
            # V1's available/partial status implied at least one successful
            # provider page; unavailable could also have meant an empty 200 and
            # cannot safely be inferred during migration.
            connection.execute(
                "UPDATE fetch_runs SET request_succeeded=1 "
                "WHERE status IN ('available', 'partial')"
            )

        snapshot_columns = self._columns(connection, "sentiment_snapshots")
        if "report_status" not in snapshot_columns:
            connection.execute(
                "ALTER TABLE sentiment_snapshots ADD COLUMN report_status "
                "TEXT NOT NULL DEFAULT 'unavailable'"
            )

        snapshot_columns = self._columns(connection, "sentiment_snapshots")
        if snapshot_columns and "profile_fingerprint" not in snapshot_columns:
            connection.execute(
                "ALTER TABLE sentiment_snapshots RENAME TO sentiment_snapshots_v1"
            )
            connection.execute(
                """
                CREATE TABLE sentiment_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    analysis_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_status TEXT NOT NULL,
                    signal_json TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    model_profile TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    profile_fingerprint TEXT NOT NULL,
                    statistics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (ticker, analysis_date, prompt_version, profile_fingerprint)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO sentiment_snapshots(
                    snapshot_id, ticker, analysis_date, status, report_status, signal_json,
                    report_json, model_profile, prompt_version, fingerprint,
                    profile_fingerprint, statistics_json, created_at
                )
                SELECT snapshot_id, ticker, analysis_date, status, report_status, signal_json,
                       report_json, model_profile, prompt_version, fingerprint,
                       '', statistics_json, created_at
                FROM sentiment_snapshots_v1
                """
            )
            connection.execute("DROP TABLE sentiment_snapshots_v1")

        if version < 2:
            # V1 stored mutable provider sentiment/engagement on a content
            # version. Seed one observation so old archives remain readable;
            # the result is conservatively treated as proxy by the service.
            connection.execute(
                """
                INSERT OR IGNORE INTO post_observations(
                    provider, provider_post_id, content_hash, observed_at,
                    provider_sentiment, engagement_json
                )
                SELECT provider, provider_post_id, content_hash, first_seen_at,
                       provider_sentiment, engagement_json
                FROM post_versions
                """
            )

    def _encrypt_json(self, value: Any) -> bytes:
        plaintext = json.dumps(
            _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self._fernet.encrypt(plaintext)

    def _decrypt_json(self, value: bytes | None) -> Any:
        if value is None:
            return None
        try:
            return json.loads(self._fernet.decrypt(value).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError):
            raise ArchiveConfigurationError(
                "social archive content cannot be decrypted with the configured key"
            ) from None

    def author_key(self, author: dict[str, Any]) -> str:
        identity = next(
            (
                str(author.get(key))
                for key in ("id", "user_id", "username", "name")
                if author.get(key) not in (None, "")
            ),
            "unknown",
        )
        return hmac.new(self._hmac_key, identity.encode("utf-8"), hashlib.sha256).hexdigest()

    def _identity_hmac(self, provider: str, post_id: str, content_hash: str) -> str:
        opaque = f"{provider}\0{post_id}\0{content_hash}".encode()
        return hmac.new(self._hmac_key, opaque, hashlib.sha256).hexdigest()

    def record_fetch(self, result: Any) -> dict[str, Any]:
        """Atomically append posts/versions and one fetch-run audit record."""
        posts = list(getattr(result, "posts", None) or [])
        fetch_id = str(getattr(result, "fetch_id", ""))
        ticker = str(getattr(result, "ticker", "")).upper()
        started_at = _iso_utc(getattr(result, "started_at", None))
        completed_at = _iso_utc(getattr(result, "completed_at", None))
        status = str(
            getattr(getattr(result, "status", None), "value", None)
            or getattr(result, "status", "unavailable")
        )
        pages = int(getattr(result, "pages", 0) or 0)
        truncated = bool(getattr(result, "truncated", False))
        ordering_violated = bool(getattr(result, "ordering_violated", False))
        watermark_stopped = bool(getattr(result, "watermark_stopped", False))
        request_succeeded = bool(getattr(result, "request_succeeded", False))
        warnings = [str(item)[:500] for item in (getattr(result, "warnings", None) or [])]
        inserted_versions = 0
        retention_skips = 0

        with self._connect() as connection, connection:
            for post in posts:
                provider = str(post.provider)
                post_id = str(post.provider_post_id)
                observed = _iso_utc(post.first_seen_at)
                published = _iso_utc(post.published_at)
                content_hash = str(post.content_hash)
                author = dict(post.author or {})
                author_key = self.author_key(author)
                identity_hmac = self._identity_hmac(provider, post_id, content_hash)
                if connection.execute(
                    "SELECT 1 FROM purged_identities WHERE identity_hmac=?",
                    (identity_hmac,),
                ).fetchone():
                    # A deep page must not resurrect raw content deliberately
                    # removed by retention. The archive keeps only this keyed,
                    # non-provider identifier as an anti-resurrection tombstone.
                    retention_skips += 1
                    continue
                connection.execute(
                    """
                        INSERT INTO posts(
                            provider, provider_post_id, first_seen_at, last_seen_at,
                            published_at, current_content_hash, author_key, is_ai_generated
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(provider, provider_post_id) DO UPDATE SET
                            first_seen_at=MIN(posts.first_seen_at, excluded.first_seen_at),
                            last_seen_at=MAX(posts.last_seen_at, excluded.last_seen_at),
                            published_at=MIN(posts.published_at, excluded.published_at),
                            current_content_hash=CASE
                                WHEN excluded.last_seen_at >= posts.last_seen_at
                                THEN excluded.current_content_hash
                                ELSE posts.current_content_hash
                            END,
                            author_key=CASE
                                WHEN excluded.last_seen_at >= posts.last_seen_at
                                THEN excluded.author_key
                                ELSE posts.author_key
                            END,
                            is_ai_generated=CASE
                                WHEN excluded.last_seen_at >= posts.last_seen_at
                                THEN excluded.is_ai_generated
                                ELSE posts.is_ai_generated
                            END
                        """,
                    (
                        provider,
                        post_id,
                        observed,
                        observed,
                        published,
                        content_hash,
                        author_key,
                        int(bool(getattr(post, "is_ai_generated", False))),
                    ),
                )
                version_existed = connection.execute(
                    """
                    SELECT 1 FROM post_versions
                    WHERE provider=? AND provider_post_id=? AND content_hash=?
                    """,
                    (provider, post_id, content_hash),
                ).fetchone() is not None
                connection.execute(
                    """
                        INSERT INTO post_versions(
                            provider, provider_post_id, content_hash, first_seen_at,
                            last_seen_at, published_at, text_ciphertext,
                            author_ciphertext, author_key, provider_sentiment,
                            engagement_json, tagged_symbols_json, is_ai_generated
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(provider, provider_post_id, content_hash) DO UPDATE SET
                            first_seen_at=MIN(post_versions.first_seen_at, excluded.first_seen_at),
                            last_seen_at=MAX(post_versions.last_seen_at, excluded.last_seen_at),
                            published_at=MIN(post_versions.published_at, excluded.published_at)
                        """,
                    (
                        provider,
                        post_id,
                        content_hash,
                        observed,
                        observed,
                        published,
                        self._encrypt_json(str(post.text)),
                        self._encrypt_json(author),
                        author_key,
                        post.provider_sentiment,
                        json.dumps(_jsonable(post.engagement or {}), sort_keys=True),
                        json.dumps(sorted(set(post.tagged_symbols or []))),
                        int(bool(getattr(post, "is_ai_generated", False))),
                    ),
                )
                inserted_versions += int(not version_existed)
                connection.execute(
                    """
                        INSERT OR IGNORE INTO post_observations(
                            provider, provider_post_id, content_hash, observed_at,
                            provider_sentiment, engagement_json
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                    (
                        provider,
                        post_id,
                        content_hash,
                        observed,
                        post.provider_sentiment,
                        json.dumps(_jsonable(post.engagement or {}), sort_keys=True),
                    ),
                )
                for symbol in sorted(set(post.tagged_symbols or [ticker])):
                    connection.execute(
                        """
                            INSERT OR IGNORE INTO post_symbols(provider, provider_post_id, ticker)
                            VALUES(?, ?, ?)
                            """,
                        (provider, post_id, str(symbol).upper()),
                    )

            connection.execute(
                """
                    INSERT OR IGNORE INTO fetch_runs(
                        fetch_id, ticker, started_at, completed_at, status, pages,
                        post_count, truncated, ordering_violated, watermark_stopped,
                        request_succeeded, warnings_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    fetch_id,
                    ticker,
                    started_at,
                    completed_at,
                    status,
                    pages,
                    len(posts),
                    int(truncated),
                    int(ordering_violated),
                    int(watermark_stopped),
                    int(request_succeeded),
                    json.dumps(warnings, ensure_ascii=False),
                ),
            )
        return {
            "fetch_id": fetch_id,
            "ticker": ticker,
            "status": status,
            "posts_seen": len(posts),
            "versions_inserted": inserted_versions,
            "retention_skips": retention_skips,
            "pages": pages,
            "truncated": truncated,
            "watermark_stopped": watermark_stopped,
            "warnings": warnings,
        }

    def posts_for_window(
        self,
        ticker: str,
        *,
        start: datetime,
        as_of: datetime,
        post_factory: Any,
    ) -> list[Any]:
        """Return the latest post version that was actually observed by ``as_of``."""
        start_utc = _iso_utc(start)
        cutoff_utc = _iso_utc(as_of)
        query = """
        WITH eligible_versions AS (
            SELECT
                v.*,
                ROW_NUMBER() OVER (
                    PARTITION BY v.provider, v.provider_post_id
                    ORDER BY v.first_seen_at DESC, v.content_hash DESC
                ) AS revision_rank
            FROM post_versions v
            JOIN post_symbols s
              ON s.provider=v.provider AND s.provider_post_id=v.provider_post_id
            WHERE s.ticker=?
              AND v.published_at>=?
              AND v.published_at<=?
              AND v.first_seen_at<=?
              AND v.text_ciphertext IS NOT NULL
              AND v.author_ciphertext IS NOT NULL
        ), selected_versions AS (
            SELECT * FROM eligible_versions WHERE revision_rank=1
        ), eligible_observations AS (
            SELECT
                o.*,
                ROW_NUMBER() OVER (
                    PARTITION BY o.provider, o.provider_post_id, o.content_hash
                    ORDER BY o.observed_at DESC
                ) AS observation_rank
            FROM post_observations o
            JOIN selected_versions v
              ON v.provider=o.provider
             AND v.provider_post_id=o.provider_post_id
             AND v.content_hash=o.content_hash
            WHERE o.observed_at<=?
        )
        SELECT
            v.*,
            o.provider_sentiment AS observation_sentiment,
            o.engagement_json AS observation_engagement_json,
            o.observed_at AS observation_at
        FROM selected_versions v
        JOIN eligible_observations o
          ON o.provider=v.provider
         AND o.provider_post_id=v.provider_post_id
         AND o.content_hash=v.content_hash
         AND o.observation_rank=1
        ORDER BY v.published_at DESC, v.provider_post_id DESC
        """
        with self._connect() as connection:
            rows = connection.execute(
                query, (ticker.upper(), start_utc, cutoff_utc, cutoff_utc, cutoff_utc)
            ).fetchall()

        posts = []
        for row in rows:
            author = self._decrypt_json(row["author_ciphertext"])
            text = self._decrypt_json(row["text_ciphertext"])
            posts.append(
                post_factory(
                    provider=row["provider"],
                    provider_post_id=row["provider_post_id"],
                    ticker=ticker.upper(),
                    text=str(text or ""),
                    published_at=datetime.fromisoformat(row["published_at"]),
                    first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
                    provider_sentiment=row["observation_sentiment"],
                    engagement=json.loads(row["observation_engagement_json"]),
                    author=author or {},
                    tagged_symbols=json.loads(row["tagged_symbols_json"]),
                    content_hash=row["content_hash"],
                    is_ai_generated=bool(row["is_ai_generated"]),
                    author_key=row["author_key"],
                )
            )
        return posts

    def known_content_keys(self, ticker: str) -> set[tuple[str, str]]:
        """Return archived identities for a best-effort pagination watermark.

        FireAnt does not document stable ordering, so matching a known page is
        only an optimization signal; callers must mark such a fetch partial.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT v.provider_post_id, v.content_hash
                FROM post_versions v
                JOIN post_symbols s
                  ON s.provider=v.provider AND s.provider_post_id=v.provider_post_id
                WHERE s.ticker=?
                """,
                (ticker.upper(),),
            ).fetchall()
        return {(str(row["provider_post_id"]), str(row["content_hash"])) for row in rows}

    def coverage_for_window(
        self,
        ticker: str,
        *,
        start: datetime,
        as_of: datetime,
        poll_seconds: int,
    ) -> tuple[bool, list[str], str | None]:
        """Check collector continuity instead of inferring PIT coverage from post dates."""
        grace = max(poll_seconds * 2, 600)
        lower = _iso_utc(start - timedelta(seconds=grace))
        # Never let a collection completed after ``as_of`` prove historical
        # coverage. Grace is used only to tolerate a last poll just before the
        # cutoff, not to import future observations.
        upper = _iso_utc(as_of)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM fetch_runs
                WHERE ticker=? AND completed_at>=? AND completed_at<=?
                  AND request_succeeded=1
                ORDER BY completed_at ASC
                """,
                (ticker.upper(), lower, upper),
            ).fetchall()
        if not rows:
            return False, ["No successful archive collection covers this window."], None

        completed = [datetime.fromisoformat(row["completed_at"]) for row in rows]
        warnings: list[str] = []
        complete = True
        if completed[0] > start.astimezone(UTC) + timedelta(seconds=grace):
            complete = False
            warnings.append("Archive collection started after the requested window began.")
        if completed[-1] < as_of.astimezone(UTC) - timedelta(seconds=grace):
            complete = False
            warnings.append("Archive collection does not reach the requested as-of time.")
        if any(
            later - earlier > timedelta(seconds=grace)
            for earlier, later in zip(completed, completed[1:], strict=False)
        ):
            complete = False
            warnings.append("Archive collection contains a polling gap.")
        if any(
            row["truncated"] or row["ordering_violated"] or row["watermark_stopped"]
            for row in rows
        ):
            complete = False
            warnings.append(
                "A collection run was truncated, watermark-stopped, or returned "
                "unstable ordering."
            )
        return complete, warnings, rows[-1]["fetch_id"]

    def save_snapshot(
        self,
        ticker: str,
        analysis_date: str,
        *,
        signal_payload: dict[str, Any],
        report_payload: dict[str, Any],
        model_profile: str,
        prompt_version: str,
        fingerprint: str,
        profile_fingerprint: str = "",
        status: str,
        report_status: str | None = None,
        statistics: dict[str, Any],
    ) -> SnapshotRecord:
        now = _iso_utc()
        stable = (
            f"{ticker.upper()}|{analysis_date}|{prompt_version}|{profile_fingerprint}"
        )
        snapshot_id = hashlib.sha256(stable.encode()).hexdigest()[:24]
        safe_signal = _redact_snapshot_payload(signal_payload)
        safe_report = _redact_snapshot_payload(report_payload)
        safe_statistics = _redact_snapshot_payload(statistics)
        if isinstance(safe_statistics, dict):
            safe_statistics.setdefault("window_end", f"{analysis_date}T15:00:00+07:00")
            if safe_statistics.get("point_in_time_quality") in (None, "exact"):
                safe_statistics["point_in_time_quality"] = "proxy"
        # ``status`` persisted on the snapshot is always the retail FireAnt
        # lane. Older CLI callers passed the overall report status here, so the
        # canonical signal payload takes precedence when available.
        payload_status = safe_signal.get("status") if isinstance(safe_signal, dict) else None
        retail_status = str(payload_status or status)
        effective_report_status = report_status or status
        values = (
            snapshot_id,
            ticker.upper(),
            analysis_date,
            retail_status,
            effective_report_status,
            json.dumps(safe_signal, ensure_ascii=False, sort_keys=True),
            json.dumps(safe_report, ensure_ascii=False, sort_keys=True),
            model_profile,
            prompt_version,
            fingerprint,
            profile_fingerprint,
            json.dumps(safe_statistics, ensure_ascii=False, sort_keys=True),
            now,
        )
        with self._connect() as connection, connection:
            cursor = connection.execute(
                """
                    INSERT OR IGNORE INTO sentiment_snapshots(
                        snapshot_id, ticker, analysis_date, status, report_status, signal_json,
                        report_json, model_profile, prompt_version, fingerprint,
                        profile_fingerprint, statistics_json, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                values,
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    DELETE FROM snapshot_claims
                    WHERE ticker=? AND analysis_date=? AND prompt_version=?
                      AND profile_fingerprint=?
                    """,
                    (ticker.upper(), analysis_date, prompt_version, profile_fingerprint),
                )
        record = self.get_snapshot(
            ticker,
            analysis_date,
            prompt_version=prompt_version,
            profile_fingerprint=profile_fingerprint,
            strict=True,
        )
        if record is None:  # defensive: transaction succeeded but no row is unexpected
            raise RuntimeError("sentiment snapshot could not be read after save")
        return SnapshotRecord(**{**asdict(record), "created": cursor.rowcount == 1})

    def get_snapshot(
        self,
        ticker: str,
        analysis_date: str,
        *,
        prompt_version: str | None = None,
        profile_fingerprint: str | None = None,
        model_profile: str | None = None,
        fingerprint: str | None = None,
        strict: bool = False,
    ) -> SnapshotRecord | None:
        if strict and (prompt_version is None or profile_fingerprint is None):
            raise ValueError(
                "strict snapshot lookup requires prompt_version and profile_fingerprint"
            )
        where = "ticker=? AND analysis_date=?"
        parameters: list[Any] = [ticker.upper(), analysis_date]
        if prompt_version is not None:
            where += " AND prompt_version=?"
            parameters.append(prompt_version)
        if profile_fingerprint is not None:
            where += " AND profile_fingerprint=?"
            parameters.append(profile_fingerprint)
        if model_profile is not None:
            where += " AND model_profile=?"
            parameters.append(model_profile)
        if fingerprint is not None:
            where += " AND fingerprint=?"
            parameters.append(fingerprint)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM sentiment_snapshots WHERE {where}
                ORDER BY created_at DESC LIMIT 1
                """,  # noqa: S608 - where is assembled only from fixed literals
                parameters,
            ).fetchone()
        if row is None:
            return None
        return SnapshotRecord(
            snapshot_id=row["snapshot_id"],
            ticker=row["ticker"],
            analysis_date=row["analysis_date"],
            status=row["status"],
            report_status=row["report_status"],
            signal_payload=json.loads(row["signal_json"]),
            report_payload=json.loads(row["report_json"]),
            model_profile=row["model_profile"],
            prompt_version=row["prompt_version"],
            fingerprint=row["fingerprint"],
            profile_fingerprint=row["profile_fingerprint"],
            statistics=json.loads(row["statistics_json"]),
            created_at=row["created_at"],
            created=False,
        )

    def purge_expired(
        self,
        *,
        retention_days: int,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Delete expired raw/linkable rows while retaining anonymous snapshots.

        ``secure_delete``, a truncated WAL checkpoint and ``VACUUM`` reduce
        recoverable SQLite remnants. They cannot erase filesystem snapshots,
        backups, SSD wear-levelled blocks, or copies made before purge; operators
        must apply the same retention policy to those external layers.
        """
        cutoff = _iso_utc((now or datetime.now(UTC)) - timedelta(days=retention_days))
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        with self._connect() as connection:
            connection.execute("PRAGMA secure_delete=ON")
            with connection:
                expired = connection.execute(
                    """
                    SELECT provider, provider_post_id, content_hash
                    FROM post_versions WHERE first_seen_at<?
                    """,
                    (cutoff,),
                ).fetchall()
                purged = len(expired)
                purged_at = _iso_utc(now)
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO purged_identities(identity_hmac, purged_at)
                    VALUES(?, ?)
                    """,
                    [
                        (
                            self._identity_hmac(
                                row["provider"], row["provider_post_id"], row["content_hash"]
                            ),
                            purged_at,
                        )
                        for row in expired
                    ],
                )
                connection.execute(
                    "DELETE FROM post_versions WHERE first_seen_at<?",
                    (cutoff,),
                )
                orphan_cursor = connection.execute(
                    """
                    DELETE FROM posts
                    WHERE NOT EXISTS (
                        SELECT 1 FROM post_versions v
                        WHERE v.provider=posts.provider
                          AND v.provider_post_id=posts.provider_post_id
                    )
                    """
                )
                orphans = max(orphan_cursor.rowcount, 0)
                # Fetch audits have no post/author identifiers, but old warning
                # strings are unnecessary operational data after raw retention.
                connection.execute(
                    "DELETE FROM fetch_runs WHERE completed_at<?",
                    (cutoff,),
                )
            snapshots = connection.execute(
                "SELECT COUNT(*) AS count FROM sentiment_snapshots"
            ).fetchone()["count"]
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("VACUUM")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {
            "versions_purged": purged,
            "posts_purged": orphans,
            "snapshots_retained": int(snapshots),
            "secure_delete": 1,
            "vacuumed": 1,
        }
