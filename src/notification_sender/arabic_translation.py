# -*- coding: utf-8 -*-
"""Optional Arabic translation of outbound Telegram reports.

The analysis engine and trading decisions are never modified. On translation
failure, preserve the original report rather than blocking delivery.
"""
import logging
import os
import re
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


def translate_telegram_report(text: str) -> str:
    endpoint = os.getenv("TELEGRAM_TRANSLATE_URL", "").strip()
    if not endpoint:
        return text
    if endpoint == "mymemory":
        return _translate_with_mymemory(text)
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
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Invalid translation response")
        translated = data.get("translatedText")
        if isinstance(translated, str) and translated.strip():
            return translated
        logger.warning("Translation service returned no translatedText; delivering original")
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.warning("Telegram translation unavailable (%s); delivering original report", type(exc).__name__)
    return text


def _chunks(text: str, max_bytes: int = 450):
    """Split at whitespace, with a safe fallback for very long tokens."""
    current = ""
    for token in re.findall(r"\S+|\s+", text):
        if len((current + token).encode("utf-8")) <= max_bytes:
            current += token
            continue
        if current:
            yield current
            current = ""
        while len(token.encode("utf-8")) > max_bytes:
            cut = 1
            while cut < len(token) and len(token[:cut + 1].encode("utf-8")) <= max_bytes:
                cut += 1
            yield token[:cut]
            token = token[cut:]
        current = token
    if current:
        yield current


def _translate_with_mymemory(text: str) -> str:
    """Free keyless API: 500 bytes/request and 5000 chars/day anonymously."""
    translated_parts = []
    try:
        for part in _chunks(text):
            if not part.strip():
                translated_parts.append(part)
                continue
            response = requests.get(
                "https://api.mymemory.translated.net/get",
                params={"q": part, "langpair": "en|ar"},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid translation response")
            if data.get("responseStatus") != 200 or data.get("quotaFinished"):
                raise ValueError("Translation API limit or error")
            response_data = data.get("responseData")
            if not isinstance(response_data, dict):
                raise ValueError("Invalid translation response data")
            translated = response_data.get("translatedText")
            if not isinstance(translated, str) or not translated.strip():
                raise ValueError("Empty translation")
            translated_parts.append(translated)
        translated_text = "".join(translated_parts)
        return translated_text if translated_text.strip() else text
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.warning("Free Arabic translation unavailable (%s); sending English original", type(exc).__name__)
        return text
