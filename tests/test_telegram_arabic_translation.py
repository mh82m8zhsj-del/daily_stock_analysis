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


def test_chunks_preserve_unicode_whitespace_and_long_tokens():
    from src.notification_sender.arabic_translation import _chunks
    for original in ("سهم NVDA 🚀\n\t" * 120, "س" * 1000, "x" * 1000, "\n \t"):
        parts = list(_chunks(original))
        assert parts
        assert "".join(parts) == original
        assert all(0 < len(part.encode("utf-8")) <= 450 for part in parts)


def test_no_translation_parts_never_discards_report(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TRANSLATE_URL", "mymemory")
    monkeypatch.setattr("src.notification_sender.arabic_translation._chunks", lambda text: iter(()))
    assert translate_telegram_report("Hold MSFT") == "Hold MSFT"


def test_free_translation_reaches_telegram_sender(monkeypatch):
    from types import SimpleNamespace
    from src.notification_sender.telegram_sender import TelegramSender
    monkeypatch.setenv("TELEGRAM_TRANSLATE_URL", "mymemory")
    response = Mock()
    response.json.return_value = {"responseStatus": 200, "responseData": {"translatedText": "متابعة MSFT"}}
    sender = TelegramSender(SimpleNamespace(telegram_bot_token="test-token", telegram_chat_id="123"))
    with patch("src.notification_sender.arabic_translation.requests.get", return_value=response), \
            patch.object(sender, "_send_telegram_message", return_value=True) as send:
        assert sender.send_to_telegram("Hold MSFT")
    assert send.call_args.args[2] == "متابعة MSFT"


def test_empty_translation_still_delivers_original_report():
    from types import SimpleNamespace
    from src.notification_sender.telegram_sender import TelegramSender
    sender = TelegramSender(SimpleNamespace(telegram_bot_token="test-token", telegram_chat_id="123"))
    for translated in ("", " \n", None):
        with patch("src.notification_sender.telegram_sender.translate_telegram_report", return_value=translated), \
                patch.object(sender, "_send_telegram_message", return_value=True) as send:
            assert sender.send_to_telegram("Hold MSFT")
        assert send.call_args.args[2] == "Hold MSFT"


def test_malformed_translation_responses_keep_original(monkeypatch):
    for endpoint in ("mymemory", "https://translate.example.org/translate"):
        monkeypatch.setenv("TELEGRAM_TRANSLATE_URL", endpoint)
        for payload in (None, [], "invalid", {}, {"responseStatus": 200, "responseData": None},
                        {"responseStatus": 200, "responseData": []}):
            response = Mock()
            response.json.return_value = payload
            method = "get" if endpoint == "mymemory" else "post"
            with patch(f"src.notification_sender.arabic_translation.requests.{method}", return_value=response):
                assert translate_telegram_report("Hold MSFT") == "Hold MSFT"


def test_malformed_service_response_reaches_sender_as_original(monkeypatch):
    from types import SimpleNamespace
    from src.notification_sender.telegram_sender import TelegramSender
    monkeypatch.setenv("TELEGRAM_TRANSLATE_URL", "mymemory")
    response = Mock()
    response.json.return_value = {"responseStatus": 200, "responseData": None}
    sender = TelegramSender(SimpleNamespace(telegram_bot_token="test-token", telegram_chat_id="123"))
    with patch("src.notification_sender.arabic_translation.requests.get", return_value=response), \
            patch.object(sender, "_send_telegram_message", return_value=True) as send:
        assert sender.send_to_telegram("Hold MSFT")
    assert send.call_args.args[2] == "Hold MSFT"


def test_free_translation_quota_tries_google_arabic_fallback(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TRANSLATE_URL", "mymemory")
    quota = Mock()
    quota.json.return_value = {"responseStatus": 429, "quotaFinished": True}
    google = Mock()
    google.json.return_value = [[["شراء ", "Buy "], ["NVDA", "NVDA"]]]

    def fake_get(url, **kwargs):
        if "mymemory.translated.net" in url:
            return quota
        assert url == "https://translate.googleapis.com/translate_a/single"
        assert kwargs["params"]["tl"] == "ar"
        return google

    with patch("src.notification_sender.arabic_translation.requests.get", side_effect=fake_get):
        assert translate_telegram_report("Buy NVDA") == "شراء NVDA"


def test_google_fallback_unavailable_does_not_drop_alert(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TRANSLATE_URL", "mymemory")
    quota = Mock()
    quota.json.return_value = {"responseStatus": 429, "quotaFinished": True}

    def fake_get(url, **kwargs):
        if "mymemory.translated.net" in url:
            return quota
        raise __import__("requests").Timeout()

    with patch("src.notification_sender.arabic_translation.requests.get", side_effect=fake_get):
        assert translate_telegram_report("Hold MSFT") == "Hold MSFT"
