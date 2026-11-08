"""Arabic personal notification preflight and safe dispatch contracts."""
import argparse
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.config import Config
from src.notification import NotificationChannel, NotificationDispatchResult
from src.notification_sender.profile_text import arabic_profile
from src.services import aql_notifications

NOW = datetime(2026, 11, 6, 16, tzinfo=timezone.utc)
PRIVATE = "https://private.example/token-secret?recipient=123456"


def config(monkeypatch, **values):
    settings = Config(stock_list=[], **values)
    monkeypatch.setattr(aql_notifications, "get_config", lambda: settings)
    return settings


def sender(monkeypatch, result):
    mocked = Mock()
    mocked.is_available.return_value = True
    mocked.get_channels_for_route.return_value = [NotificationChannel.TELEGRAM]
    mocked.send_with_results.return_value = result
    monkeypatch.setattr(aql_notifications, "NotificationService", lambda: mocked)
    return mocked


def test_preflight_empty_config_is_not_silent_success(monkeypatch):
    config(monkeypatch)
    text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
    assert not ready
    assert "لا توجد قناة إشعار مكتملة" in text
    assert "لم تُرسل رسالة اختبار" in text


def test_preflight_uses_existing_diagnostics_without_secret_values(monkeypatch):
    config(monkeypatch, telegram_bot_token=PRIVATE)
    text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
    assert not ready
    assert "TELEGRAM_CHAT_ID" in text
    assert PRIVATE not in text
    assert "123456" not in text


def test_preflight_filters_report_route_and_never_constructs_sender(monkeypatch):
    config(monkeypatch, telegram_bot_token=PRIVATE, telegram_chat_id="123456", notification_report_channels=["email"])
    monkeypatch.setattr(aql_notifications, "NotificationService", Mock(side_effect=AssertionError("must not initialize sender")))
    # Existing read-only detector lives in notification_diagnostics and remains
    # usable even when the dispatch constructor is forbidden.
    text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
    assert not ready
    assert "لا توجد قناة مستهدفة مهيأة" in text
    assert PRIVATE not in text and "123456" not in text


def test_preflight_valid_route_has_no_delivery_claim(monkeypatch):
    config(monkeypatch, telegram_bot_token=PRIVATE, telegram_chat_id="123456")
    text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
    assert ready
    assert "تسمح بمحاولة الإرسال" in text
    assert "لا يثبت ذلك وصول الرسالة" in text
    assert PRIVATE not in text and "123456" not in text


@pytest.mark.parametrize("options", [dict(notification_quiet_hours="00:00-23:59", notification_timezone="UTC"), dict(notification_min_severity="warning")])
def test_preflight_reports_active_suppression_without_reserving_noise_state(monkeypatch, options):
    config(monkeypatch, telegram_bot_token=PRIVATE, telegram_chat_id="123456", **options)
    monkeypatch.setattr("src.notification.evaluate_notification_noise", Mock(side_effect=AssertionError("must not reserve state")))
    text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
    assert not ready
    assert "معلّق" in text or "يمنع" in text


def test_preflight_lists_enabled_dedup_and_cooldown_without_details(monkeypatch):
    config(monkeypatch, telegram_bot_token=PRIVATE, telegram_chat_id="123456", notification_dedup_ttl_seconds=127, notification_cooldown_seconds=891)
    text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
    assert ready
    assert "سياسة منع التكرار: مفعّلة" in text
    assert "سياسة مهلة الإرسال: مفعّلة" in text
    assert "127" not in text and "891" not in text


@pytest.mark.parametrize("options", [dict(notification_timezone=PRIVATE), dict(notification_report_channels=[PRIVATE])])
def test_invalid_settings_never_echo_invalid_values(monkeypatch, options):
    config(monkeypatch, telegram_bot_token=PRIVATE, telegram_chat_id="123456", **options)
    text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
    assert not ready
    assert PRIVATE not in text and "123456" not in text


