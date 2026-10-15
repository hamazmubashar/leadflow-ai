"""AI-assisted draft bodies from a local language model (M9). Disabled by default."""

from leadflow_ai.assist.ollama import (
    GenerationError,
    GenerationErrorCode,
    OllamaGenerator,
    TextGenerator,
)
from leadflow_ai.assist.prompt import (
    PROMPT_VERSION,
    SIGN_OFF,
    SYSTEM_PROMPT,
    AssistInput,
    InvalidOutputError,
    LeadContext,
    Message,
    build_messages,
    check_generated,
    compose_body,
)

__all__ = [
    "PROMPT_VERSION",
    "SIGN_OFF",
    "SYSTEM_PROMPT",
    "AssistInput",
    "GenerationError",
    "GenerationErrorCode",
    "InvalidOutputError",
    "LeadContext",
    "Message",
    "OllamaGenerator",
    "TextGenerator",
    "build_messages",
    "check_generated",
    "compose_body",
]
