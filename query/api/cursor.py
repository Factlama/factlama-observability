"""OBS-07 cursor binding (ADR-021, corrected 2026-10-24): "Cursors bind
authenticated tenant/project/application scope, resolved window, filters,
sort order, query version and finite expiry, with integrity protection.
Reuse the initial resolved window on continuation; reject changed
context, tampering or expiry."

A bare `(occurred_at, event_id)` position marker is not enough: without
binding it to the request that produced it, a continuation call made
under a different tenant/window/limit (by accident, by a stale bookmark,
or by a tampered value) would silently paginate against the wrong
context instead of failing. This signs the full bound context so any of
those are detected and rejected, never silently applied.
"""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

QUERY_VERSION = 1
"""Bumped whenever the bound-field set or sort order changes meaning --
an old cursor decoded under a new version must be rejected, not
reinterpreted."""

CURSOR_TTL = timedelta(hours=1)
"""Finite expiry (ADR-021). Long enough for a human paging through a
dashboard; short enough that a leaked/bookmarked cursor does not grant
indefinite replay against a since-changed authorization scope."""

SORT_ORDER = "occurred_at_desc"
SORT_ORDER_SPANS = "started_at_desc"
"""`GET /v0.1/ai/requests`'s own sort order -- a distinct value (not
reused from `SORT_ORDER` above) so a cursor minted for one route's keyset
(`occurred_at`/`event_id`) is never silently decoded as the other's
(`started_at`/`span_id`); `decode_span_cursor`'s own sort-order check
rejects a cross-issued cursor outright."""


class CursorError(ValueError):
    """Raised for any invalid, tampered, expired or context-mismatched
    cursor. The caller (`query/api/app.py`) maps this to a 400
    `INVALID_ARGUMENT` -- never a 500, and never silently ignored."""


def encode_cursor(
    *,
    secret: str,
    tenant_id: str,
    project_id: str | None,
    application_id: str | None,
    since: datetime,
    until: datetime,
    limit: int,
    occurred_at: datetime,
    event_id: str,
) -> str:
    """Builds the next page's cursor. `since`/`until`/`limit` are the
    *already-resolved* window this page was served under -- a
    continuation call reuses them verbatim rather than re-resolving
    (ADR-021: "Reuse the initial resolved window on continuation")."""
    payload: dict[str, Any] = {
        "v": QUERY_VERSION,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "application_id": application_id,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "limit": limit,
        "sort": SORT_ORDER,
        "exp": (datetime.now(timezone.utc) + CURSOR_TTL).isoformat(),
        "occurred_at": occurred_at.isoformat(),
        "event_id": event_id,
    }
    return _sign_and_encode(payload, secret)


def decode_cursor(
    cursor: str,
    *,
    secret: str,
    tenant_id: str,
    project_id: str | None,
    application_id: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int | None,
) -> tuple[datetime, datetime, int, datetime, str]:
    """Verifies integrity and expiry, then checks every bound field
    against the request currently presenting this cursor. `since`/
    `until`/`limit` here are whatever the *current* request additionally
    supplied -- `None` means the caller omitted it and is relying on the
    cursor to carry it forward, the expected continuation shape; a
    non-`None` value that disagrees with what the cursor was issued under
    is rejected rather than silently overriding it.

    Returns `(since, until, limit, occurred_at, event_id)` as bound in
    the cursor, for the caller to pass straight through to
    `TelemetryStore.list_reliability_events`."""
    payload = _verify_and_decode(cursor, secret)

    if payload.get("v") != QUERY_VERSION:
        raise CursorError("cursor was issued by an incompatible query version")
    if payload.get("sort") != SORT_ORDER:
        raise CursorError("cursor was issued under a different sort order")

    expires_at = _parse_timestamp(payload, "exp")
    if datetime.now(timezone.utc) >= expires_at:
        raise CursorError("cursor has expired; restart from the first page")

    if payload.get("tenant_id") != tenant_id:
        raise CursorError("cursor was issued for a different tenant")
    if payload.get("project_id") != project_id:
        raise CursorError("cursor was issued for a different project scope")
    if payload.get("application_id") != application_id:
        raise CursorError("cursor was issued for a different application scope")

    cursor_since = _parse_timestamp(payload, "since")
    cursor_until = _parse_timestamp(payload, "until")
    cursor_limit = payload.get("limit")
    if not isinstance(cursor_limit, int):
        raise CursorError("cursor is malformed")

    if since is not None and since != cursor_since:
        raise CursorError("since does not match the window this cursor was issued for")
    if until is not None and until != cursor_until:
        raise CursorError("until does not match the window this cursor was issued for")
    if limit is not None and limit != cursor_limit:
        raise CursorError("limit does not match the page size this cursor was issued for")

    occurred_at = _parse_timestamp(payload, "occurred_at")
    event_id = payload.get("event_id")
    if not isinstance(event_id, str):
        raise CursorError("cursor is malformed")

    return cursor_since, cursor_until, cursor_limit, occurred_at, event_id


