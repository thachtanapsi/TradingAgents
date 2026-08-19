from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import requests
from cryptography.fernet import Fernet

from tradingagents.dataflows.vietnam_social import (
    FireAntClient,
    SocialFetchResult,
    SocialPost,
    SocialStatus,
    VietnamSocialConfig,
    VietnamSocialService,
    sanitize_post_text,
    select_prompt_posts,
)
from tradingagents.dataflows.vietnam_social_archive import (
    ArchiveConfigurationError,
    VietnamSocialArchive,
)

UTC = timezone.utc


def _config(tmp_path, **overrides):
    base = VietnamSocialConfig(
        provider="fireant",
        authorized=True,
        hosted_llm_authorized=False,
        access_token="unit-fireant-token",
        encryption_key=Fernet.generate_key().decode(),
        watchlist=("HPG",),
        archive_path=tmp_path / "vn_social.sqlite3",
        lookback_days=7,
        min_posts=2,
        min_unique_authors=2,
        poll_seconds=86400,
        retention_days=90,
        timeout_seconds=10,
        page_size=100,
        max_pages=20,
        max_attempts=3,
    )
    return replace(base, **overrides)


class Response:
    def __init__(self, status_code=200, payload=None, headers=None, json_error=False):
        self.status_code = status_code
        self._payload = [] if payload is None else payload
        self.headers = headers or {}
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise ValueError("malformed")
        return self._payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _payload(
    post_id="1",
    *,
    ticker="HPG",
    date="2026-08-13T10:00:00+07:00",
    content="<p>Hòa Phát &amp; triển vọng tốt</p>",
    author="author-1",
    sentiment=1,
    ai=False,
):
    return {
        "postID": post_id,
        "date": date,
        "content": content,
        "sentiment": sentiment,
        "isAIGenerated": ai,
        "taggedSymbols": [{"symbol": ticker}],
        "user": {"id": author, "name": f"Name {author}", "isAuthentic": True},
        "userName": f"user-{author}",
        "totalLikes": 12,
        "totalComments": 3,
    }


def _post(
    post_id,
    observed,
    *,
    published=None,
    author="author-1",
    text="Quan điểm HPG",
    ai=False,
):
    published = published or observed - timedelta(hours=1)
    return SocialPost(
        provider="fireant",
        provider_post_id=str(post_id),
        ticker="HPG",
        text=text,
        published_at=published,
        first_seen_at=observed,
        provider_sentiment=0,
        engagement={"likes": 2},
        author={"id": author, "username": f"u-{author}", "bio": "private bio"},
        tagged_symbols=["HPG"],
        content_hash=__import__("hashlib").sha256(text.encode()).hexdigest(),
        is_ai_generated=ai,
    )


def _fetch(fetch_id, ticker, completed, posts, status=SocialStatus.AVAILABLE, **kwargs):
    return SocialFetchResult(
        fetch_id=fetch_id,
        provider="fireant",
        ticker=ticker,
        status=status,
        posts=posts,
        pages=1,
        started_at=completed - timedelta(seconds=1),
        completed_at=completed,
        **kwargs,
    )


def test_html_sanitization_removes_markup_scripts_and_preserves_vietnamese():
    assert sanitize_post_text(
        "<p>Hòa Phát &amp; thép</p><script>steal()</script><br>tăng"
    ) == "Hòa Phát & thép tăng"


def test_fireant_http_uses_bearer_exact_ticker_and_filters_wrong_tags(tmp_path):
    session = Session(
        [
            Response(
                payload=[
                    _payload("1"),
                    _payload("2", ticker="FPT"),
                    _payload("3", content="<script>x</script>"),
                ]
            )
        ]
    )
    client = FireAntClient(_config(tmp_path), session=session, sleep=lambda _: None)

    result = client.fetch_posts("hpg")

    assert result.status is SocialStatus.AVAILABLE
    assert [post.provider_post_id for post in result.posts] == ["1"]
    assert result.posts[0].text == "Hòa Phát & triển vọng tốt"
    url, kwargs = session.calls[0]
    assert url.endswith("/symbols/HPG/posts")
    assert kwargs["params"] == {"type": 0, "offset": 0, "limit": 100}
    assert kwargs["headers"]["Authorization"] == "Bearer unit-fireant-token"
    assert "unit-fireant-token" not in url
    assert "unit-fireant-token" not in str(kwargs["params"])


