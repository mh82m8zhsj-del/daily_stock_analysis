"""Read-only Yahoo 15-minute bars; no daily fallback and no broker access."""
from src.services.aql_analysis import DataUnavailable


def fetch_intraday(symbol):
    import yfinance as yf
    try:
        frame = yf.Ticker(symbol).history(
            period="1mo", interval="15m", prepost=False, auto_adjust=False,
            actions=False, timeout=15, raise_errors=True,
        )
    except Exception as exc:
        raise DataUnavailable("تعذر جلب بيانات شموع 15 دقيقة من مزود السوق") from exc
    if frame.empty:
        raise DataUnavailable("مزود السوق أعاد بيانات فارغة")
    return frame.rename(columns={name: name.lower() for name in frame.columns})
