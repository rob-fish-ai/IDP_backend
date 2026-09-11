"""Thin wrapper around the Anthropic Claude API for structured extraction.

Every call goes through one retry policy, one shared client, and one
reading of what the model sent back. A response the pipeline cannot use —
cut off at max_tokens, or not the JSON that was asked for — is a typed
failure (LLMResponseTruncated, LLMInvalidJSON) after one more attempt,
never a partial value handed downstream. Transient API trouble that
outlasts the retries is LLMUnavailable. All three are
ExtractionUnavailableError, which the job layer treats as retryable: the
case waits for the next cycle instead of shipping an empty audit as done.
"""

import base64
import json
import logging
import re
import threading
import time
from pathlib import Path

import anthropic
import httpx

from app.core.config import Settings
from app.core.exceptions import ExtractionUnavailableError

logger = logging.getLogger(__name__)


class LLMUnavailable(ExtractionUnavailableError):
    """The API kept failing transiently past the retry budget."""


class LLMResponseTruncated(ExtractionUnavailableError):
    """The model stopped at max_tokens twice; the output is incomplete."""


class LLMInvalidJSON(ExtractionUnavailableError):
    """The model answered twice with something that is not the JSON asked for."""


# Bound every Anthropic call so a half-closed/stalled connection can't hang the
# worker forever. Read is the long one — extraction responses can take a while.
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)

_MAX_RETRIES = 4
_RATE_LIMIT_BASE_DELAY = 30  # seconds — rate limit window is per minute
_OVERLOAD_BASE_DELAY = 4     # seconds — overload clears faster than rate limits

# The SDK refuses a non-streaming request whose max_tokens implies more
# than ten minutes of generation (about 21k tokens); above this budget the
# request is streamed and the final message assembled.
_STREAM_ABOVE_TOKENS = 20_000
# Ceiling for the one automatic re-ask at a larger budget after a
# truncated response.
_MAX_OUTPUT_TOKENS = 65_536

# Transient errors that are worth retrying. InternalServerError covers the
# 5xx family (it is an APIStatusError, so it must be tested before the
# generic status clause or the generic clause re-raises it as permanent).
# OverloadedError is the Anthropic 529 "overloaded" response — usually
# clears within 10-30 seconds.
_TRANSIENT_EXCS = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.InternalServerError,
)

# Claude 5-family and Opus 4.7+ models reject the temperature parameter with
# a 400; older models (Sonnet 4.6, Haiku 4.5) still accept it.
_NO_TEMPERATURE_PREFIXES = (
    "claude-sonnet-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-fable",
)

# Fable-family models think unconditionally and reject an explicit
# thinking configuration with a 400, so the parameter is never sent to them.
_THINKING_ALWAYS_ON_PREFIXES = ("claude-fable",)


def _thinking_for(settings: Settings, thinking: dict | None) -> dict:
    """The thinking configuration a call sends — always explicit.

    Omitting the parameter means different things on different models
    (adaptive on Sonnet 5, off on the 4.6 family), so a call that does not
    choose gets the configured default rather than the model's.
    """
    if thinking:
        return thinking
    mode = (getattr(settings, "llm_thinking", "disabled") or "disabled").strip().lower()
    if mode in ("adaptive", "on", "enabled"):
        return {"type": "adaptive"}
    return {"type": "disabled"}


def _request_kwargs(
    model: str,
    settings: Settings,
    max_tokens: int | None,
    thinking: dict | None = None,
) -> dict:
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens or settings.llm_max_tokens,
    }
    if not model.startswith(_NO_TEMPERATURE_PREFIXES):
        kwargs["temperature"] = settings.llm_temperature
    if not model.startswith(_THINKING_ALWAYS_ON_PREFIXES):
        kwargs["thinking"] = _thinking_for(settings, thinking)
    return kwargs


_clients: dict[str, anthropic.Anthropic] = {}
_clients_lock = threading.Lock()


def _get_client(settings: Settings) -> anthropic.Anthropic:
    """One client per API key for the life of the process.

    The client owns a connection pool; building one per call (a dozen call
    sites, each retried) opened a fresh pool for every request and threw
    it away."""
    key = settings.anthropic_api_key
    client = _clients.get(key)
    if client is None:
        with _clients_lock:
            client = _clients.get(key)
            if client is None:
                client = anthropic.Anthropic(api_key=key, timeout=_HTTP_TIMEOUT)
                _clients[key] = client
    return client