def test_fireant_retries_429_honors_retry_after_and_never_retries_auth(tmp_path):
    sleeps = []
    session = Session(
        [Response(429, headers={"Retry-After": "2"}), Response(payload=[_payload()])]
    )
    result = FireAntClient(
        _config(tmp_path), session=session, sleep=sleeps.append, jitter=lambda: 0
    ).fetch_posts("HPG")
    assert result.status is SocialStatus.AVAILABLE
    assert sleeps == [2.0]

    denied = Session([Response(401, payload={"token": "must-not-surface"})])
    result = FireAntClient(
        _config(tmp_path), session=denied, sleep=lambda _: pytest.fail("no retry")
    ).fetch_posts("HPG")
    assert result.status is SocialStatus.UNAVAILABLE
    assert result.warnings == ["FireAnt authorization failed with HTTP 401."]
    assert len(denied.calls) == 1


@pytest.mark.parametrize(
    "response,warning",
    [
        (Response(200, json_error=True), "malformed JSON"),
        (Response(200, payload={"posts": []}), "unexpected response shape"),
        (requests.Timeout("secret body"), "timed out or failed"),
    ],
)
def test_fireant_malformed_timeout_are_typed_unavailable(tmp_path, response, warning):
    result = FireAntClient(
        _config(tmp_path, max_attempts=1),
        session=Session([response]),
        sleep=lambda _: None,
    ).fetch_posts("HPG")
    assert result.status is SocialStatus.UNAVAILABLE
    assert any(warning in item for item in result.warnings)
    assert "secret body" not in " ".join(result.warnings)


def test_pagination_limit_and_unstable_order_are_partial(tmp_path):
    first = [_payload(str(index), date="2026-08-13T09:00:00+07:00") for index in range(100)]
    second = [_payload("later", date="2026-08-13T10:00:00+07:00")]
    result = FireAntClient(
        _config(tmp_path, max_pages=2),
        session=Session([Response(payload=first), Response(payload=second)]),
        sleep=lambda _: None,
    ).fetch_posts("HPG")
    assert result.status is SocialStatus.PARTIAL
    assert result.ordering_violated

    full_pages = Session([Response(payload=first), Response(payload=first)])
    result = FireAntClient(
        _config(tmp_path, max_pages=2), session=full_pages, sleep=lambda _: None
    ).fetch_posts("HPG")
    assert result.status is SocialStatus.PARTIAL
    assert result.truncated


def test_config_rejects_more_than_twenty_pages(tmp_path):
    config = _config(tmp_path, max_pages=21)
    with pytest.raises(ValueError, match="max_pages must be between 1 and 20"):
        config.validate()


def test_incremental_watermark_stops_before_deep_history_without_truncation(tmp_path):
    page = [_payload(str(index)) for index in range(100)]
    known = {
        (
            str(index),
            __import__("hashlib").sha256(
                "Hòa Phát & triển vọng tốt".encode()
            ).hexdigest(),
        )
        for index in range(100)
    }
    session = Session([Response(payload=page)])
    result = FireAntClient(
        _config(tmp_path), session=session, sleep=lambda _: None
    ).fetch_posts("HPG", known_keys=known)

    assert result.status is SocialStatus.PARTIAL
    assert result.pages == 1
    assert not result.truncated
    assert result.watermark_stopped
    assert len(session.calls) == 1


