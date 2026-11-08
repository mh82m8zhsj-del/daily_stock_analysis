# -*- coding: utf-8 -*-
"""Optional Arabic translation of outbound Telegram reports.

The analysis engine and trading decisions are never modified. On translation
failure, preserve the original report rather than blocking delivery.
"""
import logging
import os
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


def translate_telegram_report(text: str) -> str:
    endpoint = os.getenv("TELEGRAM_TRANSLATE_URL", "").strip()
    if not endpoint:
        return text
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        logger.warning("Invalid TELEGRAM_TRANSLATE_URL; delivering original report")
        return text

    # LibreTranslate-compatible /translate endpoint. API keys are never logged.
    payload = {"q": text, "source": "auto", "target": "ar", "format": "text"}
    api_key = os.getenv("TELEGRAM_TRANSLATE_API_KEY", "").strip()
    if api_key:
        payload["api_key"] = api_key
    try:
        response = requests.post(endpoint, json=payload, timeout=25)
        response.raise_for_status()
        translated = response.json().get("translatedText")
        if isinstance(translated, str) and translated.strip():
            return translated
        logger.warning("Translation service returned no translatedText; delivering original")
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.warning("Telegram translation unavailable (%s); delivering original report", type(exc).__name__)
    return text
