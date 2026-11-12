"""Offline checks for bounded provider throttling and wrapped stream errors."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.llm.rate_limit_recovery import GeminiRateLimitRecovery, llm_error_status


def provider_error(status=429, *, headers=None, body=None):
    error = RuntimeError("private provider message")
    error.status_code = status
    error.response = SimpleNamespace(status_code=status, headers=headers or {})
    error.body = body
    return error


def policy(**overrides):
    values = dict(enabled=True, max_retries=2, retry_delay=2)
    return GeminiRateLimitRecovery(**(values | overrides))


@pytest.mark.parametrize("status", [401, 403, 404, 429])
@pytest.mark.parametrize("link", ["__cause__", "__context__", "original_exception"])
def test_provider_status_survives_wrappers_and_cycles(status, link):
    root = provider_error(status)
    wrapper = provider_error(503)
    setattr(wrapper, link, root)
    root.__context__ = wrapper
    assert llm_error_status(wrapper) == status


def test_rate_recovery_obeys_retry_after_and_stops_at_configured_count(monkeypatch):
    sleeps = []
    monkeypatch.setattr("src.llm.rate_limit_recovery.time.sleep", sleeps.append)
    call = Mock(side_effect=[provider_error(headers={"Retry-After": "7"}),
                            provider_error(headers={"retry-after": "9"}), "ok"])
    recovery = policy()
    assert recovery.run(call, logger=Mock(), model="gemini/test") == "ok"
    assert sleeps == [7, 9] and call.call_count == 3
    # The same model's second transport never receives a fresh quota budget.
    call = Mock(side_effect=provider_error())
    with pytest.raises(RuntimeError):
        recovery.run(call, logger=Mock(), model="gemini/test")
    assert call.call_count == 1 and sleeps == [7, 9]


@pytest.mark.parametrize("body", [
    {"error": {"details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}},
    {"error": {"details": [{"quotaValue": "0"}]}},
    {"error": {"code": "insufficient_quota"}},
])
def test_structured_exhausted_or_zero_daily_quota_never_retries(monkeypatch, body):
    sleep = Mock()
    monkeypatch.setattr("src.llm.rate_limit_recovery.time.sleep", sleep)
    call = Mock(side_effect=provider_error(body=body))
    with pytest.raises(RuntimeError):
        policy().run(call, logger=Mock(), model="gemini/test")
    assert call.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("headers", [{"Retry-After": "120"}, {"retry-after": "61"},
                                   {"RETRY-AFTER": "120"}, {"Retry-after": "120"}])
def test_provider_deadline_beyond_budget_skips_retry_instead_of_retrying_early(monkeypatch, headers):
    sleep = Mock()
    monkeypatch.setattr("src.llm.rate_limit_recovery.time.sleep", sleep)
    call = Mock(side_effect=provider_error(headers=headers))
    with pytest.raises(RuntimeError):
        policy().run(call, logger=Mock(), model="gemini/test")
    assert call.call_count == 1
    sleep.assert_not_called()


def test_http_date_and_structured_google_retry_info_are_respected(monkeypatch):
    monkeypatch.setattr("src.llm.rate_limit_recovery.datetime", SimpleNamespace(
        now=lambda tz: datetime(2026, 11, 12, 12, tzinfo=timezone.utc)))
    sleeps = []
    monkeypatch.setattr("src.llm.rate_limit_recovery.time.sleep", sleeps.append)
    error = provider_error(headers={"Retry-After": "Thu, 12 Nov 2026 12:00:20 GMT"}, body={
        "error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "25s"}]}})
    call = Mock(side_effect=[error, "ok"])
    assert policy().run(call, logger=Mock(), model="gemini/test") == "ok"
    assert sleeps == [25]


@pytest.mark.parametrize("invalid", ["bad", "NaN", "inf", "-1"])
def test_invalid_retry_after_uses_bounded_backoff(monkeypatch, invalid):
    sleeps = []
    monkeypatch.setattr("src.llm.rate_limit_recovery.time.sleep", sleeps.append)
    call = Mock(side_effect=provider_error(headers={"Retry-After": invalid}))
    with pytest.raises(RuntimeError):
        policy().run(call, logger=Mock(), model="gemini/test")
    assert sleeps == [2, 4] and call.call_count == 3


@pytest.mark.parametrize("values", [dict(enabled=False), dict(max_retries=0),
                                  dict(retry_delay=0), dict(max_retries=float("inf")),
                                  dict(retry_delay=float("nan"))])
def test_disabled_or_invalid_policy_does_not_retry(monkeypatch, values):
    sleep = Mock()
    monkeypatch.setattr("src.llm.rate_limit_recovery.time.sleep", sleep)
    call = Mock(side_effect=provider_error())
    with pytest.raises(RuntimeError):
        policy(**values).run(call, logger=Mock(), model="gemini/test")
    assert call.call_count == 1
    sleep.assert_not_called()


def test_total_wait_and_untrusted_retry_configuration_are_bounded(monkeypatch):
    sleeps = []
    monkeypatch.setattr("src.llm.rate_limit_recovery.time.sleep", sleeps.append)
    call = Mock(side_effect=provider_error())
    with pytest.raises(RuntimeError):
        policy(max_retries=100000, retry_delay=60).run(call, logger=Mock(), model="gemini/test")
    assert sleeps == [60, 60] and call.call_count == 3