def _create_message(client: anthropic.Anthropic, kwargs: dict, system: str, messages: list[dict]):
    """Send one request, streaming when the budget calls for it."""
    if kwargs["max_tokens"] > _STREAM_ABOVE_TOKENS:
        with client.messages.stream(**kwargs, system=system, messages=messages) as stream:
            return stream.get_final_message()
    return client.messages.create(**kwargs, system=system, messages=messages)


def _with_retries(label: str, attempt):
    """Run `attempt()` under the shared retry policy.

    Rate limits back off from 30s, overloads (529) and 5xx/network/timeout
    errors from 4s, both doubling; any other API status is permanent and
    raised as is. Exhausting the retries raises LLMUnavailable.
    """
    for n in range(1, _MAX_RETRIES + 1):
        try:
            return attempt()
        except _TRANSIENT_EXCS as exc:
            last, base, what = exc, _OVERLOAD_BASE_DELAY, f"transient API error {type(exc).__name__}"
        except anthropic.RateLimitError as exc:
            last, base, what = exc, _RATE_LIMIT_BASE_DELAY, "rate limit"
        except anthropic.APIStatusError as exc:
            if getattr(exc, "status_code", None) == 529 or "overloaded" in str(exc).lower():
                last, base, what = exc, _OVERLOAD_BASE_DELAY, "API overloaded (529)"
            else:
                raise  # non-retryable: auth, schema, bad request
        if n == _MAX_RETRIES:
            logger.error("%s: %s — giving up after %d attempts", label, what, _MAX_RETRIES)
            raise LLMUnavailable(f"{label}: {what} after {_MAX_RETRIES} attempts: {last}") from last
        delay = base * (2 ** (n - 1))
        logger.warning("%s: %s (attempt %d/%d). Waiting %ds before retry...",
                       label, what, n, _MAX_RETRIES, delay)
        time.sleep(delay)


def _response_text(message) -> str:
    # Models with thinking enabled (Sonnet 5+) open content with thinking
    # blocks — join the text blocks instead of assuming content[0] is text.
    return "".join(b.text for b in message.content if b.type == "text")


def _complete(
    label: str,
    system_prompt: str,
    content,
    settings: Settings,
    *,
    max_tokens: int | None,
    model: str | None,
    thinking: dict | None,
):
    """One completed message under the retry policy."""
    client = _get_client(settings)
    kwargs = _request_kwargs(model or settings.llm_model, settings, max_tokens, thinking)
    messages = [{"role": "user", "content": content}]
    return _with_retries(label, lambda: _create_message(client, kwargs, system_prompt, messages))


def call_llm(
    system_prompt: str,
    user_prompt: str,
    settings: Settings,
    *,
    max_tokens: int | None = None,
    model: str | None = None,
    thinking: dict | None = None,
    label: str = "LLM",
) -> str:
    """Send a prompt to Claude and return the raw text response.

    A response cut off at max_tokens is asked for once more at double the
    budget; a second truncation raises LLMResponseTruncated rather than
    returning the amputated text.

    Args:
        model: Override the default model for this call. Use for routing
               cheaper tasks (classification) to a different model while
               keeping extraction on the default.
        thinking: explicit thinking configuration; None takes the
               configured default (settings.llm_thinking).
    """
    budget = max_tokens or settings.llm_max_tokens
    for attempt in (1, 2):
        message = _complete(label, system_prompt, user_prompt, settings,
                            max_tokens=budget, model=model, thinking=thinking)
        if message.stop_reason != "max_tokens":
            return _response_text(message)
        if attempt == 1 and budget < _MAX_OUTPUT_TOKENS:
            new_budget = min(budget * 2, _MAX_OUTPUT_TOKENS)
            logger.warning(
                "%s: response truncated at max_tokens=%d (model=%s) — asking once more at %d",
                label, budget, message.model, new_budget,
            )
            budget = new_budget
            continue
        raise LLMResponseTruncated(
            f"{label}: response truncated at max_tokens={budget} (model={message.model})"
        )
    raise LLMResponseTruncated(f"{label}: response truncated")  # unreachable


def _parse_json(raw: str, label: str) -> dict:
    """The JSON object in a model response, tolerating a preamble or a
    markdown fence around it. Raises ValueError when there is none."""
    # An empty body is not malformed JSON, and reporting it as
    # "Expecting value: line 1 column 1" hides what actually happened. The
    # cause seen in production: a thinking-enabled model spent the whole
    # max_tokens budget on thinking blocks and emitted no text block at all.
    if not raw.strip():
        raise ValueError(
            f"{label}: model returned no text content. If the model has extended "
            "thinking enabled, max_tokens covers thinking AND output — "
            "raise the budget or pass thinking={'type': 'disabled'}."
        )
    text = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    else:
        start = None
        for i, ch in enumerate(text):
            if ch in ('{', '['):
                start = i
                break
        if start is not None:
            bracket = '}' if text[start] == '{' else ']'
            end = text.rfind(bracket)
            if end > start:
                text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("%s: model returned invalid JSON (%s): %s", label, exc, text[:500])
        raise ValueError(f"{label}: invalid JSON — {exc}") from exc