def test_archive_encrypts_raw_fields_is_0600_and_first_seen_is_immutable(tmp_path):
    config = _config(tmp_path)
    archive = VietnamSocialArchive(config.archive_path, config.encryption_key)
    first = datetime(2026, 8, 10, 3, tzinfo=UTC)
    second = first + timedelta(days=1)
    archive.record_fetch(_fetch("f1", "HPG", first, [_post("1", first, text="raw bí mật")]))
    archive.record_fetch(_fetch("f2", "HPG", second, [_post("1", second, text="raw bí mật")]))

    assert os.stat(config.archive_path).st_mode & 0o777 == 0o600
    assert b"raw b\xc3\xad m\xe1\xba\xadt" not in config.archive_path.read_bytes()
    assert b"private bio" not in config.archive_path.read_bytes()
    with sqlite3.connect(config.archive_path) as connection:
        row = connection.execute(
            "SELECT first_seen_at,last_seen_at FROM posts WHERE provider_post_id='1'"
        ).fetchone()
    assert row[0] == first.isoformat()
    assert row[1] == second.isoformat()


def test_archive_rejects_symlink_target(tmp_path):
    key = Fernet.generate_key().decode()
    real = tmp_path / "real.sqlite3"
    real.touch()
    link = tmp_path / "linked.sqlite3"
    link.symlink_to(real)
    with pytest.raises(ArchiveConfigurationError, match="symlink"):
        VietnamSocialArchive(link, key)


def test_archive_point_in_time_requires_published_and_first_seen_before_cutoff(tmp_path):
    config = _config(tmp_path)
    archive = VietnamSocialArchive(config.archive_path, config.encryption_key)
    cutoff = datetime(2026, 8, 13, 8, tzinfo=UTC)
    posts = [
        _post("known", cutoff - timedelta(hours=1), published=cutoff - timedelta(days=1)),
        _post("backfill", cutoff + timedelta(hours=1), published=cutoff - timedelta(days=2)),
        _post("future", cutoff - timedelta(hours=1), published=cutoff + timedelta(minutes=1)),
    ]
    archive.record_fetch(_fetch("f1", "HPG", cutoff + timedelta(hours=1), posts))

    loaded = archive.posts_for_window(
        "HPG",
        start=cutoff - timedelta(days=7),
        as_of=cutoff,
        post_factory=SocialPost,
    )
    assert [post.provider_post_id for post in loaded] == ["known"]


def test_service_coverage_thresholds_and_ai_exclusion(tmp_path):
    config = _config(tmp_path)
    archive = VietnamSocialArchive(config.archive_path, config.encryption_key)
    cutoff = datetime(2026, 8, 13, 8, tzinfo=UTC)
    start = cutoff - timedelta(days=7)
    base_posts = [
        _post("1", start + timedelta(hours=1), author="a"),
        _post("2", start + timedelta(hours=1), author="b"),
        _post("ai", start + timedelta(hours=1), author="c", ai=True),
    ]
    for index in range(5):
        completed = start + timedelta(days=index * 2)
        archive.record_fetch(
            _fetch(f"f{index}", "HPG", completed, base_posts if index == 0 else [])
        )
    service = VietnamSocialService(config, archive=archive)

    batch = service.load_evidence("HPG", cutoff)

    assert batch.status is SocialStatus.AVAILABLE
    assert batch.sample_size == 2
    assert batch.unique_authors == 2
    assert all(not post.is_ai_generated for post in batch.posts)
    assert batch.point_in_time_quality == "proxy"


def test_incomplete_archive_is_partial_only_when_sample_is_adequate(tmp_path):
    config = _config(tmp_path)
    archive = VietnamSocialArchive(config.archive_path, config.encryption_key)
    cutoff = datetime(2026, 8, 13, 8, tzinfo=UTC)
    archive.record_fetch(
        _fetch(
            "late",
            "HPG",
            cutoff,
            [_post("1", cutoff, author="a"), _post("2", cutoff, author="b")],
        )
    )
    batch = VietnamSocialService(config, archive=archive).load_evidence("HPG", cutoff)
    assert batch.status is SocialStatus.PARTIAL
    assert batch.point_in_time_quality == "partial"
    assert batch.warnings

    sparse = VietnamSocialService(replace(config, min_posts=3), archive=archive).load_evidence(
        "HPG", cutoff
    )
    assert sparse.status is SocialStatus.UNAVAILABLE