@pytest.mark.parametrize("flag", ["no_notify", "dry_run"])
def test_explicit_opt_out_skips_service_entirely(monkeypatch, capsys, flag):
    factory = Mock(side_effect=AssertionError("must not construct sender"))
    monkeypatch.setattr(aql_notifications, "NotificationService", factory)
    assert aql_notifications.dispatch_report("report", argparse.Namespace(**{flag: True})) == 0
    factory.assert_not_called()
    assert "معطّل" in capsys.readouterr().out


def test_no_channels_is_error_with_actionable_arabic_reason(monkeypatch, capsys):
    notifier = sender(monkeypatch, None)
    notifier.is_available.return_value = False
    assert aql_notifications.dispatch_report("report", argparse.Namespace()) == 1
    notifier.send_with_results.assert_not_called()
    assert "لا توجد قناة" in capsys.readouterr().out


def test_no_report_route_is_error(monkeypatch, capsys):
    notifier = sender(monkeypatch, None)
    notifier.get_channels_for_route.return_value = []
    assert aql_notifications.dispatch_report("report", argparse.Namespace()) == 1
    notifier.send_with_results.assert_not_called()
    assert "NOTIFICATION_REPORT_CHANNELS" in capsys.readouterr().out


@pytest.mark.parametrize("status,success,expected", [("sent", True, 0), ("partial_failed", True, 0), ("all_failed", False, 1), ("noise_suppressed", False, 1), ("no_channel", False, 1)])
def test_actual_dispatch_outcome_controls_exit_status(monkeypatch, capsys, status, success, expected):
    notifier = sender(monkeypatch, NotificationDispatchResult(True, success, status, message=PRIVATE))
    original = arabic_profile.get()

    def send(content, route_type):
        assert arabic_profile.get()
        assert content == "# عقل الأسهم" and route_type == "report"
        return NotificationDispatchResult(True, success, status, message=PRIVATE)

    notifier.send_with_results.side_effect = send
    assert aql_notifications.dispatch_report("# عقل الأسهم", argparse.Namespace()) == expected
    assert arabic_profile.get() == original
    assert notifier._markdown_to_image_channels == set()
    text = capsys.readouterr().out
    assert PRIVATE not in text
    if status == "noise_suppressed":
        assert "لم يُرسل" in text and "سياسة" in text
        assert "بنجاح" not in text


@pytest.mark.parametrize("failure_at", ["configuration", "constructor", "send"])
def test_configuration_and_sender_exceptions_are_secret_safe(monkeypatch, capsys, failure_at):
    if failure_at == "configuration":
        monkeypatch.setattr(aql_notifications, "get_config", Mock(side_effect=ValueError(PRIVATE)))
        text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
        assert not ready and PRIVATE not in text
    else:
        notifier = sender(monkeypatch, None)
        if failure_at == "constructor":
            monkeypatch.setattr(aql_notifications, "NotificationService", Mock(side_effect=ValueError(PRIVATE)))
        else:
            notifier.send_with_results.side_effect = ValueError(PRIVATE)
        assert aql_notifications.dispatch_report("report", argparse.Namespace()) == 1
        assert PRIVATE not in capsys.readouterr().out
        assert not arabic_profile.get()


def test_diagnostic_message_and_unknown_keys_are_never_exposed(monkeypatch):
    config(monkeypatch)
    bad_issue = SimpleNamespace(code="unknown", key=PRIVATE, message=PRIVATE)
    monkeypatch.setattr(aql_notifications, "run_notification_diagnostics", lambda _: SimpleNamespace(ok=False, configured_channels=(), errors=(bad_issue,), warnings=()))
    text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
    assert not ready and PRIVATE not in text


def _account_payload(**overrides):
    import json
    payload = dict(equity=10000, start_equity=10000, cash=10000, open_risk=0,
                   daily_loss=0, as_of=NOW.isoformat(), positions={})
    payload.update(overrides)
    return json.dumps(payload)


def test_preflight_account_missing_does_not_block_monitoring_report(monkeypatch):
    config(monkeypatch, telegram_bot_token=PRIVATE, telegram_chat_id="123456")
    monkeypatch.delenv("AQL_ACCOUNT_JSON", raising=False)
    text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
    assert ready
    assert "لقطة الحساب غير متاحة" in text and "إشارات الدخول ممنوعة" in text
    assert "لا يثبت صلاحية بيانات الاعتماد أو اتصال الشبكة" in text


