"""OBS-07 cursor binding (ADR-021, corrected 2026-10-24):
`query.api.cursor` in isolation. The route-level wiring (actual
pagination round-trip through the HTTP API) is proven in
`tests/test_query_api.py::TestListEvaluations`.
"""

from datetime import datetime, timedelta, timezone

import pytest

import query.api.cursor as cursor_module
from query.api.cursor import CursorError, decode_cursor, encode_cursor

SECRET = "test-secret"
SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 1, 2, tzinfo=timezone.utc)
OCCURRED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _encode(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "secret": SECRET,
        "tenant_id": "tenant-a",
        "project_id": None,
        "application_id": None,
        "since": SINCE,
        "until": UNTIL,
        "limit": 50,
        "occurred_at": OCCURRED_AT,
        "event_id": "event-1",
    }
    kwargs.update(overrides)
    return encode_cursor(**kwargs)  # type: ignore[arg-type]


def _decode(cursor: str, **overrides: object) -> tuple[datetime, datetime, int, datetime, str]:
    kwargs: dict[str, object] = {
        "secret": SECRET,
        "tenant_id": "tenant-a",
        "project_id": None,
        "application_id": None,
        "since": None,
        "until": None,
        "limit": None,
    }
    kwargs.update(overrides)
    return decode_cursor(cursor, **kwargs)  # type: ignore[arg-type]


def test_round_trip_returns_the_bound_values() -> None:
    cursor = _encode()
    since, until, limit, occurred_at, event_id = _decode(cursor)
    assert (since, until, limit, occurred_at, event_id) == (
        SINCE,
        UNTIL,
        50,
        OCCURRED_AT,
        "event-1",
    )


def test_a_tampered_cursor_is_rejected() -> None:
    cursor = _encode()
    tampered = cursor[:-4] + ("AAAA" if cursor[-4:] != "AAAA" else "BBBB")
    with pytest.raises(CursorError):
        _decode(tampered)


def test_garbage_input_is_rejected_not_crashed_on() -> None:
    with pytest.raises(CursorError):
        _decode("not-a-real-cursor-at-all")


def test_a_different_tenant_is_rejected() -> None:
    cursor = _encode(tenant_id="tenant-a")
    with pytest.raises(CursorError):
        _decode(cursor, tenant_id="tenant-b")


def test_a_different_project_scope_is_rejected() -> None:
    cursor = _encode(project_id="proj-a")
    with pytest.raises(CursorError):
        _decode(cursor, project_id="proj-b")


def test_a_different_application_scope_is_rejected() -> None:
    cursor = _encode(application_id="app-a")
    with pytest.raises(CursorError):
        _decode(cursor, application_id="app-b")


def test_a_continuation_request_resending_a_different_since_is_rejected() -> None:
    cursor = _encode(since=SINCE)
    with pytest.raises(CursorError):
        _decode(cursor, since=SINCE + timedelta(days=1))


def test_a_continuation_request_resending_a_different_until_is_rejected() -> None:
    cursor = _encode(until=UNTIL)
    with pytest.raises(CursorError):
        _decode(cursor, until=UNTIL + timedelta(days=1))


def test_a_continuation_request_resending_a_different_limit_is_rejected() -> None:
    cursor = _encode(limit=50)
    with pytest.raises(CursorError):
        _decode(cursor, limit=10)


def test_a_continuation_request_resending_the_same_window_and_limit_succeeds() -> None:
    cursor = _encode(since=SINCE, until=UNTIL, limit=50)
    since, until, limit, _, _ = _decode(cursor, since=SINCE, until=UNTIL, limit=50)
    assert (since, until, limit) == (SINCE, UNTIL, 50)


def test_an_expired_cursor_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cursor_module, "CURSOR_TTL", timedelta(seconds=-1))
    cursor = _encode()
    with pytest.raises(CursorError):
        _decode(cursor)


def test_an_incompatible_query_version_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cursor_module, "QUERY_VERSION", 999)
    cursor = _encode()  # signed under version 999
    monkeypatch.setattr(cursor_module, "QUERY_VERSION", 1)  # decode expects version 1
    with pytest.raises(CursorError):
        _decode(cursor)


def test_a_cursor_signed_with_a_different_secret_is_rejected() -> None:
    cursor = _encode(secret="secret-a")
    with pytest.raises(CursorError):
        _decode(cursor, secret="secret-b")