def test_future_collection_cannot_prove_historical_coverage(tmp_path):
    config = _config(tmp_path)
    archive = VietnamSocialArchive(config.archive_path, config.encryption_key)
    cutoff = datetime(2026, 8, 13, 8, tzinfo=UTC)
    start = cutoff - timedelta(days=7)
    posts = [
        _post("1", start + timedelta(hours=1), author="a"),
        _post("2", start + timedelta(hours=1), author="b"),
    ]
    archive.record_fetch(_fetch("start", "HPG", start, posts))
    archive.record_fetch(_fetch("future", "HPG", cutoff + timedelta(minutes=5), []))

    batch = VietnamSocialService(config, archive=archive).load_evidence("HPG", cutoff)
    assert batch.status is SocialStatus.PARTIAL
    assert any("does not reach" in warning for warning in batch.warnings)


def test_live_cutoff_reads_raw_archive_and_never_reuses_close_snapshot(tmp_path):
    config = _config(
        tmp_path,
        min_posts=1,
        min_unique_authors=1,
    )
    archive = VietnamSocialArchive(config.archive_path, config.encryption_key)
    before_cutoff = datetime(2026, 8, 19, 9, 1, tzinfo=UTC)  # 16:01 Vietnam
    after_cutoff = datetime(2026, 8, 19, 9, 6, tzinfo=UTC)  # 16:06 Vietnam
    cutoff = datetime(2026, 8, 19, 16, 5, tzinfo=timezone(timedelta(hours=7)))
    archive.record_fetch(
        _fetch(
            "live-before",
            "HPG",
            before_cutoff,
            [
                _post(
                    "known-at-1601",
                    before_cutoff,
                    published=datetime(2026, 8, 19, 9, 0, tzinfo=UTC),
                )
            ],
        )
    )
    archive.record_fetch(
        _fetch(
            "live-after",
            "HPG",
            after_cutoff,
            [
                _post(
                    "not-known-at-1605",
                    after_cutoff,
                    published=datetime(2026, 8, 19, 9, 2, tzinfo=UTC),
                )
            ],
        )
    )
    archive.save_snapshot(
        "HPG",
        "2026-08-19",
        signal_payload={"status": "available", "score": 9},
        report_payload={"rendered_report": "15:00 close snapshot"},
        model_profile="fake",
        prompt_version=config.prompt_version,
        fingerprint="close-snapshot",
        status="available",
        statistics={"sample_size": 99, "unique_authors": 20},
        profile_fingerprint=config.profile_fingerprint(archive.archive_id),
    )

    batch = VietnamSocialService(config, archive=archive).load_evidence(
        "HPG", cutoff, allow_snapshot=False
    )
    close_batch = VietnamSocialService(config, archive=archive).load_evidence(
        "HPG", "2026-08-19", allow_snapshot=False
    )

    assert batch.snapshot_id is None
    assert [post.provider_post_id for post in batch.posts] == ["known-at-1601"]
    assert batch.window_end == "2026-08-19T09:05:00+00:00"
    assert close_batch.posts == []


def test_explicit_social_collection_does_not_require_watchlist_membership(tmp_path):
    config = _config(tmp_path, watchlist=())
    client = FireAntClient(
        config,
        session=Session([Response(payload=[_payload(ticker="CTG")])]),
        sleep=lambda _: None,
    )
    archive = VietnamSocialArchive(config.archive_path, config.encryption_key)
    service = VietnamSocialService(config, client=client, archive=archive)

    result = service.collect_once("ctg")

    assert result[0]["ticker"] == "CTG"
    assert client.session.calls[0][0].endswith("/symbols/CTG/posts")
    with pytest.raises(RuntimeError, match="TICKERS is empty"):
        service.collect_once()


