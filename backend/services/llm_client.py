"""
backend/services/llm_client.py
============================================================
PURPOSE:
    The single, authoritative wrapper for all Anthropic API calls.
    No other file in this codebase should import or instantiate
    the Anthropic SDK directly.

    Provides two public functions:
      complete()            — single-turn text completion
      complete_with_tools() — single-turn with tool use (for ReAct agent)

WHY ONE WRAPPER:
    Centralizing all API calls means:
    - API key handling is in exactly one place
    - Retry logic, timeouts, and error mapping are consistent
    - Swapping models or adding logging requires one change, not many
    - Tests can mock one module instead of many call sites

SECURITY CONTRACT (from CLAUDE.md — non-negotiable):
    The Anthropic API is NEVER called before PII redaction is complete.
    This file cannot enforce that because it accepts pre-built messages
    and does not know whether their content was redacted.
    ENFORCEMENT IS THE CALLER'S RESPONSIBILITY:
      anomaly_detector.py and dispute_generator.py both accept
      RedactedBill (from api/models.py) and build messages from it.
      RedactedBill can only be created after assert_no_pii_leak() passes
      in pii_redactor.py. That chain is the enforcement mechanism.

SDK VERSION: anthropic==0.28.0 (pinned in requirements.txt)
============================================================
"""

import logging
import os
from typing import Optional

import anthropic

from backend.config import settings

log = logging.getLogger(__name__)


# ============================================================
# CUSTOM EXCEPTIONS
#
# WHY CUSTOM EXCEPTIONS (not re-raise SDK exceptions):
#   routes.py needs to map failures to HTTP status codes.
#   It should not import the anthropic SDK just to catch its
#   exception types. Our exceptions are the stable API surface;
#   the SDK exceptions are an implementation detail.
# ============================================================

class LLMError(Exception):
    """
    Base class for all LLM client errors.

    Raised when the Anthropic API call fails for an unknown or generic reason.
    routes.py maps this to HTTP 500.
    """


class LLMAuthenticationError(LLMError):
    """
    Raised when the ANTHROPIC_API_KEY is missing, empty, or rejected.

    This is always an operator configuration error, never a user error.
    routes.py maps this to HTTP 500 with a generic "service unavailable"
    message — do not expose the word "API key" to end users.
    """


class LLMRateLimitError(LLMError):
    """
    Raised when Anthropic returns HTTP 429 (rate limit exceeded).

    Our slowapi middleware limits 10 requests/minute/IP, which should
    prevent us from ever hitting Anthropic's rate limits under normal use.
    If this fires, it means either the rate limit is too high, or multiple
    users are hitting the system simultaneously.
    routes.py maps this to HTTP 429 so the frontend can tell the user to wait.
    """


# ============================================================
# CONSTANTS
# ============================================================

# Model to use for all API calls.
# Per CLAUDE.md decision table: claude-sonnet-4-20250514 for
# best balance of speed and quality.
# WHY ENV VAR: allows A/B testing or emergency model swap
# without a code change or container rebuild.
_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

# SDK-level timeout in seconds. This is the total time the HTTP
# request is allowed to take, including streaming.
# WHY 120s: complex bills with many line items can take 30-60s.
# 120s is a safe ceiling. FastAPI's own request timeout should be
# set higher than this in production (nginx/proxy layer).
_TIMEOUT = float(os.getenv("ANTHROPIC_TIMEOUT_SECONDS", "120"))

# SDK-level retries for transient errors (5xx, 529 overloaded).
# WHY 2: one immediate retry catches transient blips without
# tripling latency. Rate limit 429s are NOT retried by the SDK
# at this setting — they surface as RateLimitError immediately.
_MAX_RETRIES = int(os.getenv("ANTHROPIC_MAX_RETRIES", "2"))

# Default max_tokens for simple text completion (dispute letters, etc.)
# WHY 2048: dispute letters are typically 300-600 words (~400-800 tokens).
# 2048 gives comfortable headroom without paying for unused capacity.
_DEFAULT_MAX_TOKENS_TEXT = 2048

# Default max_tokens for tool-use completion (ReAct agent).
# WHY 4096: the agent may reason over many line items before calling
# a tool. Enough space for a full chain-of-thought + tool call JSON.
_DEFAULT_MAX_TOKENS_TOOLS = 4096


# ============================================================
# SINGLETON CLIENT
# ============================================================

# Module-level singleton. Reusing one AsyncAnthropic instance across
# requests means connection pooling happens at the SDK level.
# WHY LAZY (None until first call): the container might start before
# the operator sets ANTHROPIC_API_KEY, and we want a clear error at
# first use, not a crash at import time.
_client: Optional[anthropic.AsyncAnthropic] = None