@pytest.mark.parametrize("raw", [PRIVATE, _account_payload(as_of="2026-11-05T16:00:00+00:00"), _account_payload(as_of=PRIVATE)])
def test_preflight_invalid_or_old_account_is_private_and_only_blocks_entries(monkeypatch, raw):
    config(monkeypatch, telegram_bot_token=PRIVATE, telegram_chat_id="123456")
    monkeypatch.setenv("AQL_ACCOUNT_JSON", raw)
    text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
    assert ready
    assert "لقطة الحساب غير صالحة أو غير حديثة" in text
    assert "إشارات الدخول ممنوعة" in text
    assert PRIVATE not in text and "10000" not in text and "123456" not in text


def test_preflight_unknown_positions_block_entries_without_changing_account(monkeypatch):
    config(monkeypatch, telegram_bot_token=PRIVATE, telegram_chat_id="123456")
    raw = _account_payload(positions=None)
    monkeypatch.setenv("AQL_ACCOUNT_JSON", raw)
    text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
    assert ready and "لقطة الحساب صالحة وحديثة" in text
    assert "قائمة المراكز غير متاحة" in text and "إشارات الدخول ممنوعة" in text
    import os
    assert os.environ["AQL_ACCOUNT_JSON"] == raw
    assert "10000" not in text


def test_preflight_account_daily_stop_never_disables_monitoring_report(monkeypatch):
    config(monkeypatch, telegram_bot_token=PRIVATE, telegram_chat_id="123456")
    monkeypatch.setenv("AQL_ACCOUNT_JSON", _account_payload(daily_loss=200))
    text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
    assert ready
    assert "قائمة المراكز متاحة" in text
    assert "ميزانية المخاطرة اليومية منفدة" in text and "فتح صفقات جديدة ممنوع" in text
    assert "200" not in text and "10000" not in text


def test_preflight_valid_flat_account_shows_entry_prerequisites_without_balances(monkeypatch):
    config(monkeypatch, telegram_bot_token=PRIVATE, telegram_chat_id="123456")
    monkeypatch.setenv("AQL_ACCOUNT_JSON", _account_payload())
    text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
    assert ready
    assert "قائمة المراكز متاحة" in text and "ميزانية مخاطرة يومية متبقية" in text
    assert "10000" not in text


def test_preflight_owned_position_without_stop_blocks_entries_only(monkeypatch):
    config(monkeypatch, telegram_bot_token=PRIVATE, telegram_chat_id="123456")
    monkeypatch.setenv("AQL_ACCOUNT_JSON", _account_payload(positions={
        "NVDA": {"shares": 5, "average_cost": 80},
    }))
    text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
    assert ready
    assert "وقف أحد المراكز المملوكة غير متاح" in text
    assert "إشارات الدخول ممنوعة" in text
    assert "NVDA" not in text and "80" not in text


def test_real_config_bootstrap_logs_never_disclose_invalid_private_values(monkeypatch, tmp_path, caplog, capsys):
    import logging
    from src.config import Config

    env_file = tmp_path / "private.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("NOTIFICATION_COOLDOWN_SECONDS", PRIVATE)
    monkeypatch.setenv("AGENT_ARCH", PRIVATE)
    monkeypatch.setenv("LITELLM_CONFIG", PRIVATE)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", PRIVATE)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    monkeypatch.setattr(aql_notifications, "get_config", Config.notification_only_from_env)
    source = logging.getLogger("src.config")
    original_filters = list(source.filters)
    original_disabled = source.disabled
    original_disable_threshold = logging.root.manager.disable
    original_factory = logging.getLogRecordFactory()
    Config.reset_instance()
    try:
        with caplog.at_level(logging.WARNING, logger="src.config"):
            text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
        assert not ready
        assert "استُبدلت بقيم احتياطية" in text
        output = capsys.readouterr()
        assert PRIVATE not in text + output.out + output.err + caplog.text
        assert "123456" not in text + output.out + output.err + caplog.text
        # Prove the actual parser warning happened and was redacted, rather than
        # avoiding the parser with a mocked Config object or disabling logging.
        records = [record for record in caplog.records if record.name == "src.config"]
        assert records
        assert all(record.args == () and record.exc_info is None for record in records)
        assert any("حُجبت تفاصيل" in record.getMessage() for record in records)
        assert source.filters == original_filters and source.disabled == original_disabled
        assert Config._instance is None
        assert logging.root.manager.disable == original_disable_threshold
        assert logging.getLogRecordFactory() is original_factory
        with caplog.at_level(logging.WARNING, logger="src.config"):
            source.warning("ordinary warning remains visible")
        assert "ordinary warning remains visible" in caplog.text
    finally:
        Config.reset_instance()