def call_llm_json(
    system_prompt: str,
    user_prompt: str,
    settings: Settings,
    *,
    max_tokens: int | None = None,
    model: str | None = None,
    thinking: dict | None = None,
    label: str = "LLM",
) -> dict:
    """Send a prompt to Claude and parse the response as JSON.

    The system prompt should instruct the model to return valid JSON only.
    A response that is not JSON is asked for once more; a second failure
    raises LLMInvalidJSON. Truncation is handled by call_llm.
    """
    for attempt in (1, 2):
        raw = call_llm(
            system_prompt, user_prompt, settings,
            max_tokens=max_tokens, model=model, thinking=thinking, label=label,
        )
        try:
            return _parse_json(raw, label)
        except ValueError as exc:
            if attempt == 1:
                logger.warning("%s: %s — asking once more", label, exc)
                continue
            raise LLMInvalidJSON(str(exc)) from exc
    raise LLMInvalidJSON(f"{label}: invalid JSON")  # unreachable


_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _image_content(image_paths: list[str], user_prompt: str) -> list[dict]:
    """Content blocks: images first, then the text."""
    content: list[dict] = []
    for img_path in image_paths:
        path = Path(img_path)
        if not path.exists():
            logger.warning("Vision: image not found: %s", img_path)
            continue
        with open(path, "rb") as f:
            img_data = base64.standard_b64encode(f.read()).decode("utf-8")
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": _MEDIA_TYPES.get(path.suffix.lower(), "image/png"),
                "data": img_data,
            },
        })
    content.append({"type": "text", "text": user_prompt})
    return content


def call_llm_vision(
    system_prompt: str,
    user_prompt: str,
    image_paths: list[str],
    settings: Settings,
    *,
    max_tokens: int | None = None,
    thinking: dict | None = None,
    reject_truncated: bool = False,
    label: str = "Vision",
) -> str:
    """Send images + prompt to Claude Vision and return the raw text response.

    Uses base64-encoded images for multimodal input.

    thinking: passed through as for call_llm; transcription calls disable
        it so the whole max_tokens budget goes to the transcript.
    reject_truncated: return "" when the response stopped at max_tokens —
        a page transcript cut mid-table is worse than no transcript, because
        the caller would replace a complete OCR read with a partial one.
        Otherwise a truncated transcript is returned with a warning: the
        callers of a free-text transcript can use a partial one.
    """
    content = _image_content(image_paths, user_prompt)
    message = _complete(label, system_prompt, content, settings,
                        max_tokens=max_tokens, model=None, thinking=thinking)
    if message.stop_reason == "max_tokens":
        if reject_truncated:
            logger.warning(
                "%s: transcript truncated at max_tokens — discarding it "
                "rather than replacing a complete read with a partial one", label,
            )
            return ""
        logger.warning("%s: response truncated at max_tokens (model=%s) — output is incomplete",
                       label, message.model)
    return _response_text(message)


def call_llm_vision_json(
    system_prompt: str,
    user_prompt: str,
    image_paths: list[str],
    settings: Settings,
    *,
    max_tokens: int | None = None,
    thinking: dict | None = None,
    label: str = "Vision",
) -> dict:
    """Send images + prompt to Claude Vision and parse response as JSON.

    Same contract as call_llm_json: one more ask on a truncated or
    non-JSON answer, then a typed failure.
    """
    content = _image_content(image_paths, user_prompt)
    budget = max_tokens or settings.llm_max_tokens
    for attempt in (1, 2):
        message = _complete(label, system_prompt, content, settings,
                            max_tokens=budget, model=None, thinking=thinking)
        if message.stop_reason == "max_tokens":
            if attempt == 1 and budget < _MAX_OUTPUT_TOKENS:
                budget = min(budget * 2, _MAX_OUTPUT_TOKENS)
                logger.warning("%s: response truncated — asking once more at max_tokens=%d", label, budget)
                continue
            raise LLMResponseTruncated(f"{label}: response truncated at max_tokens={budget}")
        try:
            return _parse_json(_response_text(message), label)
        except ValueError as exc:
            if attempt == 1:
                logger.warning("%s: %s — asking once more", label, exc)
                continue
            raise LLMInvalidJSON(str(exc)) from exc
    raise LLMInvalidJSON(f"{label}: invalid JSON")  # unreachable
