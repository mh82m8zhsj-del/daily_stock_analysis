"""Bounded recovery for direct Gemini calls, without changing Router retries."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import time
from typing import Any, Callable


def _error_chain(error: BaseException):
    pending, seen = [error], set()
    while pending and len(seen) < 16:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for name in ("original_exception", "__cause__", "__context__"):
            nested = getattr(current, name, None)
            if isinstance(nested, BaseException):
                pending.append(nested)


def llm_error_status(error: BaseException) -> int | None:
    """Find provider status through stream wrappers without inspecting messages."""
    first_status = None
    for current in _error_chain(error):
        response = getattr(current, "response", None)
        for value in (getattr(current, "status_code", None), getattr(response, "status_code", None)):
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                continue
            try:
                status = int(value)
            except ValueError:
                continue
            if status in {401, 403, 404, 429}:
                return status
            if first_status is None and 100 <= status < 600:
                first_status = status
    return first_status


def _structured_details(error: BaseException):
    # Provider bodies can include credentials or prompts. Inspect metadata only;
    # do not stringify them, parse exception messages, or include values in logs.
    for current in _error_chain(error):
        pending = [getattr(current, "body", None), getattr(current, "detail", None)]
        seen = set()
        while pending and len(seen) < 64:
            value = pending.pop()
            if id(value) in seen or not isinstance(value, (Mapping, list, tuple)):
                continue
            seen.add(id(value))
            if isinstance(value, Mapping):
                yield value
                pending.extend(value.values())
            else:
                pending.extend(value)


def _hard_quota(error: BaseException) -> bool:
    for detail in _structured_details(error):
        quota_id = str(detail.get("quotaId", "")).lower()
        metric = str(detail.get("quotaMetric", "")).lower()
        if "perday" in quota_id or "per_day" in metric:
            return True
        value = detail.get("quotaValue")
        if value is not None and not isinstance(value, bool) and value in (0, "0"):
            return True
        if detail.get("code") in ("insufficient_quota", "daily_limit_exceeded"):
            return True
    return False


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _retry_after(error: BaseException) -> float:
    delays = [0.0]
    for current in _error_chain(error):
        response = getattr(current, "response", None)
        for headers in (getattr(current, "headers", None), getattr(response, "headers", None)):
            if not isinstance(headers, Mapping):
                continue
            for key, value in headers.items():
                if not isinstance(key, str) or key.lower() != "retry-after":
                    continue
                delay = _seconds(value)
                if delay is None and isinstance(value, str):
                    try:
                        stamp = parsedate_to_datetime(value)
                        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
                        delay = max(0.0, (stamp - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        pass
                if delay is not None:
                    delays.append(delay)
    for detail in _structured_details(error):
        if str(detail.get("@type", "")).endswith("google.rpc.RetryInfo"):
            value = detail.get("retryDelay")
            delay = _seconds(value[:-1]) if isinstance(value, str) and value.endswith("s") else None
            if delay is not None:
                delays.append(delay)
    return max(delays)


class GeminiRateLimitRecovery:
    """One shared retry budget for a model's stream and non-stream attempts.

    Keep provider waits within 60 seconds each and 120 seconds per model. A
    longer Retry-After skips local recovery; never retry before that deadline.
    The caller enables this only for direct, single-key legacy Gemini routes.
    """

    def __init__(self, *, enabled: bool, max_retries: Any, retry_delay: Any):
        retries = _seconds(max_retries)
        self.max_retries = min(int(retries), 5) if enabled and retries is not None else 0
        self.retry_delay = _seconds(retry_delay) or 0.0
        self.retries = 0
        self.waited = 0.0

    def run(self, call: Callable[[], Any], *, logger: Any, model: str) -> Any:
        while True:
            try:
                return call()
            except Exception as error:
                if (llm_error_status(error) != 429 or self.retries >= self.max_retries
                        or self.retry_delay <= 0 or _hard_quota(error)):
                    raise
                delay = max(min(self.retry_delay * (2 ** self.retries), 60.0), _retry_after(error))
                if delay > 60 or self.waited + delay > 120:
                    raise
                self.retries += 1
                self.waited += delay
                logger.warning("[LiteLLM] %s rate limited; waiting %.1fs before retry %d/%d",
                               model, delay, self.retries, self.max_retries)
                time.sleep(delay)
