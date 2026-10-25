"""F3 (review-evidence/g6-coverage-fix-review-2026-10-25): both services'
previous deadline enforcement used `@app.middleware("http")` --
Starlette's `BaseHTTPMiddleware`. That machinery runs the real
downstream handler in its own task, started via `task_group.start_soon`
*inside* `call_next` (`starlette.middleware.base.BaseHTTPMiddleware.
__call__`); wrapping `call_next(request)` itself in `asyncio.wait_for`
only cancels the wait for its first response message -- a separate task
from `asyncio.wait_for`'s own perspective -- never the handler task
itself. The real handler, and any side effects or resource use it has
already started, keeps running to completion regardless of the
timeout; only the *response* races it. A real ASGI reproduction (a 30ms
deadline against a handler that sleeps 300ms) confirmed this: the
response was a prompt 503, but the handler still completed and the
underlying ASGI call did not actually finish until the full 300ms+ had
elapsed.

This module wraps the real downstream ASGI callable (`self.app(scope,
receive, send)`) directly in `asyncio.wait_for`, as a plain ASGI
middleware -- never `BaseHTTPMiddleware`. That coroutine, once wrapped
in a single task by `wait_for`, *is* the handler's entire call stack
(FastAPI routing, dependency injection, the route coroutine, and
anything it awaits) -- cancelling that one task delivers cancellation
wherever the handler currently is, including inside any nested
`BaseHTTPMiddleware` layer's own task group (anyio's structured
concurrency cancels a task group's children when the task running its
own body is cancelled), which is exactly the propagation the previous
approach never achieved.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

Scope = Mapping[str, Any]
Message = Mapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class RequestDeadlineMiddleware:
    """Bounds total wall-clock handling time for one HTTP request,
    regardless of how many individual calls (database or otherwise) it
    makes -- each already bounded by `command_timeout` individually
    (`storage/postgres/engine.py`), but not in aggregate. `state` is the
    owning FastAPI app's own `app.state` object (read fresh on every
    request, so tests can swap `app.state.request_deadline_seconds` the
    same way they already swap `app.state.concurrency_limiter`)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        state: Any,
        exempt_paths: frozenset[str],
        build_timeout_response: Callable[[], tuple[int, bytes]],
    ) -> None:
        self.app = app
        self._state = state
        self._exempt_paths = exempt_paths
        self._build_timeout_response = build_timeout_response

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("path") in self._exempt_paths:
            await self.app(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        deadline: float = self._state.request_deadline_seconds
        try:
            await asyncio.wait_for(self.app(scope, receive, tracking_send), timeout=deadline)
        except TimeoutError:
            if response_started:
                # Already streaming a real response by the time the
                # deadline fired -- too late to swap it for a synthetic
                # one; the client sees an abruptly closed connection
                # rather than a clean error body, a real but accepted
                # limitation of enforcing the deadline at this level.
                logger.warning(
                    "request exceeded its %.3fs deadline after a response had already started",
                    deadline,
                )
                return
            status_code, body = self._build_timeout_response()
            await send(
                {
                    "type": "http.response.start",
                    "status": status_code,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body, "more_body": False})
