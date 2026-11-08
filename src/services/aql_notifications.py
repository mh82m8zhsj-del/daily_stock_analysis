"""Arabic notification preflight and dispatch without exposing operator secrets.

Preflight reuses the project's read-only diagnostics and route/noise parsers.
It never sends or reserves dedup/cooldown state. Sender results, not channel
configuration alone, determine whether a report was delivered.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import json
import logging
import os
from zoneinfo import ZoneInfo

from src.config import Config
from src.notification import NotificationService
from src.notification_noise import (
    NOTIFICATION_SEVERITY_RANK,
    is_time_in_quiet_hours,
    parse_notification_quiet_hours,
)
from src.notification_routing import split_notification_route_channels
from src.notification_sender.profile_text import arabic_profile
from src.services.notification_diagnostics import KEY_SPECS, run_notification_diagnostics
from src.services.aql_analysis import Account, DataUnavailable

_CHANNEL_NAMES = {
    "wechat": "وي تشات", "dingtalk": "دينغ توك", "feishu": "فيشو",
    "telegram": "تيليغرام", "email": "البريد الإلكتروني", "pushover": "بوش أوفر",
    "ntfy": "إن تفاي", "gotify": "غوتيفاي", "pushplus": "بوش بلس",
    "serverchan3": "سيرفر تشان", "custom": "القناة المخصصة", "discord": "ديسكورد",
    "slack": "سلاك", "astrbot": "أستر بوت",
}
# Retain a patchable loader name for isolated tests while avoiding the full
# model/provider configuration singleton during read-only notification checks.
get_config = Config.notification_only_from_env
_KNOWN_KEYS = frozenset(spec.key for spec in KEY_SPECS)
_PRIVATE_PREFLIGHT_LOGS = ContextVar("aql_private_preflight_logs", default=None)


class _PrivateConfigLogFilter(logging.Filter):
    """Redact bootstrap records before handlers receive them in this context."""

    def __init__(self):
        super().__init__()
        self.had_warning = False

    def filter(self, record):
        if _PRIVATE_PREFLIGHT_LOGS.get() is self:
            self.had_warning = self.had_warning or record.levelno >= logging.WARNING
            # Config parsers log malformed values before raising/returning a
            # fallback. Formatting exceptions or args would disclose those values.
            record.msg = "حُجبت تفاصيل تهيئة الإعدادات الخاصة أثناء الفحص الآمن."
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


@contextmanager
def _private_config_logs(*, include_provider_logs=False):
    """Keep other contexts and the process logging configuration unchanged."""
    names = ("src.config",)
    if include_provider_logs:
        # Provider bootstrap loggers can have their own handlers; filtering only
        # the root handler would leave those sinks exposed. This scope ends
        # before actual network delivery; it is not a claim to redact all sender logs.
        names += ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "httpx", "httpcore")
    sources = tuple(logging.getLogger(name) for name in names)
    redactor = _PrivateConfigLogFilter()
    token = _PRIVATE_PREFLIGHT_LOGS.set(redactor)
    for source in sources:
        source.addFilter(redactor)
    try:
        yield redactor
    finally:
        for source in sources:
            source.removeFilter(redactor)
        _PRIVATE_PREFLIGHT_LOGS.reset(token)


_ISSUE_TEXT = {
    "no_channels_configured": "لا توجد قناة إشعار مكتملة الإعدادات",
    "partial_channel_config": "إعداد قناة غير مكتمل؛ يلزم المفتاح المشار إليه",
    "invalid_ntfy_url": "عنوان قناة إن تفاي غير صالح",
    "invalid_gotify_url": "عنوان قناة غوتيفاي غير صالح",
    "advanced_without_minimal": "توجد إعدادات إضافية دون إعدادات القناة الأساسية",
    "invalid_route_channel": "إعداد مسار إشعارات يحتوي قناة غير معروفة",
    "route_channel_not_configured": "مسار إشعارات يشير إلى قناة غير مهيأة",
    "invalid_quiet_hours": "إعداد ساعات الهدوء غير صالح",
    "invalid_notification_timezone": "المنطقة الزمنية للإشعارات غير صالحة",
    "invalid_notification_min_severity": "إعداد الحد الأدنى لأهمية الإشعار غير صالح",
    "reserved_daily_digest": "خيار الملخص اليومي محجوز ولا يرسل ملخصًا تلقائيًا",
}


def _disabled_reason(args):
    if getattr(args, "no_notify", False):
        return "الإرسال معطّل صراحةً بالخيار --no-notify"
    if getattr(args, "dry_run", False):
        return "الإرسال معطّل في التشغيل التجريبي --dry-run"
    return None


def _invalid_noise_duration_keys():
    """Validate raw policy values even when bootstrap warning logs are disabled."""
    invalid = []
    for key in ("NOTIFICATION_DEDUP_TTL_SECONDS", "NOTIFICATION_COOLDOWN_SECONDS"):
        raw = os.getenv(key, "")
        if not raw.strip():
            continue
        try:
            if int(raw.strip()) < 0:
                invalid.append(key)
        except (TypeError, ValueError):
            invalid.append(key)
    return invalid


def _duration_issue_text(keys):
    return "إعداد مدة الإشعارات غير صالح؛ يلزم عدد صحيح غير سالب: " + "، ".join(keys)


def _account_status(now):
    """Describe readiness to calculate entries without returning any account value."""
    raw = os.getenv("AQL_ACCOUNT_JSON", "")
    if not raw.strip():
        return ["لقطة الحساب غير متاحة؛ إشارات الدخول ممنوعة حتى تتوفر بيانات الحساب اللازمة."]
    try:
        account = Account.from_dict(json.loads(raw), now)
    except (DataUnavailable, ValueError, TypeError):
        return ["لقطة الحساب غير صالحة أو غير حديثة؛ إشارات الدخول ممنوعة حتى تصحيحها محليًا."]
    lines = ["لقطة الحساب صالحة وحديثة بحسب البيانات المقدمة؛ لم يتم الاتصال بحساب وسيط للتحقق منها."]
    if account.positions is None:
        lines.append("قائمة المراكز غير متاحة؛ إشارات الدخول ممنوعة حتى تأكيد المراكز أو المحفظة الخالية.")
    else:
        lines.append("قائمة المراكز متاحة؛ يمكن فصل المراكز المملوكة عن الأسهم المرشحة.")
        if any(position.get("stop") is None for position in account.positions.values()):
            lines.append("وقف أحد المراكز المملوكة غير متاح؛ إشارات الدخول ممنوعة حتى اكتمال بيانات المخاطرة.")
    if account.remaining_risk <= 0:
        lines.append("ميزانية المخاطرة اليومية منفدة؛ فتح صفقات جديدة ممنوع.")
    else:
        lines.append("توجد ميزانية مخاطرة يومية متبقية؛ تبقى شروط السوق والبيانات وتأكيد الاختراق إلزامية.")
    return lines


def check_settings(args, now: datetime):
    """Return Arabic preflight text and delivery readiness, without any send.

    Only known configuration KEY names and static channel labels may be shown.
    Diagnostic/exception messages and all configuration values are intentionally
    excluded, including invalid route tokens or timezone names.
    """
    lines = ["# فحص إشعارات عقل الأسهم", "فحص محلي فقط؛ لم تُرسل رسالة اختبار ولم تُعرض قيم الإعدادات.",
             "الفحص يتحقق من وجود الإعدادات وصيغتها فقط؛ لا يثبت صلاحية بيانات الاعتماد أو اتصال الشبكة أو وصول الرسائل."]
    disabled = _disabled_reason(args)
    if disabled:
        lines.append(disabled)
    invalid_durations = _invalid_noise_duration_keys()
    if invalid_durations:
        lines.append(_duration_issue_text(invalid_durations))
    try:
        with _private_config_logs() as bootstrap:
            config = get_config()
            diagnostics = run_notification_diagnostics(config)
        lines.extend(_account_status(now))
        configured = set(diagnostics.configured_channels)
        labels = [_CHANNEL_NAMES[channel] for channel in _CHANNEL_NAMES if channel in configured]
        lines.append("القنوات المكتملة: " + ("، ".join(labels) if labels else "لا توجد"))
        ready = diagnostics.ok and not disabled and not bootstrap.had_warning and not invalid_durations
        if bootstrap.had_warning:
            lines.append("بعض الإعدادات غير صالحة أو استُبدلت بقيم احتياطية؛ الفحص غير جاهز حتى مراجعتها محليًا.")
        for issue in (*diagnostics.errors, *diagnostics.warnings):
            text = _ISSUE_TEXT.get(issue.code, "توجد مشكلة في إعدادات الإشعارات")
            key = f" ({issue.key})" if issue.key in _KNOWN_KEYS else ""
            lines.append(text + key)

        route = getattr(config, "notification_report_channels", []) or []
        valid, invalid = split_notification_route_channels(route)
        targets = configured.intersection(valid) if route else configured
        lines.append("مسار التقارير: " + ("توجد قناة مستهدفة مهيأة" if targets else "لا توجد قناة مستهدفة مهيأة"))
        ready = bool(ready and targets and not invalid)
        if not targets:
            lines.append("راجع NOTIFICATION_REPORT_CHANNELS ومفاتيح القناة؛ لن يصل التقرير إلى مسار فارغ.")

        for attribute, label in (
            ("notification_dedup_ttl_seconds", "سياسة منع التكرار"),
            ("notification_cooldown_seconds", "سياسة مهلة الإرسال"),
        ):
            enabled = bool(getattr(config, attribute, 0))
            lines.append(f"{label}: {'مفعّلة؛ قد تعلق تقريرًا مطابقًا أو متقاربًا' if enabled else 'غير مفعّلة'}")
        quiet_raw = getattr(config, "notification_quiet_hours", "") or ""
        lines.append("سياسة ساعات الهدوء: " + ("مفعّلة" if quiet_raw else "غير مفعّلة"))
        if quiet_raw and diagnostics.ok:
            quiet = parse_notification_quiet_hours(quiet_raw)
            timezone_name = getattr(config, "notification_timezone", "") or ""
            local_now = now.astimezone(ZoneInfo(timezone_name)) if timezone_name else now.astimezone()
            if quiet and is_time_in_quiet_hours(local_now, quiet):
                lines.append("التوقيت الحالي ضمن ساعات الهدوء؛ إرسال التقرير معلّق الآن.")
                ready = False
        severity = str(getattr(config, "notification_min_severity", "") or "").strip().lower()
        lines.append("سياسة الحد الأدنى للأهمية: " + ("مفعّلة" if severity else "غير مفعّلة"))
        if NOTIFICATION_SEVERITY_RANK.get(severity, 0) > NOTIFICATION_SEVERITY_RANK["info"]:
            lines.append("الحد الأدنى للأهمية يمنع تقارير المراقبة العادية؛ راجع NOTIFICATION_MIN_SEVERITY.")
            ready = False
        lines.append("نتيجة الفحص: " + ("الإعدادات تسمح بمحاولة الإرسال؛ لا يثبت ذلك وصول الرسالة." if ready else "الإرسال غير جاهز أو معلّق؛ راجع الأسباب أعلاه."))
        return "\n".join(lines), bool(ready)
    except Exception:
        # Do not expose provider/configuration exception messages; they may carry
        # invalid URLs, recipients or credential-bearing user input.
        lines.append("تعذر فحص إعدادات الإشعارات بأمان؛ راجع صيغة الإعدادات دون مشاركة قيمها الخاصة.")
        return "\n".join(lines), False


def dispatch_report(content: str, args) -> int:
    """Send through existing transports and describe the actual result in Arabic.

    Explicit command-line opt-out is successful local-only operation. Missing
    routes, suppression and failed sends return 1, never silent success.
    """
    disabled = _disabled_reason(args)
    if disabled:
        print(disabled + "؛ حُفظ التقرير محليًا دون إشعار.")
        return 0
    invalid_durations = _invalid_noise_duration_keys()
    if invalid_durations:
        print("لم يُرسل تقرير عقل الأسهم: " + _duration_issue_text(invalid_durations))
        return 1
    token = arabic_profile.set(True)
    try:
        with _private_config_logs(include_provider_logs=True) as bootstrap:
            notifier = NotificationService()
        if bootstrap.had_warning:
            print("لم يُرسل تقرير عقل الأسهم: بعض إعدادات التهيئة غير صالحة أو استُبدلت بقيم احتياطية؛ راجعها محليًا.")
            return 1
        notifier._markdown_to_image_channels = set()
        if not notifier.is_available():
            print("لم يُرسل تقرير عقل الأسهم: لا توجد قناة إشعار مكتملة الإعدادات؛ استخدم --check-notify للفحص الآمن.")
            return 1
        if not notifier.get_channels_for_route("report"):
            print("لم يُرسل تقرير عقل الأسهم: مسار التقارير لا يستهدف قناة مهيأة؛ راجع NOTIFICATION_REPORT_CHANNELS.")
            return 1
        result = notifier.send_with_results(content, route_type="report")
        if result.status == "noise_suppressed":
            print("لم يُرسل تقرير عقل الأسهم: علّقته سياسة منع التكرار أو مهلة الإرسال أو ساعات الهدوء أو الحد الأدنى للأهمية؛ استخدم --check-notify.")
            return 1
        if result.status == "no_channel":
            print("لم يُرسل تقرير عقل الأسهم: لا توجد قناة متاحة لمسار التقارير.")
            return 1
        if result.success:
            if result.status == "partial_failed":
                print("أُرسل تقرير عقل الأسهم إلى قناة واحدة على الأقل، وتعذر إرساله إلى بعض القنوات الأخرى.")
            else:
                print("أُرسل تقرير عقل الأسهم بنجاح عبر قناة مهيأة.")
            return 0
        print("تعذر إرسال تقرير عقل الأسهم عبر القنوات المستهدفة؛ تحقق من إعدادات الاتصال والقناة دون مشاركة الأسرار.")
        return 1
    except Exception:
        print("تعذر تهيئة الإشعارات أو إرسال التقرير بأمان؛ استخدم --check-notify وراجع الإعدادات الخاصة محليًا.")
        return 1
    finally:
        arabic_profile.reset(token)
