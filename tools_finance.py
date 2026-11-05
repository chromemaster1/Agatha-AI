import sys

TICKER_ALIASES = {
    "bitcoin": "BTC-USD", "btc": "BTC-USD",
    "ethereum": "ETH-USD", "eth": "ETH-USD",
    "dogecoin": "DOGE-USD", "doge": "DOGE-USD",
    "solana": "SOL-USD",
    "apple": "AAPL", "tesla": "TSLA", "amazon": "AMZN",
    "google": "GOOGL", "microsoft": "MSFT", "nvidia": "NVDA",
    "s&p 500": "^GSPC", "s and p 500": "^GSPC", "sp500": "^GSPC",
    "nasdaq": "^IXIC", "dow jones": "^DJI", "dow": "^DJI",
}


def fetch_market_data(symbol: str, period: str = "5d", interval: str = "1h") -> dict:
    """Fetch recent price history and basic technical indicators for a
    ticker via yfinance (free, no API key). Returns factual numbers only —
    no interpretation, no advice. Raises on bad symbols or fetch failure."""
    try:
        import yfinance as yf
    except ImportError as e:
        raise ImportError(
            f"The 'yfinance' package is not installed for THIS interpreter ({sys.executable}): {e}\n"
            f"Install with:\n    {sys.executable} -m pip install yfinance"
        )

    ticker = yf.Ticker(symbol)
    hist = ticker.history(period=period, interval=interval)
    if hist.empty:
        raise RuntimeError(
            f"No data found for '{symbol}'. Check the ticker symbol — e.g. BTC-USD, ETH-USD, AAPL, TSLA."
        )

    closes = hist["Close"].dropna()
    if closes.empty:
        raise RuntimeError(f"No usable price data returned for '{symbol}'.")

    latest = float(closes.iloc[-1])
    first = float(closes.iloc[0])
    change = latest - first
    pct_change = (change / first * 100) if first else 0.0

    def _safe_round(x, nd=2):
        return round(float(x), nd) if x == x else None  # x == x is False for NaN

    sma_short = closes.rolling(window=min(10, len(closes))).mean().iloc[-1] if len(closes) >= 2 else latest
    sma_long = closes.rolling(window=min(30, len(closes))).mean().iloc[-1] if len(closes) >= 5 else sma_short

    delta = closes.diff().dropna()
    rsi = None
    if len(delta) >= 2:
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        window = min(14, len(delta))
        avg_gain = gain.rolling(window=window).mean().iloc[-1]
        avg_loss = loss.rolling(window=window).mean().iloc[-1]
        if avg_loss and avg_loss == avg_loss and avg_loss != 0:
            rsi = 100 - (100 / (1 + (avg_gain / avg_loss)))
        elif avg_loss == 0:
            rsi = 100.0

    return {
        "symbol": symbol.upper(),
        "latest_price": _safe_round(latest),
        "period_change": _safe_round(change),
        "period_pct_change": _safe_round(pct_change),
        "sma_short": _safe_round(sma_short),
        "sma_long": _safe_round(sma_long),
        "rsi": _safe_round(rsi, 1),
        "period": period,
        "interval": interval,
        "data_points": len(closes),
    }
