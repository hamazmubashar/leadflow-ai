"""Reject oversized request bodies before any handler or parser sees them."""

import json
from collections.abc import Mapping

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BODY_BYTES = 16 * 1024
MAX_IMPORT_BODY_BYTES = 1024 * 1024  # CSV import route only


class BodySizeLimitMiddleware:
    """Return 413 for bodies over ``max_bytes``, whether or not Content-Length is sent.

    The body is buffered (at most the applicable limit) and replayed to the app,
    so a chunked upload is cut off as soon as it crosses the limit.
    ``path_limits`` overrides ``max_bytes`` for exact request paths.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int = MAX_REQUEST_BODY_BYTES,
        path_limits: Mapping[str, int] | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.path_limits = dict(path_limits or {})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.path_limits.get(scope["path"], self.max_bytes)
        declared = _content_length(scope)
        if declared is not None and declared > limit:
            await self._reject(send, limit)
            return

        chunks: list[bytes] = []
        size = 0
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] != "http.request":
                return  # client disconnected before sending the body
            body = message.get("body", b"")
            size += len(body)
            if size > limit:
                await self._reject(send, limit)
                return
            chunks.append(body)
            more_body = message.get("more_body", False)

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _reject(self, send: Send, limit: int) -> None:
        body = json.dumps(
            {
                "detail": {
                    "code": "request.too_large",
                    "message": f"Request body must be at most {limit} bytes",
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _content_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