def test_preflight_log_filter_is_context_local_and_removed_on_error(caplog):
    from contextvars import Context
    import logging

    source = logging.getLogger("src.config")
    original_filters = list(source.filters)
    with caplog.at_level(logging.WARNING, logger="src.config"):
        with pytest.raises(RuntimeError):
            with aql_notifications._private_config_logs():
                source.warning("value=%s", PRIVATE)
                Context().run(source.warning, "other context remains visible")
                raise RuntimeError("stop")
        source.warning("after failure remains visible")
    assert PRIVATE not in caplog.text
    assert "other context remains visible" in caplog.text
    assert "after failure remains visible" in caplog.text
    assert source.filters == original_filters


def test_dispatch_bootstrap_logs_are_private_and_fallback_prevents_send(monkeypatch, caplog, capsys):
    import logging

    notifier = Mock()

    def factory():
        logging.getLogger("src.config").warning("invalid configuration=%s", PRIVATE)
        return notifier

    monkeypatch.setattr(aql_notifications, "NotificationService", factory)
    source = logging.getLogger("src.config")
    original_filters = list(source.filters)
    with caplog.at_level(logging.WARNING, logger="src.config"):
        assert aql_notifications.dispatch_report("report", argparse.Namespace()) == 1
    notifier.send_with_results.assert_not_called()
    output = capsys.readouterr()
    assert PRIVATE not in caplog.text + output.out + output.err
    assert "استُبدلت بقيم احتياطية" in output.out
    assert source.filters == original_filters


def test_other_context_warning_does_not_mark_preflight_as_fallback(caplog):
    from contextvars import Context
    import logging

    source = logging.getLogger("src.config")
    with caplog.at_level(logging.WARNING, logger="src.config"):
        with aql_notifications._private_config_logs() as bootstrap:
            Context().run(source.warning, "other context remains visible")
            assert not bootstrap.had_warning
    assert "other context remains visible" in caplog.text


def test_preflight_real_projection_never_loads_llm_singleton_or_network(monkeypatch, caplog, capsys):
    import builtins
    import httpx
    import requests

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", PRIVATE)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    monkeypatch.setenv("LITELLM_CONFIG", PRIVATE)
    monkeypatch.setenv("OPENAI_BASE_URL", PRIVATE)
    monkeypatch.setenv("LLM_PRIMARY_BASE_URL", PRIVATE)
    monkeypatch.setenv("LLM_PRIMARY_MODELS", PRIVATE)
    monkeypatch.setenv("AGENT_ARCH", PRIVATE)
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)
    providers = Mock(side_effect=AssertionError("provider metadata must not load"))
    singleton = Mock(side_effect=AssertionError("global configuration must not load"))
    httpx_request = Mock(side_effect=AssertionError("HTTP must not run"))
    requests_request = Mock(side_effect=AssertionError("HTTP must not run"))
    monkeypatch.setattr("src.config.get_litellm_model_providers", providers)
    monkeypatch.setattr(Config, "_load_from_env", singleton)
    monkeypatch.setattr(httpx.Client, "request", httpx_request)
    monkeypatch.setattr(requests.Session, "request", requests_request)
    monkeypatch.setattr(aql_notifications, "get_config", Config.notification_only_from_env)
    original_import = builtins.__import__
    blocked_imports = []

    def guarded_import(name, *args, **kwargs):
        if name == "litellm" or name.startswith("litellm."):
            blocked_imports.append(name)
            raise AssertionError("LiteLLM must not import")
        return original_import(name, *args, **kwargs)

    Config.reset_instance()
    sentinel = Config(stock_list=[])
    Config._instance = sentinel
    try:
        monkeypatch.setattr(builtins, "__import__", guarded_import)
        text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
        assert ready
        assert Config._instance is sentinel
        assert not blocked_imports
        providers.assert_not_called()
        singleton.assert_not_called()
        httpx_request.assert_not_called()
        requests_request.assert_not_called()
        output = capsys.readouterr()
        assert PRIVATE not in text + output.out + output.err + caplog.text
        assert "123456" not in text + output.out + output.err + caplog.text
    finally:
        Config.reset_instance()