def encode_span_cursor(
    *,
    secret: str,
    tenant_id: str,
    project_id: str | None,
    application_id: str | None,
    since: datetime,
    until: datetime,
    limit: int,
    started_at: datetime,
    span_id: str,
) -> str:
    """`encode_cursor`'s own span-level equivalent -- `GET
    /v0.1/ai/requests`'s keyset marker is `(started_at, span_id)`, not
    `(occurred_at, event_id)`."""
    payload: dict[str, Any] = {
        "v": QUERY_VERSION,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "application_id": application_id,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "limit": limit,
        "sort": SORT_ORDER_SPANS,
        "exp": (datetime.now(timezone.utc) + CURSOR_TTL).isoformat(),
        "started_at": started_at.isoformat(),
        "span_id": span_id,
    }
    return _sign_and_encode(payload, secret)


def decode_span_cursor(
    cursor: str,
    *,
    secret: str,
    tenant_id: str,
    project_id: str | None,
    application_id: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int | None,
) -> tuple[datetime, datetime, int, datetime, str]:
    """`decode_cursor`'s own span-level equivalent. Returns `(since,
    until, limit, started_at, span_id)`."""
    payload = _verify_and_decode(cursor, secret)

    if payload.get("v") != QUERY_VERSION:
        raise CursorError("cursor was issued by an incompatible query version")
    if payload.get("sort") != SORT_ORDER_SPANS:
        raise CursorError("cursor was issued under a different sort order")

    expires_at = _parse_timestamp(payload, "exp")
    if datetime.now(timezone.utc) >= expires_at:
        raise CursorError("cursor has expired; restart from the first page")

    if payload.get("tenant_id") != tenant_id:
        raise CursorError("cursor was issued for a different tenant")
    if payload.get("project_id") != project_id:
        raise CursorError("cursor was issued for a different project scope")
    if payload.get("application_id") != application_id:
        raise CursorError("cursor was issued for a different application scope")

    cursor_since = _parse_timestamp(payload, "since")
    cursor_until = _parse_timestamp(payload, "until")
    cursor_limit = payload.get("limit")
    if not isinstance(cursor_limit, int):
        raise CursorError("cursor is malformed")

    if since is not None and since != cursor_since:
        raise CursorError("since does not match the window this cursor was issued for")
    if until is not None and until != cursor_until:
        raise CursorError("until does not match the window this cursor was issued for")
    if limit is not None and limit != cursor_limit:
        raise CursorError("limit does not match the page size this cursor was issued for")

    started_at = _parse_timestamp(payload, "started_at")
    span_id = payload.get("span_id")
    if not isinstance(span_id, str):
        raise CursorError("cursor is malformed")

    return cursor_since, cursor_until, cursor_limit, started_at, span_id


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _sign_and_encode(payload: dict[str, Any], secret: str) -> str:
    signature = hmac.new(secret.encode(), _canonical_json(payload), hashlib.sha256).hexdigest()
    envelope = json.dumps({"payload": payload, "sig": signature}, separators=(",", ":"))
    return base64.urlsafe_b64encode(envelope.encode()).decode("ascii")


def _verify_and_decode(cursor: str, secret: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode()
        envelope = json.loads(raw)
        payload = envelope["payload"]
        signature = envelope["sig"]
    except (ValueError, KeyError, TypeError) as exc:
        raise CursorError("cursor is malformed") from exc
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise CursorError("cursor is malformed")
    expected = hmac.new(secret.encode(), _canonical_json(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise CursorError("cursor failed integrity verification")
    return payload


def _parse_timestamp(payload: dict[str, Any], field: str) -> datetime:
    raw = payload.get(field)
    if not isinstance(raw, str):
        raise CursorError("cursor is malformed")
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CursorError("cursor is malformed") from exc
