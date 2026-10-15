"""M9 Ollama client against a real HTTP server on 127.0.0.1 inside the test process.

No Ollama and no outside network: the suite-wide network guard stays on for every
address except the loopback test server, so a request that tried to use a proxy or
follow a redirect elsewhere would fail loudly.
"""

import json
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from leadflow_ai.assist import GenerationError, GenerationErrorCode, Message, OllamaGenerator
from leadflow_ai.assist.ollama import MAX_OUTPUT_TOKENS, MAX_RESPONSE_BYTES

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
MESSAGES: list[Message] = [
    {"role": "system", "content": "system rules"},
    {"role": "user", "content": "lead data"},
]
_REAL_CONNECT = socket.socket.connect
_REAL_CREATE_CONNECTION = socket.create_connection
_REAL_GETADDRINFO = socket.getaddrinfo

Handler = Callable[[BaseHTTPRequestHandler], None]


@pytest.fixture(autouse=True)
def _allow_loopback_only(_block_network: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-open loopback (only) after the suite's network guard has closed everything.

    Requesting ``_block_network`` makes sure the guard is applied first.
    """

    def connect(sock: socket.socket, address: Any) -> None:
        if address[0] not in LOOPBACK:
            raise AssertionError(f"non-loopback connection attempted: {address[0]}")
        _REAL_CONNECT(sock, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in LOOPBACK:
            raise AssertionError(f"non-loopback lookup attempted: {host}")
        return _REAL_GETADDRINFO(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", _REAL_CREATE_CONNECTION)


class FakeOllama:
    """A loopback HTTP server; ``respond`` decides what each request gets."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        self.respond: Handler = lambda handler: _json(handler, _chat("A reply."))
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                outer.requests.append((self.path, dict(self.headers), self.rfile.read(length)))
                outer.respond(self)

            def log_message(self, *_args: object) -> None:  # keep test output quiet
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _chat(content: object, **extra: object) -> dict[str, object]:
    return {"model": "qwen2.5:3b", "message": {"role": "assistant", "content": content}} | extra


def _send(handler: BaseHTTPRequestHandler, status: int, body: bytes, **headers: str) -> None:
    handler.send_response(status)
    for name, value in {"Content-Length": str(len(body)), **headers}.items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def _json(handler: BaseHTTPRequestHandler, data: object, status: int = 200) -> None:
    _send(handler, status, json.dumps(data).encode(), **{"Content-Type": "application/json"})


@pytest.fixture
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama()
    yield server
    server.close()


def _generator(url: str, timeout: float = 5.0) -> OllamaGenerator:
    return OllamaGenerator(url, "qwen2.5:3b", timeout)


def _code(generator: OllamaGenerator) -> GenerationErrorCode:
    with pytest.raises(GenerationError) as caught:
        generator.generate(MESSAGES)
    error = caught.value
    assert str(error) == error.code.value  # nothing but the fixed code
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__  # no chained details
    return error.code


# --- success ----------------------------------------------------------------------------


def test_one_non_streaming_chat_request(ollama: FakeOllama) -> None:
    generator = _generator(ollama.url + "/")
    assert generator.model == "qwen2.5:3b"
    assert generator.generate(MESSAGES) == "A reply."
    [(path, headers, body)] = ollama.requests
    assert path == "/api/chat"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {
        "model": "qwen2.5:3b",
        "messages": MESSAGES,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": MAX_OUTPUT_TOKENS},
    }


def test_proxy_environment_variables_are_ignored(
    ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a proxy honoured, the request would go to 192.0.2.1 and trip the guard."""
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://192.0.2.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    assert _generator(ollama.url).generate(MESSAGES) == "A reply."
    assert len(ollama.requests) == 1


# --- failures map to fixed codes ------------------------------------------------------------


def test_connection_refused_is_unavailable() -> None:
    with socket.socket() as probe:  # find a free port, then leave it closed
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert _code(_generator(f"http://127.0.0.1:{port}")) is GenerationErrorCode.UNAVAILABLE


def test_a_missing_model_is_model_not_found(ollama: FakeOllama) -> None:
    ollama.respond = lambda h: _json(h, {"error": "model 'qwen2.5:3b' not found"}, status=404)
    assert _code(_generator(ollama.url)) is GenerationErrorCode.MODEL_NOT_FOUND


def test_a_server_error_is_unavailable(ollama: FakeOllama) -> None:
    ollama.respond = lambda h: _json(h, {"error": "out of memory"}, status=500)
    assert _code(_generator(ollama.url)) is GenerationErrorCode.UNAVAILABLE


def test_redirects_are_refused(ollama: FakeOllama) -> None:
    ollama.respond = lambda h: _send(h, 307, b"", Location=ollama.url + "/elsewhere")
    assert _code(_generator(ollama.url)) is GenerationErrorCode.UNAVAILABLE
    assert [path for path, _, _ in ollama.requests] == ["/api/chat"]  # never followed


def test_no_answer_before_the_timeout_is_timeout(ollama: FakeOllama) -> None:
    ollama.respond = lambda h: time.sleep(1.5)
    assert _code(_generator(ollama.url, timeout=0.3)) is GenerationErrorCode.TIMEOUT


def test_a_body_that_stalls_after_the_headers_is_timeout(ollama: FakeOllama) -> None:
    def stall(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        handler.wfile.flush()
        time.sleep(1.5)

    ollama.respond = stall
    assert _code(_generator(ollama.url, timeout=0.3)) is GenerationErrorCode.TIMEOUT


def test_a_body_cut_short_is_invalid_output(ollama: FakeOllama) -> None:
    def truncated(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        handler.wfile.write(b'{"message"')
        handler.close_connection = True

    ollama.respond = truncated
    assert _code(_generator(ollama.url)) is GenerationErrorCode.INVALID_OUTPUT


def test_a_connection_reset_while_reading_is_unavailable(ollama: FakeOllama) -> None:
    def reset(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        handler.wfile.flush()
        time.sleep(0.2)  # let the client start reading the body
        # SO_LINGER 0: close() sends a TCP reset instead of a normal end of stream.
        handler.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        handler.connection.close()

    ollama.respond = reset
    assert _code(_generator(ollama.url)) is GenerationErrorCode.UNAVAILABLE


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe",
        b"[]",
        json.dumps({"message": "text"}).encode(),
        json.dumps(_chat(None)).encode(),
        json.dumps(_chat(42)).encode(),
        json.dumps(_chat("cut off mid-sent", done_reason="length")).encode(),
        json.dumps(_chat("x" * MAX_RESPONSE_BYTES)).encode(),
    ],
    ids=[
        "not-json",
        "not-utf8",
        "not-an-object",
        "message-not-an-object",
        "no-content",
        "content-not-text",
        "truncated-by-token-limit",
        "too-large",
    ],
)
def test_malformed_responses_are_invalid_output(ollama: FakeOllama, body: bytes) -> None:
    ollama.respond = lambda h: _send(h, 200, body)
    assert _code(_generator(ollama.url)) is GenerationErrorCode.INVALID_OUTPUT


def test_a_complete_answer_is_accepted_whatever_the_done_reason(ollama: FakeOllama) -> None:
    ollama.respond = lambda h: _json(h, _chat("Fine.", done_reason="stop"))
    assert _generator(ollama.url).generate(MESSAGES) == "Fine."