def test_snapshot_is_idempotent_and_survives_raw_purge(tmp_path):
    config = _config(tmp_path, retention_days=1)
    archive = VietnamSocialArchive(config.archive_path, config.encryption_key)
    old = datetime(2026, 1, 1, tzinfo=UTC)
    archive.record_fetch(_fetch("old", "HPG", old, [_post("1", old)]))
    first = archive.save_snapshot(
        "HPG",
        "2026-01-01",
        signal_payload={"status": "available", "score": 6},
        report_payload={"rendered_report": "first"},
        model_profile="ollama:qwen3:8b",
        prompt_version="vn-social-v1",
        fingerprint="abc",
        status="available",
        statistics={"sample_size": 10, "unique_authors": 5},
    )
    second = archive.save_snapshot(
        "HPG",
        "2026-01-01",
        signal_payload={"status": "available", "score": 1},
        report_payload={"rendered_report": "must-not-overwrite"},
        model_profile="another",
        prompt_version="vn-social-v1",
        fingerprint="changed",
        status="available",
        statistics={},
    )
    purge = archive.purge_expired(retention_days=1, now=old + timedelta(days=3))

    assert first.created is True
    assert second.created is False
    assert second.report_payload["rendered_report"] == "first"
    assert purge["versions_purged"] == 1
    assert purge["snapshots_retained"] == 1


def test_locked_service_never_initializes_network_and_reports_disabled(tmp_path):
    config = _config(
        tmp_path,
        authorized=False,
        access_token=None,
        encryption_key=None,
    )
    service = VietnamSocialService(config)
    status = service.status(live=True)
    assert status["authorized"] is False
    assert status["live_checked"] is False
    assert service.load_evidence("HPG", "2026-08-13").status is SocialStatus.DISABLED
    with pytest.raises(RuntimeError, match="authorization locked"):
        service.collect_once()


def test_live_status_accepts_authenticated_empty_response(tmp_path):
    config = _config(tmp_path)
    client = FireAntClient(
        config,
        session=Session([Response(200, payload=[])]),
        sleep=lambda _: None,
    )
    archive = VietnamSocialArchive(config.archive_path, config.encryption_key)
    status = VietnamSocialService(config, client=client, archive=archive).status(live=True)

    assert status["live_checked"] is True
    assert status["live_status"] == "unavailable"
    assert status["live_sample_size"] == 0
    assert status["issues"] == []


def test_prompt_selection_combines_recency_engagement_and_caps_authors():
    now = datetime(2026, 8, 13, tzinfo=UTC)
    posts = []
    for index in range(60):
        post = _post(
            str(index),
            now,
            published=now - timedelta(minutes=index),
            author=f"a-{index // 3}",
        )
        posts.append(replace(post, engagement={"likes": index * 10}))
    selected = select_prompt_posts(posts)
    counts = {}
    for post in selected:
        key = post.author["id"]
        counts[key] = counts.get(key, 0) + 1
    assert len(selected) == 40
    assert max(counts.values()) <= 3
    assert posts[0] in selected
    assert posts[-1] in selected


def test_response_completion_time_is_used_per_page(tmp_path):
    moments = iter(
        [
            datetime(2026, 8, 13, 1, tzinfo=UTC),
            datetime(2026, 8, 13, 1, 0, 5, tzinfo=UTC),
            datetime(2026, 8, 13, 1, 0, 6, tzinfo=UTC),
        ]
    )
    client = FireAntClient(
        _config(tmp_path),
        session=Session([Response(payload=[_payload()])]),
        sleep=lambda _: None,
        now=lambda: next(moments),
    )
    result = client.fetch_posts("HPG")
    assert result.started_at == datetime(2026, 8, 13, 1, tzinfo=UTC)
    assert result.posts[0].first_seen_at == datetime(2026, 8, 13, 1, 0, 5, tzinfo=UTC)


