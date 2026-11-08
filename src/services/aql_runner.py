"""CLI integration using existing notification transports without LLM generation."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from data_provider.aql_intraday import fetch_intraday
from src.services.aql_analysis import (
    Account, DataUnavailable, MODES, WATCHLIST, assess, expire_entry_plans, render_report, session_bounds,
)


def run(args):
    now = datetime.now(timezone.utc)
    mode = os.getenv("AQL_MODE", "conservative").strip() or "conservative"
    if mode not in MODES:
        print("خطأ في الإعدادات: اختر conservative للوضع المحافظ أو adventurous للوضع المغامر")
        return 2
    if getattr(args, "check_notify", False):
        from src.services.aql_notifications import check_settings
        text, ok = check_settings(args, now)
        print(text)
        return 0 if ok else 1
    if any(getattr(args, name, False) for name in (
        "schedule", "serve", "serve_only", "webui", "webui_only", "market_review", "portfolio",
        "backtest", "etf_rotation",
    )):
        print("عقل الأسهم يدعم التحليل الفردي للأسهم فقط؛ استخدم التشغيل المجدول الخارجي لتكرار التحليل")
        return 2
    symbols = getattr(args, "stocks", None) or os.getenv("AQL_STOCK_LIST", "")
    symbols = list(dict.fromkeys(s.strip().upper() for s in symbols.split(",") if s.strip())) if symbols else list(WATCHLIST)
    try:
        payload = json.loads(os.getenv("AQL_ACCOUNT_JSON", "null"))
        account = Account.from_dict(payload, now)
    except (DataUnavailable, ValueError, TypeError):
        account = None  # Market analysis remains useful; entry is always blocked.
    if account is not None and account.positions:
        symbols = list(dict.fromkeys([*symbols, *account.positions]))
    if not symbols:
        print("قائمة الأسهم فارغة؛ حدد أسهمًا صالحة قبل التحليل")
        return 2
    results = []
    reserved_risk = reserved_cash = 0.0
    failed = False
    for symbol in symbols:
        if symbol not in WATCHLIST and not (account and account.positions and symbol in account.positions):
            results.append(assess(symbol, None, account, now, mode))
            continue
        try:
            _, _, opening, closing = session_bounds(datetime.now(timezone.utc))
            if not opening <= datetime.now(timezone.utc) < closing:
                raise DataUnavailable("خارج ساعات الجلسة الأمريكية؛ لا دخول جديد")
        except DataUnavailable as exc:
            result = assess(symbol, None, account, now, mode)
            result.reason = str(exc)
            results.append(result)
            continue
        try:
            frame = fetch_intraday(symbol)
            result = assess(symbol, frame, account, datetime.now(timezone.utc), mode,
                            reserved_risk=reserved_risk, reserved_cash=reserved_cash)
        except DataUnavailable as exc:
            result = assess(symbol, None, account, now, mode)
            result.reason = str(exc)
            failed = True
        # One report is one basket: reserve both cash and stop risk across symbols.
        if result.plan:
            reserved_risk += result.plan["risk"]
            reserved_cash += result.plan["cost"]
        results.append(result)
    published_at = datetime.now(timezone.utc)
    expire_entry_plans(results, account, published_at)
    content = render_report(results, published_at)
    print(content)
    folder = Path("reports")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"aql_{now.strftime('%Y%m%d_%H%M%S_%f')}_{mode}.md").write_text(content, encoding="utf-8")
    from src.services.aql_notifications import dispatch_report
    delivery_status = dispatch_report(content, args)
    return 1 if failed or delivery_status else 0