@pytest.mark.parametrize("logger_name", ["LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "httpx", "httpcore"])
def test_dispatch_bootstrap_filters_provider_owned_handlers(monkeypatch, capsys, logger_name):
    import io
    import logging

    source = logging.getLogger(logger_name)
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    source.addHandler(handler)
    original_filters = list(source.filters)
    notifier = Mock()

    def factory():
        source.warning("provider URL: %s", PRIVATE)
        return notifier

    monkeypatch.setattr(aql_notifications, "NotificationService", factory)
    try:
        assert aql_notifications.dispatch_report("report", argparse.Namespace()) == 1
        notifier.send_with_results.assert_not_called()
        assert PRIVATE not in sink.getvalue() + capsys.readouterr().out
        assert "حُجبت تفاصيل" in sink.getvalue()
        assert source.filters == original_filters
    finally:
        source.removeHandler(handler)


@pytest.mark.parametrize("key", ["NOTIFICATION_DEDUP_TTL_SECONDS", "NOTIFICATION_COOLDOWN_SECONDS"])
@pytest.mark.parametrize("raw", [PRIVATE, "-1"])
@pytest.mark.parametrize("log_mode", ["error_level", "disabled_logger", "global_disabled"])
def test_invalid_raw_duration_blocks_check_and_dispatch_when_warning_logs_are_disabled(
    monkeypatch, caplog, capsys, key, raw, log_mode,
):
    import logging

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", PRIVATE)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    monkeypatch.setenv(key, raw)
    monkeypatch.setattr(aql_notifications, "get_config", Config.notification_only_from_env)
    factory = Mock(side_effect=AssertionError("invalid durations must not construct sender"))
    monkeypatch.setattr(aql_notifications, "NotificationService", factory)
    source = logging.getLogger("src.config")
    original_level, original_disabled = source.level, source.disabled
    original_global_disable = logging.root.manager.disable
    try:
        if log_mode == "error_level":
            source.setLevel(logging.ERROR)
        elif log_mode == "disabled_logger":
            source.disabled = True
        else:
            logging.disable(logging.WARNING)
        expected_logging = (source.level, source.disabled, logging.root.manager.disable)
        text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
        assert not ready
        assert key in text and "يلزم عدد صحيح غير سالب" in text
        assert aql_notifications.dispatch_report("report", argparse.Namespace()) == 1
        factory.assert_not_called()
        output = capsys.readouterr()
        assert raw not in text + output.out + output.err + caplog.text
        assert "123456" not in text + output.out + output.err + caplog.text
        assert (source.level, source.disabled, logging.root.manager.disable) == expected_logging
    finally:
        source.setLevel(original_level)
        source.disabled = original_disabled
        logging.disable(original_global_disable)


@pytest.mark.parametrize("raw", ["", "  ", "0", "12", " +5 "])
def test_raw_duration_guard_accepts_blank_or_nonnegative_integer(monkeypatch, raw):
    config(monkeypatch, telegram_bot_token=PRIVATE, telegram_chat_id="123456")
    monkeypatch.setenv("NOTIFICATION_DEDUP_TTL_SECONDS", raw)
    monkeypatch.setenv("NOTIFICATION_COOLDOWN_SECONDS", raw)
    text, ready = aql_notifications.check_settings(argparse.Namespace(), NOW)
    assert ready
    assert "إعداد مدة الإشعارات غير صالح" not in text
