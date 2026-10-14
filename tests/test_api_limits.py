"""BodySizeLimitMiddleware at the ASGI level: pass-through, disconnects, bad headers."""

import asyncio
from collections.abc import Awaitable, Callable

from starlette.types import Message, Receive, Scope, Send

from leadflow_ai.api.limits import BodySizeLimitMiddleware


class RecordingApp:
    def __init__(self) -> None:
        self.calls: list[tuple[Scope, list[Message]]] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        received = [await receive()] if scope["type"] == "http" else []
        self.calls.append((scope, received))


def _receiver(messages: list[Message]) -> Callable[[], Awaitable[Message]]:
    queue = list(messages)

    async def receive() -> Message:
        return queue.pop(0)

    return receive


def _run(
    scope: Scope, messages: list[Message], max_bytes: int = 10
) -> tuple[RecordingApp, list[Message]]:
    app = RecordingApp()
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(BodySizeLimitMiddleware(app, max_bytes=max_bytes)(scope, _receiver(messages), send))
    return app, sent


def _http(headers: list[tuple[bytes, bytes]] | None = None) -> Scope:
    return {"type": "http", "method": "POST", "path": "/", "headers": headers or []}


def test_non_http_scopes_pass_through_untouched() -> None:
    for scope_type in ("lifespan", "websocket"):
        scope: Scope = {"type": scope_type, "headers": []}
        app, sent = _run(scope, [])
        assert app.calls == [(scope, [])]
        assert sent == []


def test_client_disconnect_before_body_does_not_call_app() -> None:
    app, sent = _run(_http(), [{"type": "http.disconnect"}])
    assert app.calls == []
    assert sent == []


def test_invalid_content_length_falls_back_to_counting() -> None:
    headers = [(b"content-length", b"not-a-number")]
    app, sent = _run(
        _http(headers), [{"type": "http.request", "body": b"small", "more_body": False}]
    )
    assert app.calls[0][1] == [{"type": "http.request", "body": b"small", "more_body": False}]
    assert sent == []

    app, sent = _run(
        _http(headers),
        [{"type": "http.request", "body": b"far too large body", "more_body": False}],
    )
    assert app.calls == []
    assert sent[0]["status"] == 413


def test_multi_chunk_body_is_replayed_as_one_message() -> None:
    app, _ = _run(
        _http(),
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        ],
    )
    assert app.calls[0][1] == [{"type": "http.request", "body": b"abcdef", "more_body": False}]


def test_after_replay_receive_reads_from_the_client() -> None:
    later: list[Message] = []

    class TwoReads:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await receive()
            later.append(await receive())

    async def main() -> None:
        receive = _receiver(
            [
                {"type": "http.request", "body": b"x", "more_body": False},
                {"type": "http.disconnect"},
            ]
        )

        async def send(message: Message) -> None:
            raise AssertionError("nothing should be sent")

        await BodySizeLimitMiddleware(TwoReads(), max_bytes=10)(_http(), receive, send)

    asyncio.run(main())
    assert later == [{"type": "http.disconnect"}]
