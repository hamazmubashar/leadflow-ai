"""A minimal client for a local Ollama server (M9), using only the standard library.

Safety properties, each covered by tests:
- the URL is validated at startup to be loopback only (see ``Settings.llm_url``);
- proxy environment variables (``HTTP_PROXY`` ...) are ignored, so lead notes can never
  leave this computer through a proxy;
- redirects are refused, so a response cannot send the request elsewhere;
- the response size is capped;
- every failure becomes a ``GenerationError`` with a fixed code. No exception, log
  line or message ever contains the prompt, the lead's data or the model's output.

``timeout_seconds`` applies to each socket operation. With ``stream: false`` Ollama
sends nothing until generation ends, so it bounds the wait for the whole answer.
"""

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from leadflow_ai.assist.prompt import Message

MAX_RESPONSE_BYTES = 64 * 1024
MAX_OUTPUT_TOKENS = 400
TEMPERATURE = 0.3
HTTP_NOT_FOUND = 404


class GenerationErrorCode(StrEnum):
    DISABLED = "llm.disabled"
    BUSY = "llm.busy"
    UNAVAILABLE = "llm.unavailable"
    MODEL_NOT_FOUND = "llm.model_not_found"
    TIMEOUT = "llm.timeout"
    INVALID_OUTPUT = "llm.invalid_output"


class GenerationError(Exception):
    """Generation failed. Only the fixed ``code`` is kept; no text is attached."""

    def __init__(self, code: GenerationErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class TextGenerator(Protocol):
    """Anything that turns chat messages into text (Ollama here, a fake in tests)."""

    @property
    def model(self) -> str: ...

    def generate(self, messages: Sequence[Message]) -> str: ...


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None  # urllib then raises HTTPError for the 3xx response


class OllamaGenerator:
    """Calls ``POST {base_url}/api/chat`` once, without streaming."""

    def __init__(self, base_url: str, model: str, timeout_seconds: float) -> None:
        self._url = base_url.rstrip("/") + "/api/chat"
        self._model = model
        self._timeout = timeout_seconds
        # An empty ProxyHandler disables proxies taken from the environment.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _RefuseRedirects()
        )

    @property
    def model(self) -> str:
        return self._model

    def generate(self, messages: Sequence[Message]) -> str:
        payload = {
            "model": self._model,
            "messages": list(messages),
            "stream": False,
            "options": {"temperature": TEMPERATURE, "num_predict": MAX_OUTPUT_TOKENS},
        }
        request = urllib.request.Request(  # noqa: S310 - the URL is validated as loopback http
            self._url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            code = (
                GenerationErrorCode.MODEL_NOT_FOUND
                if exc.code == HTTP_NOT_FOUND
                else GenerationErrorCode.UNAVAILABLE
            )
            raise GenerationError(code) from None
        except TimeoutError:
            raise GenerationError(GenerationErrorCode.TIMEOUT) from None
        except urllib.error.URLError as exc:
            code = (
                GenerationErrorCode.TIMEOUT
                if isinstance(exc.reason, TimeoutError)
                else GenerationErrorCode.UNAVAILABLE
            )
            raise GenerationError(code) from None
        except (OSError, http.client.HTTPException):
            raise GenerationError(GenerationErrorCode.UNAVAILABLE) from None
        return _content(raw)


def _content(raw: bytes) -> str:
    """The assistant text from an Ollama chat response, or INVALID_OUTPUT."""
    if len(raw) > MAX_RESPONSE_BYTES:
        raise GenerationError(GenerationErrorCode.INVALID_OUTPUT)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise GenerationError(GenerationErrorCode.INVALID_OUTPUT) from None
    message = data.get("message") if isinstance(data, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    # "length" means the answer was cut off at MAX_OUTPUT_TOKENS: never store half a text.
    if not isinstance(content, str) or data.get("done_reason") == "length":
        raise GenerationError(GenerationErrorCode.INVALID_OUTPUT)
    return content