def test_mutable_provider_observation_is_selected_at_cutoff(tmp_path):
    config = _config(tmp_path)
    archive = VietnamSocialArchive(config.archive_path, config.encryption_key)
    early = datetime(2026, 8, 10, 3, tzinfo=UTC)
    late = early + timedelta(days=1)
    original = _post("1", early)
    updated = replace(original, first_seen_at=late, provider_sentiment=-1, engagement={"likes": 99})
    archive.record_fetch(_fetch("early", "HPG", early, [original]))
    archive.record_fetch(_fetch("late", "HPG", late, [updated]))

    before = archive.posts_for_window(
        "HPG", start=early - timedelta(days=1), as_of=early, post_factory=SocialPost
    )[0]
    after = archive.posts_for_window(
        "HPG", start=early - timedelta(days=1), as_of=late, post_factory=SocialPost
    )[0]
    assert (before.provider_sentiment, before.engagement) == (0, {"likes": 2})
    assert (after.provider_sentiment, after.engagement) == (-1, {"likes": 99})


def test_out_of_order_archive_upsert_keeps_min_first_seen_and_max_last_seen(tmp_path):
    config = _config(tmp_path)
    archive = VietnamSocialArchive(config.archive_path, config.encryption_key)
    early = datetime(2026, 8, 10, 3, tzinfo=UTC)
    late = early + timedelta(days=1)
    archive.record_fetch(_fetch("late", "HPG", late, [_post("1", late)]))
    archive.record_fetch(_fetch("early", "HPG", early, [_post("1", early)]))
    with sqlite3.connect(config.archive_path) as connection:
        row = connection.execute(
            "SELECT first_seen_at,last_seen_at FROM posts WHERE provider_post_id='1'"
        ).fetchone()
    assert row == (early.isoformat(), late.isoformat())


def test_collector_lock_skips_overlapping_network_fetch(tmp_path):
    config = _config(tmp_path)
    first_archive = VietnamSocialArchive(config.archive_path, config.encryption_key)
    second_archive = VietnamSocialArchive(config.archive_path, config.encryption_key)
    entered = threading.Event()
    release = threading.Event()

    class BlockingClient:
        def fetch_posts(self, ticker, **_kwargs):
            entered.set()
            release.wait(timeout=2)
            now = datetime.now(UTC)
            return _fetch("one", ticker, now, [])

    first = VietnamSocialService(config, client=BlockingClient(), archive=first_archive)
    class MustNotFetch:
        def fetch_posts(self, *_args, **_kwargs):
            pytest.fail("overlapping collector must not call FireAnt")

    second = VietnamSocialService(config, client=MustNotFetch(), archive=second_archive)
    outcome = []
    worker = threading.Thread(target=lambda: outcome.extend(first.collect_once()))
    worker.start()
    assert entered.wait(timeout=1)
    skipped = second.collect_once()
    release.set()
    worker.join(timeout=2)
    assert skipped == [{"ticker": "HPG", "status": "skipped", "reason": "collector_lock_held"}]
    assert outcome[0]["fetch_id"] == "one"


def test_snapshot_strict_profile_intraday_guard_and_claim(tmp_path):
    config = _config(tmp_path)
    archive = VietnamSocialArchive(config.archive_path, config.encryption_key)
    service = VietnamSocialService(config, archive=archive, client=object())
    record = service.save_snapshot(
        "HPG",
        "2026-08-13",
        signal_payload={"status": "available", "author": {"id": "secret"}},
        report_payload={"rendered_report": "aggregate"},
        model_profile="ollama:qwen3:8b",
        prompt_version="vn-social-v1",
        fingerprint="inputs",
        status="partial",
        report_status="partial",
        statistics={
            "sample_size": 10,
            "unique_authors": 5,
            "point_in_time_quality": "exact",
        },
    )
    assert record.status == "available"
    assert record.report_status == "partial"
    assert record.statistics["point_in_time_quality"] == "proxy"
    assert "author" not in record.signal_payload
    assert service.get_snapshot("HPG", "2026-08-13", model_profile="other") is None
    assert service.load_evidence("HPG", "2026-08-13T14:00:00+07:00").snapshot_id is None

    first_claim = service.claim_snapshot("FPT", "2026-08-13")
    second_claim = service.claim_snapshot("FPT", "2026-08-13")
    assert first_claim.acquired is True
    assert second_claim.acquired is False
    assert service.release_snapshot_claim(first_claim) is True