def _get_client() -> anthropic.AsyncAnthropic:
    """
    Return the cached AsyncAnthropic client, creating it on first call.

    WHAT: Lazy-initializes the module-level singleton. Validates that
    ANTHROPIC_API_KEY is present and non-empty before creating the client.

    WHY AsyncAnthropic (not Anthropic):
        FastAPI runs an async event loop. A synchronous Anthropic client
        would block the event loop for the entire duration of each API call
        (5-60 seconds), preventing any other request from being handled.
        AsyncAnthropic uses httpx's async transport — it yields control
        back to the event loop while waiting for the API response.

    WHY VALIDATE KEY EXPLICITLY:
        The SDK will also fail on a missing key, but with a cryptic
        error message. Explicit validation gives a clear LLMAuthenticationError
        that routes.py can handle gracefully.

    RAISES:
        LLMAuthenticationError: if ANTHROPIC_API_KEY is not set or empty.
    """
    global _client
    if _client is not None:
        return _client

    # WHY settings.anthropic_api_key.get_secret_value() (not os.getenv):
    #   settings.anthropic_api_key is a SecretStr — pydantic renders it as "**********"
    #   in repr() and str(), preventing accidental logging. os.getenv returns a plain
    #   string that could appear in tracebacks or be assigned to a variable that is
    #   later logged. get_secret_value() is the only safe way to retrieve the raw key,
    #   and only at the point of use (here, passing to the Anthropic SDK).
    api_key = settings.anthropic_api_key.get_secret_value().strip()

    if not api_key:
        raise LLMAuthenticationError(
            "ANTHROPIC_API_KEY is not set. "
            "Add it to your .env file and restart the container."
        )

    log.info(f"Initializing Anthropic client (model={_MODEL}, timeout={_TIMEOUT}s)")

    _client = anthropic.AsyncAnthropic(
        api_key=api_key,
        max_retries=_MAX_RETRIES,
        timeout=_TIMEOUT,
    )
    return _client


# ============================================================
# PUBLIC API
# ============================================================

