"""Offline end-to-end contracts for the Arabic personal analysis profile."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import exchange_calendars as xcals
import pandas as pd
import pytest

from src.services.aql_analysis import Account, DataUnavailable, assess, render_report
from src.services import aql_runner

NOW = datetime(2026, 11, 6, 16, 0, tzinfo=timezone.utc)  # Friday 11:00 New York


@pytest.fixture
def bars():
    calendar = xcals.get_calendar("XNYS")
    days = calendar.sessions_in_range("2026-10-29", "2026-11-06")
    index = pd.DatetimeIndex([], tz="UTC")
    for day in days:
        index = index.append(pd.date_range(calendar.session_open(day), calendar.session_close(day), freq="15min", inclusive="left"))
    index = index[index < pd.Timestamp(NOW)]
    frame = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0}, index=index)
    frame.loc[index[-1], ["open", "high", "low", "close", "volume"]] = [100, 103, 99, 102, 3000]
    return frame


@pytest.fixture
def account():
    return Account(10000, 10000, 10000, 0, NOW, positions={})


def test_confirmed_breakout_and_exact_sizing(bars, account):
    result = assess("NVDA", bars, account, NOW)
    assert result.plan == {"entry": 102.0, "stop": 99.0, "target_1": 108.0, "target_2": 111.0,
                           "shares": 66, "risk": 198.0, "cost": 6732.0, "rr": 2.0}
    assert result.metrics["rvol"] == 3
    assert result.metrics["resistance"] == 101
    assert result.metrics["vwap"] == pytest.approx((5 * 100 * 1000 + (304 / 3) * 3000) / 8000)


def test_forming_candle_never_confirms(bars, account):
    bars.loc[bars.index[-1], ["high", "close"]] = [101, 100]
    bars.loc[pd.Timestamp(NOW)] = [100, 200, 100, 200, 100000]
    assert assess("NVDA", bars, account, NOW + timedelta(minutes=14)).plan is None


def test_equality_is_not_breakout(bars, account):
    bars.loc[bars.index[-1], "close"] = 101
    assert assess("NVDA", bars, account, NOW).plan is None


@pytest.mark.parametrize("mutation", ["nan", "infinite", "zero_volume", "negative", "ohlc", "duplicate", "naive", "gap", "old", "future", "rvol_history", "daily"])
def test_missing_or_invalid_data_blocks_entry(bars, account, mutation):
    if mutation in {"nan", "infinite", "negative"}:
        bars.iloc[-1, 3] = {"nan": float("nan"), "infinite": float("inf"), "negative": -1}[mutation]
    elif mutation == "zero_volume":
        bars.iloc[-1, 4] = 0
    elif mutation == "ohlc":
        bars.iloc[-1, 1] = 100
    elif mutation == "duplicate":
        bars = pd.concat([bars, bars.tail(1)])
    elif mutation == "naive":
        bars.index = bars.index.tz_localize(None)
    elif mutation == "gap":
        bars = bars.drop(bars.index[-3])
    elif mutation == "old":
        bars = bars.iloc[:-1]
    elif mutation == "future":
        bars.loc[pd.Timestamp(NOW) + pd.Timedelta(minutes=15)] = bars.iloc[-1]
    elif mutation == "rvol_history":
        bars = bars.iloc[-21:]
    elif mutation == "daily":
        bars = bars.resample("1D").last().dropna()
    assert assess("NVDA", bars, account, NOW).plan is None


@pytest.mark.parametrize("payload", [None, {}, {"equity": -1}, {"equity": float("nan")}, []])
def test_bad_account_payload(payload):
    with pytest.raises(DataUnavailable):
        Account.from_dict(payload, NOW)


@pytest.mark.parametrize("account_change", ["missing", "stale", "future", "yesterday", "daily_limit", "open_risk", "cash"])
def test_account_risk_guards(bars, account_change):
    values = {"equity": 10000, "start_equity": 10000, "cash": 10000, "open_risk": 0, "as_of": NOW, "positions": {}}
    if account_change == "missing":
        account = None
    else:
        if account_change == "stale":
            values["as_of"] -= timedelta(minutes=6)
        elif account_change == "future":
            values["as_of"] += timedelta(seconds=1)
        elif account_change == "yesterday":
            values["as_of"] -= timedelta(days=1)
        elif account_change == "daily_limit":
            values.update(equity=9800, cash=9800)
        elif account_change == "open_risk":
            values["open_risk"] = 200
        elif account_change == "cash":
            values["cash"] = 50
        account = Account(**values)
    assert assess("NVDA", bars, account, NOW).plan is None


def test_remaining_daily_budget_and_cash_are_caps(bars):
    result = assess("NVDA", bars, Account(9900, 10000, 9900, 50, NOW, positions={}), NOW)
    assert result.plan["risk"] <= 50
    result = assess("NVDA", bars, Account(10000, 10000, 205, 0, NOW, positions={}), NOW)
    assert result.plan["shares"] == 2
    assert result.plan["cost"] <= 205


def test_modes_do_not_relax_safety(bars, account):
    bars.iloc[-1, 4] = 1700
    assert assess("NVDA", bars, account, NOW, "conservative").plan is None
    assert assess("NVDA", bars, account, NOW, "adventurous").plan
    assert assess("NVDA", bars, None, NOW, "adventurous").plan is None
    with pytest.raises(ValueError):
        assess("NVDA", bars, account, NOW, "typo")


@pytest.mark.parametrize("moment", [datetime(2026, 11, 7, 16, tzinfo=timezone.utc), datetime(2026, 12, 25, 16, tzinfo=timezone.utc), datetime(2026, 11, 27, 19, tzinfo=timezone.utc), NOW.replace(hour=13)])
def test_closed_sessions_fail_closed(bars, account, moment):
    assert assess("NVDA", bars, account, moment).plan is None


def test_scope_and_arabic_report(bars, account):
    result = assess("600519", bars, account, NOW)
    assert result.plan is None
    report = render_report([result, assess("NVDA", bars, account, NOW)], NOW)
    assert "عقل الأسهم" in report and "66 سهم" in report
    assert not any('\u4e00' <= c <= '\u9fff' for c in report)


def test_runner_reserves_basket_budget_and_uses_same_arabic_alert(bars, monkeypatch, tmp_path):
    class Clock:
        @staticmethod
        def now(tz):
            return NOW
    monkeypatch.setattr(aql_runner, "datetime", Clock)
    monkeypatch.setattr(aql_runner, "fetch_intraday", lambda symbol: bars)
    monkeypatch.setenv("AQL_ACCOUNT_JSON", json.dumps({"equity": 10000, "start_equity": 10000, "cash": 10000, "open_risk": 0, "daily_loss": 0, "positions": {}, "as_of": NOW.isoformat()}))
    monkeypatch.chdir(tmp_path)
    notifier = Mock()
    notifier.send_with_results.return_value = Mock(success=True, status="sent")
    monkeypatch.setattr("src.services.aql_notifications.NotificationService", lambda: notifier)
    assert aql_runner.run(argparse.Namespace(stocks="NVDA,AMD", no_notify=False)) == 0
    report = next((tmp_path / "reports").glob("*.md")).read_text()
    assert "66 سهم" in report and "قرار الدخول: ممنوع" in report
    assert notifier.send_with_results.call_args.args[0] == report
    assert notifier._markdown_to_image_channels == set()


def test_notification_localization_is_scoped():
    from src.notification_sender.profile_text import arabic_profile, profile_text
    from src.formatters import markdown_to_html_document
    assert profile_text("original") == "original"
    token = arabic_profile.set(True)
    try:
        assert profile_text("original") == "عقل الأسهم"
        assert 'lang="ar" dir="rtl"' in markdown_to_html_document("# عقل الأسهم")
    finally:
        arabic_profile.reset(token)
    assert profile_text("original") == "original"


def test_provider_failure_and_dry_run_do_not_send(monkeypatch, tmp_path):
    class Clock:
        @staticmethod
        def now(tz):
            return NOW
    monkeypatch.setattr(aql_runner, "datetime", Clock)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AQL_ACCOUNT_JSON", raising=False)

    def unavailable(symbol):
        raise DataUnavailable("المزود غير متاح")
    monkeypatch.setattr(aql_runner, "fetch_intraday", unavailable)
    assert aql_runner.run(argparse.Namespace(stocks="NVDA", dry_run=True)) == 1
    assert "المزود غير متاح" in next((tmp_path / "reports").glob("*.md")).read_text()


def test_main_routes_profile_before_legacy_generation(monkeypatch):
    import main
    monkeypatch.delenv("AQL_ENABLED", raising=False)
    monkeypatch.setattr(main, "parse_arguments", lambda: argparse.Namespace(aql=True))
    legacy = Mock(side_effect=AssertionError("legacy generation must not run"))
    monkeypatch.setattr(main, "get_config", legacy)
    run = Mock(return_value=0)
    monkeypatch.setattr(aql_runner, "run", run)
    assert main.main() == 0
    run.assert_called_once()
    legacy.assert_not_called()


@pytest.mark.parametrize("flag", ["schedule", "serve", "serve_only", "webui", "webui_only", "portfolio", "market_review", "backtest", "etf_rotation"])
def test_unsupported_modes_cannot_enter_legacy_or_broker_paths(flag, monkeypatch):
    fetch = Mock(side_effect=AssertionError("must not fetch"))
    monkeypatch.setattr(aql_runner, "fetch_intraday", fetch)
    assert aql_runner.run(argparse.Namespace(**{flag: True})) == 2
    fetch.assert_not_called()


def test_intraday_adapter_has_no_daily_fallback(monkeypatch):
    import yfinance as yf
    from data_provider.aql_intraday import fetch_intraday
    ticker = Mock()
    ticker.history.return_value = pd.DataFrame({"Close": [100]}, index=pd.DatetimeIndex([NOW]))
    monkeypatch.setattr(yf, "Ticker", lambda symbol: ticker)
    assert "close" in fetch_intraday("NVDA").columns
    assert ticker.history.call_args.kwargs["interval"] == "15m"
    assert ticker.history.call_args.kwargs["prepost"] is False
    ticker.history.side_effect = RuntimeError("unavailable")
    with pytest.raises(DataUnavailable):
        fetch_intraday("NVDA")


@pytest.mark.parametrize("equity,cash,open_risk,reserved", [(1000, 1000, 0, 0), (10000, 500, 25, 50), (9900, 9900, 30, 20), (10500, 10500, 190, 0), (9800, 9800, 0, 0), (10000, 10000, 200, 0)])
def test_every_plan_respects_both_risk_limits(bars, equity, cash, open_risk, reserved):
    account = Account(equity, 10000, cash, open_risk, NOW, positions={})
    for mode in ("conservative", "adventurous"):
        result = assess("NVDA", bars, account, NOW, mode, reserved_risk=reserved)
        if result.plan:
            plan = result.plan
            assert plan["risk"] <= equity * .02
            assert plan["risk"] + open_risk + reserved + max(0, 10000 - equity) <= 200
            assert plan["cost"] <= cash
            assert plan["target_1"] - plan["entry"] >= 2 * (plan["entry"] - plan["stop"])


def test_historical_gap_cannot_reduce_resistance(bars, account):
    # Missing prior-session bars cannot be silently replaced by older bars.
    bars = bars.drop(bars.index[-10])
    assert assess("NVDA", bars, account, NOW).plan is None


def test_arabic_sender_payload_and_legacy_default(monkeypatch):
    from types import SimpleNamespace
    from src.notification_sender.ntfy_sender import NtfySender
    from src.notification_sender.profile_text import arabic_profile
    post = Mock(return_value=Mock(status_code=200))
    monkeypatch.setattr("src.notification_sender.ntfy_sender.requests.post", post)
    sender = NtfySender(SimpleNamespace(ntfy_url="https://ntfy.example/test"))
    token = arabic_profile.set(True)
    try:
        assert sender.send_to_ntfy("# عقل الأسهم")
        assert post.call_args.kwargs["json"]["title"] == "عقل الأسهم"
        assert post.call_args.kwargs["json"]["message"] == "# عقل الأسهم"
    finally:
        arabic_profile.reset(token)
    assert sender.send_to_ntfy("legacy")
    assert "股票分析报告" in post.call_args.kwargs["json"]["title"]


def test_profits_cannot_erase_daily_loss_stop(bars):
    account = Account(10500, 10000, 10500, 0, NOW, daily_loss=200, positions={})
    assert assess("NVDA", bars, account, NOW).plan is None
    payload = {"equity": 10000, "start_equity": 10000, "cash": 10000, "open_risk": 0, "as_of": NOW.isoformat()}
    with pytest.raises(DataUnavailable):
        Account.from_dict(payload, NOW)


def test_finite_inputs_with_overflowing_indicators_never_authorize_entry(bars, account):
    bars["volume"] *= 1e304
    assert (bars.to_numpy() < float("inf")).all()
    assert assess("NVDA", bars, account, NOW).plan is None


def test_unknown_holdings_block_new_entry(bars):
    account = Account(10000, 10000, 10000, 0, NOW)
    result = assess("NVDA", bars, account, NOW)
    assert result.plan is None
    assert result.category == "unclassified"
    assert "المحفظة غير كافية" in result.reason


def test_owned_breakout_is_not_a_buy_candidate(bars):
    account = Account(10000, 10000, 9000, 30, NOW, positions={
        "NVDA": {"shares": 5.5, "average_cost": 98, "stop": 99},
    })
    result = assess("NVDA", bars, account, NOW)
    assert result.plan is None
    assert result.category == "owned"
    assert "لا توصية بدخول" in result.reason
    report = render_report([result], NOW)
    assert "إدارة المراكز المملوكة" in report
    assert "5.5 سهم" in report
    assert "حجم الصفقة:" not in report


def test_daily_stop_does_not_hide_existing_position_warnings(bars):
    account = Account(10000, 10000, 9000, 30, NOW, daily_loss=200, positions={
        "NVDA": {"shares": 5, "average_cost": 98, "stop": 103},
    })
    owned = assess("NVDA", bars, account, NOW)
    candidate = assess("AMD", bars, account, NOW)
    assert owned.plan is None and "راجع قرار الخروج" in owned.reason
    assert candidate.plan is None and "الخسارة اليومية" in candidate.reason
    report = render_report([candidate, owned], NOW)
    assert report.index("إدارة المراكز المملوكة") < report.index("الأسهم المرشحة")


@pytest.mark.parametrize("positions", [[], "NVDA", {"nvda": {}}, {"NVDA": {}},
                                       {"NVDA": {"shares": 0, "average_cost": 100}},
                                       {"NVDA": {"shares": 2, "average_cost": 100, "stop": float("nan")}}])
def test_incomplete_positions_never_imply_empty_portfolio(positions):
    with pytest.raises(DataUnavailable):
        Account.from_dict({"equity": 10000, "start_equity": 10000, "cash": 10000,
                           "open_risk": 0, "daily_loss": 0, "as_of": NOW.isoformat(),
                           "positions": positions}, NOW)


def test_explicit_flat_portfolio_and_valid_held_positions(bars):
    payload = {"equity": 10000, "start_equity": 10000, "cash": 9000,
               "open_risk": 0, "daily_loss": 0, "as_of": NOW.isoformat()}
    unknown = Account.from_dict(payload, NOW)
    assert unknown.positions is None
    assert assess("NVDA", bars, unknown, NOW).plan is None
    payload["positions"] = {}
    assert assess("NVDA", bars, Account.from_dict(payload, NOW), NOW).plan is not None
    payload["positions"] = {"NVDA": {"shares": 1, "average_cost": 100}}
    owned = assess("NVDA", bars, Account.from_dict(payload, NOW), NOW)
    assert owned.category == "owned" and owned.plan is None
    assert "وقف حالي" in owned.reason


def test_owned_positions_are_included_even_outside_candidate_watchlist(bars, monkeypatch, tmp_path):
    class Clock:
        @staticmethod
        def now(tz):
            return NOW
    monkeypatch.setattr(aql_runner, "datetime", Clock)
    fetch = Mock(return_value=bars)
    monkeypatch.setattr(aql_runner, "fetch_intraday", fetch)
    monkeypatch.setenv("AQL_ACCOUNT_JSON", json.dumps({
        "equity": 10000, "start_equity": 10000, "cash": 9000, "open_risk": 0,
        "daily_loss": 0, "as_of": NOW.isoformat(),
        "positions": {"ORCL": {"shares": 3, "average_cost": 100, "stop": 99}},
    }))
    monkeypatch.chdir(tmp_path)
    assert aql_runner.run(argparse.Namespace(stocks="NVDA", no_notify=True)) == 0
    assert {call.args[0] for call in fetch.call_args_list} == {"NVDA", "ORCL"}
    report = next((tmp_path / "reports").glob("*.md")).read_text()
    assert "ORCL" in report and "إدارة المراكز المملوكة" in report


def test_closed_market_runner_never_fetches_or_issues_entry(monkeypatch, tmp_path):
    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, 11, 8, 16, tzinfo=timezone.utc)
    monkeypatch.setattr(aql_runner, "datetime", Clock)
    fetch = Mock(side_effect=AssertionError("must not fetch on Sunday"))
    monkeypatch.setattr(aql_runner, "fetch_intraday", fetch)
    monkeypatch.chdir(tmp_path)
    assert aql_runner.run(argparse.Namespace(stocks="NVDA", no_notify=True)) == 0
    fetch.assert_not_called()
    report = next((tmp_path / "reports").glob("*.md")).read_text()
    assert "السوق الأمريكي مغلق" in report and "حجم الصفقة:" not in report


def test_check_notify_does_not_analyze_or_send(monkeypatch):
    monkeypatch.setattr(aql_runner, "fetch_intraday", Mock(side_effect=AssertionError("no market calls")))
    check = Mock(return_value=("إعدادات المراقبة جاهزة", True))
    monkeypatch.setattr("src.services.aql_notifications.check_settings", check)
    assert aql_runner.run(argparse.Namespace(check_notify=True)) == 0
    check.assert_called_once()


def test_stale_plan_cannot_survive_slow_report_generation(bars, account):
    from src.services.aql_analysis import expire_entry_plans
    result = assess("NVDA", bars, account, NOW)
    assert result.plan
    # Even a refreshed account cannot make an old signal current.
    later = NOW + timedelta(minutes=15)
    fresh_account = Account(10000, 10000, 10000, 0, later, positions={})
    expire_entry_plans([result], fresh_account, later)
    assert result.plan is None
    assert "متأخرة" in result.reason


def test_expired_account_removes_plans_before_publication(bars, account):
    from src.services.aql_analysis import expire_entry_plans
    result = assess("NVDA", bars, account, NOW)
    expire_entry_plans([result], account, NOW + timedelta(minutes=6))
    assert result.plan is None
    assert "حديثة" in result.reason


def test_market_close_removes_even_recent_plan_at_publication(account):
    from src.services.aql_analysis import Assessment, expire_entry_plans
    closing = NOW.replace(hour=21, minute=0)
    account = Account(10000, 10000, 10000, 0, closing, positions={})
    result = Assessment("NVDA", "conservative", metrics={"confirmed_at": (closing - timedelta(minutes=1)).isoformat()}, plan={"shares": 1})
    expire_entry_plans([result], account, closing)
    assert result.plan is None
    assert "السوق مغلق" in result.reason


def test_unprotected_owned_position_blocks_candidate_entry(bars):
    account = Account(10000, 10000, 9000, 0, NOW, positions={
        "AMD": {"shares": 2, "average_cost": 100, "stop": None},
    })
    result = assess("NVDA", bars, account, NOW)
    assert result.plan is None and "دون وقف" in result.reason


def test_malformed_extreme_price_does_not_crash_or_issue_a_plan(bars, account):
    bars.iloc[-1, 1] = 1e30
    bars.iloc[-1, 3] = 1e30
    result = assess("NVDA", bars, account, NOW)
    assert result.plan is None and "خطة دخول آمنة" in result.reason


def test_foreign_exchange_suffix_cannot_enter_us_owned_universe():
    from src.services.aql_analysis import parse_positions
    with pytest.raises(DataUnavailable):
        parse_positions({"BARC.L": {"shares": 1, "average_cost": 100, "stop": 95}})
    assert parse_positions({"BRK-B": {"shares": 1, "average_cost": 100, "stop": 95}})
