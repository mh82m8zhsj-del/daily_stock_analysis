"""CLI integration using existing notification transports without LLM generation."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from data_provider.aql_intraday import fetch_intraday
from src.services.aql_analysis import (
    Account, Assessment, DataUnavailable, MODES, WATCHLIST, assess, render_report,
)


def run(args):
    now = datetime.now(timezone.utc)
    mode = os.getenv("AQL_MODE", "conservative").strip() or "conservative"
    if mode not in MODES:
        print("خطأ في الإعدادات: اختر conservative للوضع المحافظ أو adventurous للوضع المغامر")
        return 2
    if any(getattr(args, name, False) for name in (
        "schedule", "serve", "serve_only", "webui", "webui_only", "market_review", "portfolio",
        "backtest", "etf_rotation", "check_notify",
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
    results = []
    reserved_risk = reserved_cash = 0.0
    failed = False
    for symbol in symbols:
        if symbol not in WATCHLIST:
            results.append(assess(symbol, None, account, now, mode))
            continue
        try:
            frame = fetch_intraday(symbol)
            result = assess(symbol, frame, account, datetime.now(timezone.utc), mode,
                            reserved_risk=reserved_risk, reserved_cash=reserved_cash)
        except DataUnavailable as exc:
            result = Assessment(symbol, mode, str(exc))
            failed = True
        # One report is one basket: reserve both cash and stop risk across symbols.
        if result.plan:
            reserved_risk += result.plan["risk"]
            reserved_cash += result.plan["cost"]
        results.append(result)
    content = render_report(results, now)
    print(content)
    folder = Path("reports")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"aql_{now.strftime('%Y%m%d_%H%M%S_%f')}_{mode}.md").write_text(content, encoding="utf-8")
    if not getattr(args, "no_notify", False) and not getattr(args, "dry_run", False):
        from src.notification import NotificationService
        from src.notification_sender.profile_text import arabic_profile
        token = arabic_profile.set(True)
        try:
            notifier = NotificationService()
            # Text preserves Arabic shaping and avoids legacy image-only captions.
            notifier._markdown_to_image_channels = set()
            if notifier.is_available():
                if not notifier.send(content, route_type="report"):
                    print("تعذر إرسال تقرير عقل الأسهم عبر قنوات التنبيه المهيأة")
                    return 1
        finally:
            arabic_profile.reset(token)
    return 1 if failed else 0
