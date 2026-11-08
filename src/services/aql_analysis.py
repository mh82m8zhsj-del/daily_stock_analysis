"""Deterministic, Arabic, read-only US breakout analysis for عقل الأسهم.

No model output or daily-bar fallback is allowed to authorize an entry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
import math
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")
WATCHLIST = ("NVDA", "MSFT", "AAPL", "AMD", "GOOGL", "META", "AMZN", "AVGO", "PLTR", "TSLA")
MODES = {"conservative": (2.0, 0.005), "adventurous": (1.5, 0.0)}
MODE_NAMES = {"conservative": "محافظ", "adventurous": "مغامر"}


class DataUnavailable(ValueError):
    """A required input is missing, inconsistent or stale."""


def positive(value, *, zero=False):
    if isinstance(value, bool):
        raise DataUnavailable("بيانات رقمية غير صالحة")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataUnavailable("بيانات رقمية ناقصة") from exc
    if not math.isfinite(result) or result < 0 or (not zero and result == 0):
        raise DataUnavailable("بيانات رقمية غير صالحة")
    return result


@dataclass(frozen=True)
class Account:
    equity: float
    start_equity: float
    cash: float
    open_risk: float
    as_of: datetime
    daily_loss: float = 0.0

    @classmethod
    def from_dict(cls, payload, now):
        try:
            stamp = datetime.fromisoformat(payload["as_of"].replace("Z", "+00:00"))
            account = cls(
                positive(payload["equity"]), positive(payload["start_equity"]),
                positive(payload["cash"], zero=True), positive(payload["open_risk"], zero=True),
                stamp, positive(payload["daily_loss"], zero=True),
            )
            account.validate(now)
            return account
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise DataUnavailable("لقطة الحساب ناقصة أو غير صالحة أو أقدم من خمس دقائق") from exc

    def validate(self, now):
        for value in (self.equity, self.start_equity):
            positive(value)
        for value in (self.cash, self.open_risk, self.daily_loss):
            positive(value, zero=True)
        if (self.as_of.tzinfo is None or now.tzinfo is None
                or not timedelta(0) <= now - self.as_of <= timedelta(minutes=5)
                or self.as_of.astimezone(NY).date() != now.astimezone(NY).date()
                or self.cash > self.equity):
            raise DataUnavailable("لقطة الحساب غير متسقة أو غير حديثة")

    @property
    def remaining_risk(self):
        # Equity includes realized AND unrealized P&L. Profits never expand the daily cap.
        loss = max(self.daily_loss, self.start_equity - self.equity, 0.0)
        return max(0.0, self.start_equity * 0.02 - loss - self.open_risk)


@dataclass
class Assessment:
    symbol: str
    mode: str
    reason: str = "لا توجد إشارة دخول مؤكدة"
    metrics: dict = field(default_factory=dict)
    plan: dict | None = None


def session_bounds(now):
    """Fail closed on holidays, early closes or calendar failures."""
    if now.tzinfo is None:
        raise DataUnavailable("توقيت التحليل يجب أن يتضمن المنطقة الزمنية")
    try:
        import exchange_calendars as xcals
        calendar = xcals.get_calendar("XNYS")
        day = pd.Timestamp(now.astimezone(NY).date())
        if not calendar.is_session(day):
            raise DataUnavailable("السوق الأمريكي مغلق اليوم")
        return calendar, day, calendar.session_open(day), calendar.session_close(day)
    except DataUnavailable:
        raise
    except Exception as exc:
        raise DataUnavailable("تعذر التحقق من تقويم جلسة السوق الأمريكي") from exc


def indicators(frame, now):
    calendar, day, opening, closing = session_bounds(now)
    if not opening <= pd.Timestamp(now) < closing:
        raise DataUnavailable("خارج ساعات الجلسة الأمريكية؛ لا دخول جديد")
    required = ["open", "high", "low", "close", "volume"]
    if not isinstance(frame, pd.DataFrame) or not set(required).issubset(frame.columns):
        raise DataUnavailable("بيانات شموع 15 دقيقة ناقصة")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None or frame.index.has_duplicates:
        raise DataUnavailable("توقيت الشموع ناقص أو مكرر")
    bars = frame[required].sort_index().copy()
    if (bars.index > pd.Timestamp(now)).any():
        raise DataUnavailable("بيانات شموع مؤرخة في المستقبل")
    # Yahoo timestamps are bar OPEN times. Never use the still-forming candle.
    bars = bars.loc[bars.index + pd.Timedelta(minutes=15) <= pd.Timestamp(now)]
    if len(bars) < 21:
        raise DataUnavailable("يلزم سجل مكتمل لحساب الدعم والمقاومة")
    try:
        bars = bars.astype(float)
    except (TypeError, ValueError) as exc:
        raise DataUnavailable("قيم الشموع غير صالحة") from exc
    if (not all(math.isfinite(v) and v > 0 for v in bars.to_numpy().ravel())
            or (bars.high < bars[["open", "close", "low"]].max(axis=1)).any()
            or (bars.low > bars[["open", "close", "high"]].min(axis=1)).any()):
        raise DataUnavailable("أسعار أو أحجام الشموع غير صالحة")
    current = bars.loc[(bars.index >= opening) & (bars.index < closing)]
    expected = pd.date_range(opening, min(pd.Timestamp(now) - pd.Timedelta(minutes=15), closing), freq="15min")
    if len(current) < 5 or not current.index.equals(expected):
        raise DataUnavailable("شموع الجلسة ناقصة أو غير منتظمة؛ يلزم خمس شموع مغلقة")
    last = current.iloc[-1]
    stamp = current.index[-1]
    if pd.Timestamp(now) - (stamp + pd.Timedelta(minutes=15)) >= pd.Timedelta(minutes=15):
        raise DataUnavailable("بيانات السوق متأخرة عن آخر إغلاق مطلوب")
    # RVOL compares the same 15-minute slot in the previous FIVE trading sessions.
    slot = stamp - opening
    baseline = []
    previous_day = day
    for _ in range(5):
        previous_day = calendar.previous_session(previous_day)
        reference = calendar.session_open(previous_day) + slot
        if reference >= calendar.session_close(previous_day) or reference not in bars.index:
            raise DataUnavailable("سجل الحجم النسبي لنفس الفترة في خمس جلسات غير مكتمل")
        baseline.append(float(bars.loc[reference, "volume"]))
    prior = bars.loc[bars.index < stamp].tail(20)
    expected_history = expected
    history_day = day
    while len(expected_history) < 21:
        history_day = calendar.previous_session(history_day)
        grid = pd.date_range(calendar.session_open(history_day), calendar.session_close(history_day),
                             freq="15min", inclusive="left")
        expected_history = grid.append(expected_history)
    if not bars.tail(21).index.equals(expected_history[-21:]):
        raise DataUnavailable("سجل الدعم والمقاومة يحتوي شموعًا مفقودة أو غير منتظمة")
    vwap = float((((current.high + current.low + current.close) / 3) * current.volume).sum() / current.volume.sum())
    metrics = {
        "close": float(last.close), "vwap": vwap,
        "rvol": float(last.volume) / (sum(baseline) / len(baseline)),
        "momentum": float(last.close / current.iloc[-5].close - 1),
        "resistance": float(prior.high.max()), "support": float(prior.tail(4).low.min()),
        "confirmed_at": (stamp + pd.Timedelta(minutes=15)).isoformat(),
    }
    if not all(math.isfinite(value) for key, value in metrics.items() if key != "confirmed_at"):
        raise DataUnavailable("تعذر حساب مؤشرات رقمية صالحة من البيانات المتاحة")
    return metrics


def assess(symbol, frame, account, now, mode="conservative", *, reserved_risk=0.0, reserved_cash=0.0):
    result = Assessment(symbol, mode)
    if mode not in MODES:
        raise ValueError("وضع التحليل غير معروف")
    if symbol not in WATCHLIST:
        result.reason = "الرمز خارج قائمة ناسداك للتكنولوجيا والذكاء الاصطناعي المعتمدة"
        return result
    try:
        result.metrics = indicators(frame, now)
        if account is None:
            raise DataUnavailable("لقطة الحساب غير متاحة؛ لا يمكن حساب حجم الصفقة والمخاطرة")
        account.validate(now)
        reserved_risk = positive(reserved_risk, zero=True)
        reserved_cash = positive(reserved_cash, zero=True)
        metrics = result.metrics
        rvol_min, momentum_min = MODES[mode]
        if metrics["close"] <= metrics["resistance"]:
            result.reason = "انتظار إغلاق شمعة 15 دقيقة فوق المقاومة لتأكيد الاختراق"
            return result
        if metrics["close"] <= metrics["vwap"] or metrics["rvol"] < rvol_min or metrics["momentum"] <= momentum_min:
            result.reason = "الاختراق لا يستوفي شروط متوسط السعر المرجح بالحجم أو الحجم النسبي أو الزخم"
            return result
        budget = min(account.equity * 0.02, max(0.0, account.remaining_risk - reserved_risk))
        if budget <= 0:
            raise DataUnavailable("بلوغ حد الخسارة اليومية أو استنفاد ميزانية المخاطرة البالغة 2%")
        # Conservative rounding: entry up and stop down to a cent; size always DOWN.
        entry = Decimal(str(metrics["close"])).quantize(Decimal("0.01"), rounding="ROUND_CEILING")
        stop = Decimal(str(metrics["support"])).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        distance = entry - stop
        if distance <= 0:
            raise DataUnavailable("وقف الخسارة غير صالح")
        risk_size = int((Decimal(str(budget)) / distance).to_integral_value(rounding=ROUND_FLOOR))
        available_cash = max(0.0, account.cash - reserved_cash)
        cash_size = int((Decimal(str(available_cash)) / entry).to_integral_value(rounding=ROUND_FLOOR))
        shares = min(risk_size, cash_size)
        if shares < 1:
            raise DataUnavailable("الرصيد أو ميزانية المخاطرة لا يكفيان لشراء سهم واحد")
        result.plan = {
            "entry": float(entry), "stop": float(stop),
            "target_1": float(entry + distance * 2), "target_2": float(entry + distance * 3),
            "shares": shares, "risk": float(distance * shares), "cost": float(entry * shares), "rr": 2.0,
        }
        result.reason = "إشارة دخول افتراضية مؤكدة بشمعة 15 دقيقة مغلقة"
    except DataUnavailable as exc:
        result.reason = str(exc)
    return result


def render_report(results, now):
    lines = ["# عقل الأسهم", f"وقت التحليل: `{now.astimezone(NY).isoformat()}`",
             "ناسداك • التكنولوجيا والذكاء الاصطناعي • تحليل فقط دون تنفيذ صفقات",
             "المخاطرة القصوى للصفقة: 2% • حد الخسارة اليومية: 2% • أدنى عائد إلى مخاطرة: `2:1`",
             "الأسعار بالدولار الأمريكي. الأهداف حسابية وليست توقعًا مضمونًا. الانزلاق والفجوات قد تتجاوز وقف الخسارة.", ""]
    for result in results:
        lines += [f"## {result.symbol} — الوضع {MODE_NAMES[result.mode]}", result.reason]
        m = result.metrics
        if m:
            lines += [f"متوسط السعر المرجح بالحجم: {m['vwap']:.2f} | الحجم النسبي: {m['rvol']:.2f}",
                      f"زخم ساعة: {m['momentum']:.2%} | الدعم: {m['support']:.2f} | المقاومة: {m['resistance']:.2f}",
                      f"إغلاق شمعة التأكيد: `{m['confirmed_at']}`"]
        if result.plan:
            p = result.plan
            lines += [f"الدخول: {p['entry']:.2f} | وقف الخسارة: {p['stop']:.2f}",
                      f"الهدف الأول: {p['target_1']:.2f} (`2:1`) | الهدف الثاني: {p['target_2']:.2f} (`3:1`)",
                      f"حجم الصفقة: {p['shares']} سهم | القيمة: {p['cost']:.2f} | الخسارة المخططة القصوى: {p['risk']:.2f}"]
        else:
            lines.append("قرار الدخول: ممنوع. لا تُعرض مستويات دخول أو أهداف أو حجم صفقة غير قابل للتحقق.")
        lines.append("")
    return "\n".join(lines)