async def complete(
    messages: list[dict],
    *,
    system: str,
    max_tokens: int = _DEFAULT_MAX_TOKENS_TEXT,
    temperature: float = 0.0,
) -> str:
    """
    Single-turn text completion. Returns the model's text response.

    WHAT:
        Sends messages to the Anthropic Messages API and returns the
        text content of the response as a plain string.

    WHEN TO USE:
        For calls where you need prose text back — dispute letter
        generation, summarization. No tool use.

    WHY temperature=0.0 DEFAULT:
        Anomaly detection and dispute letters must be reproducible.
        The same bill scanned twice should produce the same output.
        Callers can override for any case where variation is wanted.

    ARGS:
        messages:   List of message dicts in Anthropic format:
                    [{"role": "user", "content": "..."}, ...]
                    Must alternate user/assistant, starting with user.
        system:     System prompt. Sets the model's persona and constraints.
                    Build this from non-PII context only.
        max_tokens: Maximum tokens in the response. Default 2048.
        temperature: Sampling temperature 0.0-1.0. Default 0.0 (deterministic).

    RETURNS:
        The model's text response as a string.
        If the response contains multiple text blocks, they are
        joined with newlines.

    RAISES:
        LLMAuthenticationError: bad or missing API key
        LLMRateLimitError:      Anthropic rate limit hit
        LLMError:               any other API failure
    """
    client = _get_client()

    log.debug(
        f"LLM complete: model={_MODEL} max_tokens={max_tokens} "
        f"temp={temperature} messages={len(messages)}"
    )

    try:
        response = await client.messages.create(
            model=_MODEL,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except anthropic.AuthenticationError as exc:
        raise LLMAuthenticationError(
            "Anthropic API rejected the API key. Check ANTHROPIC_API_KEY."
        ) from exc
    except anthropic.RateLimitError as exc:
        raise LLMRateLimitError(
            "Anthropic rate limit reached. Reduce RATE_LIMIT_PER_MINUTE or wait."
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError(
            f"Could not reach Anthropic API (network error): {exc}"
        ) from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(
            f"Anthropic API returned error {exc.status_code}: {exc.message}"
        ) from exc

    # WHY CHECK stop_reason:
    #   "max_tokens" means the response was truncated. A truncated dispute
    #   letter or analysis is dangerous — it may be incomplete mid-sentence.
    #   We log a warning and return the partial text. The caller decides
    #   whether to retry with a higher max_tokens or surface an error.
    if response.stop_reason == "max_tokens":
        log.warning(
            f"LLM response was truncated (stop_reason=max_tokens, "
            f"max_tokens={max_tokens}). Consider increasing max_tokens."
        )

    # Extract text from content blocks.
    # WHY NOT response.content[0].text:
    #   The first content block is not guaranteed to be a TextBlock.
    #   In rare cases the API may return a mix of block types.
    #   We gather all text blocks explicitly.
    text_blocks = [
        block.text
        for block in response.content
        if block.type == "text"
    ]

    if not text_blocks:
        raise LLMError(
            f"Anthropic response contained no text blocks. "
            f"stop_reason={response.stop_reason}, "
            f"content_types={[b.type for b in response.content]}"
        )

    result = "\n".join(text_blocks)

    log.debug(
        f"LLM complete done: stop_reason={response.stop_reason} "
        f"input_tokens={response.usage.input_tokens} "
        f"output_tokens={response.usage.output_tokens}"
    )
    return result


async def complete_with_tools(
    messages: list[dict],
    *,
    system: str,
    tools: list[dict],
    max_tokens: int = _DEFAULT_MAX_TOKENS_TOOLS,
) -> anthropic.types.Message:
    """
    Single-turn tool-use completion. Returns the full Message object.

    WHAT:
        Sends messages with a tool schema to the Anthropic Messages API.
        Returns the raw Message — NOT just the text — so the ReAct agent
        can inspect stop_reason and content blocks to drive its loop.

    WHEN TO USE:
        Only from react_agent.py. The ReAct loop calls this repeatedly,
        each time appending the previous assistant turn and tool results
        to messages, until stop_reason == "end_turn".

    WHY RETURN Message NOT str:
        The caller needs to know:
          1. Did the model call a tool? (stop_reason == "tool_use")
          2. Which tool? (content block with type == "tool_use")
          3. What arguments? (content block .input dict)
          4. Is the loop done? (stop_reason == "end_turn")
        A string return would strip all of that information.

    WHY NO temperature PARAMETER:
        Tool-use calls should be maximally deterministic. The model is
        selecting tools and arguments — randomness here produces
        unpredictable agent behavior. Temperature is fixed at 0.0.
        This is intentional, not an oversight.

    ARGS:
        messages: Conversation history in Anthropic format.
                  Includes prior assistant turns and tool_result turns.
        system:   System prompt for the agent persona and task.
        tools:    Tool definitions in Anthropic tool schema format:
                  [{"name": str, "description": str, "input_schema": {...}}, ...]
        max_tokens: Maximum tokens for this turn. Default 4096.

    RETURNS:
        anthropic.types.Message — inspect .stop_reason and .content.
        Typical patterns for the caller:
          stop_reason == "tool_use"  → find ToolUseBlock in .content, call tool
          stop_reason == "end_turn"  → find TextBlock in .content, done

    RAISES:
        LLMAuthenticationError: bad or missing API key
        LLMRateLimitError:      Anthropic rate limit hit
        LLMError:               any other API failure
    """
    client = _get_client()

    log.debug(
        f"LLM complete_with_tools: model={_MODEL} max_tokens={max_tokens} "
        f"tools={[t['name'] for t in tools]} messages={len(messages)}"
    )

    try:
        response = await client.messages.create(
            model=_MODEL,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=0.0,
            # WHY 0.0: temperature=0 is fully supported with tool use on
            # claude-sonnet-4-20250514 (verified against Anthropic API docs:
            # platform.claude.com/docs/en/api/messages/create.md).
            # Tool selection must be deterministic — the same bill should
            # produce the same agent decisions on every run.
        )
    except anthropic.AuthenticationError as exc:
        raise LLMAuthenticationError(
            "Anthropic API rejected the API key. Check ANTHROPIC_API_KEY."
        ) from exc
    except anthropic.RateLimitError as exc:
        raise LLMRateLimitError(
            "Anthropic rate limit reached. Reduce RATE_LIMIT_PER_MINUTE or wait."
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError(
            f"Could not reach Anthropic API (network error): {exc}"
        ) from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(
            f"Anthropic API returned error {exc.status_code}: {exc.message}"
        ) from exc

    if response.stop_reason == "max_tokens":
        log.warning(
            f"LLM tool-use response truncated (stop_reason=max_tokens, "
            f"max_tokens={max_tokens}). Agent turn may be incomplete."
        )

    log.debug(
        f"LLM complete_with_tools done: stop_reason={response.stop_reason} "
        f"input_tokens={response.usage.input_tokens} "
        f"output_tokens={response.usage.output_tokens}"
    )
    return response


def reset_singleton() -> None:
    """
    Clear the cached client singleton.

    WHEN TO USE:
        Tests only. Allows a test to inject a different API key or
        mock client without process restart.
        Not for production use.
    """
    global _client
    _client = None
    log.debug("LLM client singleton cleared")
