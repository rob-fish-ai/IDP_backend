"""Thin wrapper around the Anthropic Claude API for structured extraction."""

import json
import logging
import re
import time

import anthropic
import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)

# Bound every Anthropic call so a half-closed/stalled connection can't hang the
# worker forever. Read is the long one — extraction responses can take a while.
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)

_MAX_RETRIES = 4
_RATE_LIMIT_BASE_DELAY = 30  # seconds — rate limit window is per minute
_OVERLOAD_BASE_DELAY = 4     # seconds — overload clears faster than rate limits

# Transient errors that are worth retrying. OverloadedError is the Anthropic
# 529 "overloaded" response — usually clears within 10-30 seconds.
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


def _request_kwargs(model: str, settings: Settings, max_tokens: int | None) -> dict:
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens or settings.llm_max_tokens,
    }
    if not model.startswith(_NO_TEMPERATURE_PREFIXES):
        kwargs["temperature"] = settings.llm_temperature
    return kwargs


def _get_client(settings: Settings) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=_HTTP_TIMEOUT)


def _response_text(message) -> str:
    # Models with thinking enabled (Sonnet 5+) open content with thinking
    # blocks — join the text blocks instead of assuming content[0] is text.
    return "".join(b.text for b in message.content if b.type == "text")


def call_llm(
    system_prompt: str,
    user_prompt: str,
    settings: Settings,
    *,
    max_tokens: int | None = None,
    model: str | None = None,
) -> str:
    """Send a prompt to Claude and return the raw text response.

    Retries on rate limit errors with exponential backoff.

    Args:
        model: Override the default model for this call. Use for routing
               cheaper tasks (classification) to Haiku while keeping
               extraction on Sonnet.
    """
    client = _get_client(settings)
    chosen_model = model or settings.llm_model

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            message = client.messages.create(
                **_request_kwargs(chosen_model, settings, max_tokens),
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return _response_text(message)

        except (anthropic.RateLimitError, anthropic.APIStatusError) as exc:
            # OverloadedError (529) is an APIStatusError subclass.
            # Rate limits use a longer base delay than overloads.
            if isinstance(exc, anthropic.RateLimitError):
                base = _RATE_LIMIT_BASE_DELAY
                label = "Rate limit"
            elif getattr(exc, "status_code", None) == 529 or "overloaded" in str(exc).lower():
                base = _OVERLOAD_BASE_DELAY
                label = "API overloaded (529)"
            else:
                raise  # non-retryable APIStatusError

            if attempt == _MAX_RETRIES:
                logger.error("%s — giving up after %d attempts", label, _MAX_RETRIES)
                raise
            delay = base * (2 ** (attempt - 1))
            logger.warning(
                "%s (attempt %d/%d). Waiting %ds before retry...",
                label, attempt, _MAX_RETRIES, delay,
            )
            time.sleep(delay)

        except _TRANSIENT_EXCS as exc:
            if attempt == _MAX_RETRIES:
                logger.error(
                    "Transient API error %s — giving up after %d attempts",
                    type(exc).__name__, _MAX_RETRIES,
                )
                raise
            delay = _OVERLOAD_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "Transient API error %s (attempt %d/%d). Waiting %ds...",
                type(exc).__name__, attempt, _MAX_RETRIES, delay,
            )
            time.sleep(delay)


def call_llm_vision(
    system_prompt: str,
    user_prompt: str,
    image_paths: list[str],
    settings: Settings,
    *,
    max_tokens: int | None = None,
) -> str:
    """Send images + prompt to Claude Vision and return the raw text response.

    Uses base64-encoded images for multimodal input.
    """
    import base64
    from pathlib import Path

    client = _get_client(settings)

    # Build content blocks: images first, then text
    content: list[dict] = []
    for img_path in image_paths:
        path = Path(img_path)
        if not path.exists():
            logger.warning("Vision: image not found: %s", img_path)
            continue

        with open(path, "rb") as f:
            img_data = base64.standard_b64encode(f.read()).decode("utf-8")

        # Determine media type
        suffix = path.suffix.lower()
        media_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(suffix, "image/png")

        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": img_data,
            },
        })

    content.append({"type": "text", "text": user_prompt})

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            message = client.messages.create(
                **_request_kwargs(settings.llm_model, settings, max_tokens),
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
            )
            return _response_text(message)

        except (anthropic.RateLimitError, anthropic.APIStatusError) as exc:
            if isinstance(exc, anthropic.RateLimitError):
                base = _RATE_LIMIT_BASE_DELAY
                label = "Vision: Rate limit"
            elif getattr(exc, "status_code", None) == 529 or "overloaded" in str(exc).lower():
                base = _OVERLOAD_BASE_DELAY
                label = "Vision: API overloaded (529)"
            else:
                raise

            if attempt == _MAX_RETRIES:
                logger.error("%s — giving up after %d attempts", label, _MAX_RETRIES)
                raise
            delay = base * (2 ** (attempt - 1))
            logger.warning(
                "%s (attempt %d/%d). Waiting %ds...",
                label, attempt, _MAX_RETRIES, delay,
            )
            time.sleep(delay)

        except _TRANSIENT_EXCS as exc:
            if attempt == _MAX_RETRIES:
                logger.error(
                    "Vision: transient API error %s — giving up after %d attempts",
                    type(exc).__name__, _MAX_RETRIES,
                )
                raise
            delay = _OVERLOAD_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "Vision: transient API error %s (attempt %d/%d). Waiting %ds...",
                type(exc).__name__, attempt, _MAX_RETRIES, delay,
            )
            time.sleep(delay)


def call_llm_vision_json(
    system_prompt: str,
    user_prompt: str,
    image_paths: list[str],
    settings: Settings,
    *,
    max_tokens: int | None = None,
) -> dict:
    """Send images + prompt to Claude Vision and parse response as JSON."""
    raw = call_llm_vision(
        system_prompt, user_prompt, image_paths, settings,
        max_tokens=max_tokens,
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
    except json.JSONDecodeError:
        logger.error("Vision LLM returned invalid JSON: %s", text[:500])
        raise


def call_llm_json(
    system_prompt: str,
    user_prompt: str,
    settings: Settings,
    *,
    max_tokens: int | None = None,
    model: str | None = None,
) -> dict:
    """Send a prompt to Claude and parse the response as JSON.

    The system prompt should instruct the model to return valid JSON only.
    """
    raw = call_llm(
        system_prompt, user_prompt, settings,
        max_tokens=max_tokens, model=model,
    )

    # Extract JSON from response — handle preamble text and markdown fences
    text = raw.strip()

    # Try to find a JSON code fence first
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    else:
        # Fall back: find the first { or [ and match to the last } or ]
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
    except json.JSONDecodeError:
        logger.error("LLM returned invalid JSON: %s", text[:500])
        raise
