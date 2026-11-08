"""Tests for optional Telegram-only Arabic translation."""
from unittest.mock import Mock, patch

from src.notification_sender.arabic_translation import translate_telegram_report


def test_no_endpoint_keeps_original(monkeypatch):
    monkeypatch.delenv("TELEGRAM_TRANSLATE_URL", raising=False)
    assert translate_telegram_report("Buy NVDA at $100") == "Buy NVDA at $100"


def test_translation_success(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TRANSLATE_URL", "https://translate.example.org/translate")
    monkeypatch.setenv("TELEGRAM_TRANSLATE_API_KEY", "test-key")
    response = Mock()
    response.json.return_value = {"translatedText": "شراء NVDA بسعر $100"}
    with patch("src.notification_sender.arabic_translation.requests.post", return_value=response) as post:
        assert translate_telegram_report("Buy NVDA at $100") == "شراء NVDA بسعر $100"
    assert post.call_args.kwargs["json"]["target"] == "ar"


def test_translation_failure_keeps_original(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TRANSLATE_URL", "https://translate.example.org/translate")
    with patch("src.notification_sender.arabic_translation.requests.post", side_effect=__import__("requests").Timeout):
        assert translate_telegram_report("Hold MSFT") == "Hold MSFT"


def test_rejects_insecure_endpoint(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TRANSLATE_URL", "http://translate.example.org/translate")
    with patch("src.notification_sender.arabic_translation.requests.post") as post:
        assert translate_telegram_report("Hold MSFT") == "Hold MSFT"
        post.assert_not_called()


def test_free_translation_without_key(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TRANSLATE_URL", "mymemory")
    response = Mock()
    response.json.return_value = {
        "responseStatus": 200,
        "quotaFinished": False,
        "responseData": {"translatedText": "شراء NVDA"},
    }
    with patch("src.notification_sender.arabic_translation.requests.get", return_value=response) as get:
        assert translate_telegram_report("Buy NVDA") == "شراء NVDA"
    assert get.call_args.kwargs["params"]["langpair"] == "en|ar"


def test_free_translation_quota_failure_preserves_original(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TRANSLATE_URL", "mymemory")
    response = Mock()
    response.json.return_value = {"responseStatus": 429, "quotaFinished": True}
    with patch("src.notification_sender.arabic_translation.requests.get", return_value=response):
        assert translate_telegram_report("Hold MSFT") == "Hold MSFT"


def test_free_translation_chunks_respect_api_limit():
    from src.notification_sender.arabic_translation import _chunks
    parts = list(_chunks("Buy " * 300))
    assert "".join(parts) == "Buy " * 300
    assert all(len(p.encode("utf-8")) <= 450 for p in parts)
