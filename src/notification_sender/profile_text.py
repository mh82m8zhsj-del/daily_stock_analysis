"""Scoped text overrides for the Arabic personal report, preserving other callers."""
from contextvars import ContextVar

arabic_profile = ContextVar("aql_arabic_notification", default=False)


def profile_text(original, arabic="عقل الأسهم"):
    return arabic if arabic_profile.get() else original
