"""NASDAQ-100 market data client using yfinance + multi-source constituents.

Sources (in priority order):
  1. stockanalysis.com (maintained list of 100 NDX members with sectors)
  2. Hardcoded seed (fallback — updated quarterly; NDX rebalances annually in Dec)
  3. Wikipedia (last resort)

This removes the 403 Forbidden failure caused by Wikipedia's UA policy.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from io import StringIO

import httpx
import pandas as pd
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential

from .logging_setup import get_logger
from .models import StockMarket

log = get_logger(__name__)

STOCKANALYSIS_URL = "https://stockanalysis.com/list/nasdaq-100-stocks/"
NASDAQ100_WIKI_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Wikimedia policy: identify the tool + contact URL.
_WIKI_HEADERS = {
    "User-Agent": (
        "nasdaq-research-agent/1.0 "
        "(+https://github.com/jinhae8971/nasdaq-research-agent; "
        "contact: github issues)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ──────────────────────────────────────────────────────────────────────────────
# Hardcoded Nasdaq-100 seed list (as of 2026-04; last NDX rebalance: Dec 2025)
# Update annually after the December rebalance.
# ──────────────────────────────────────────────────────────────────────────────
__SEED_REV = "2026-04-18"

NASDAQ100_SEED: list[dict] = [
    # Top mega-caps
    {"ticker": "NVDA",  "name": "NVIDIA",           "sector": "Information Technology"},
    {"ticker": "MSFT",  "name": "Microsoft",        "sector": "Information Technology"},
    {"ticker": "AAPL",  "name": "Apple",            "sector": "Information Technology"},
    {"ticker": "AMZN",  "name": "Amazon",           "sector": "Consumer Discretionary"},
    {"ticker": "GOOGL", "name": "Alphabet (A)",     "sector": "Communication Services"},
    {"ticker": "GOOG",  "name": "Alphabet (C)",     "sector": "Communication Services"},
    {"ticker": "META",  "name": "Meta Platforms",   "sector": "Communication Services"},
    {"ticker": "TSLA",  "name": "Tesla",            "sector": "Consumer Discretionary"},
    {"ticker": "AVGO",  "name": "Broadcom",         "sector": "Information Technology"},
    {"ticker": "COST",  "name": "Costco",           "sector": "Consumer Staples"},
    # Remainder (sorted alphabetically for maintainability)
    {"ticker": "ADBE",  "name": "Adobe",            "sector": "Information Technology"},
    {"ticker": "ADI",   "name": "Analog Devices",   "sector": "Information Technology"},
    {"ticker": "ADP",   "name": "Automatic Data Processing", "sector": "Industrials"},
    {"ticker": "ADSK",  "name": "Autodesk",         "sector": "Information Technology"},
    {"ticker": "AEP",   "name": "American Electric Power",   "sector": "Utilities"},
    {"ticker": "AMAT",  "name": "Applied Materials","sector": "Information Technology"},
    {"ticker": "AMD",   "name": "AMD",              "sector": "Information Technology"},
    {"ticker": "AMGN",  "name": "Amgen",            "sector": "Health Care"},
    {"ticker": "APP",   "name": "AppLovin",         "sector": "Information Technology"},
    {"ticker": "ARM",   "name": "Arm Holdings",     "sector": "Information Technology"},
    {"ticker": "ASML",  "name": "ASML",             "sector": "Information Technology"},
    {"ticker": "AXON",  "name": "Axon Enterprise",  "sector": "Industrials"},
    {"ticker": "AZN",   "name": "AstraZeneca",      "sector": "Health Care"},
    {"ticker": "BIIB",  "name": "Biogen",           "sector": "Health Care"},
    {"ticker": "BKNG",  "name": "Booking Holdings", "sector": "Consumer Discretionary"},
    {"ticker": "CDNS",  "name": "Cadence Design",   "sector": "Information Technology"},
    {"ticker": "CDW",   "name": "CDW",              "sector": "Information Technology"},
    {"ticker": "CEG",   "name": "Constellation Energy", "sector": "Utilities"},
    {"ticker": "CHTR",  "name": "Charter Communications", "sector": "Communication Services"},
    {"ticker": "CMCSA", "name": "Comcast",          "sector": "Communication Services"},
    {"ticker": "CPRT",  "name": "Copart",           "sector": "Industrials"},
    {"ticker": "CRWD",  "name": "CrowdStrike",      "sector": "Information Technology"},
    {"ticker": "CSCO",  "name": "Cisco",            "sector": "Information Technology"},
    {"ticker": "CSGP",  "name": "CoStar Group",     "sector": "Real Estate"},
    {"ticker": "CSX",   "name": "CSX",              "sector": "Industrials"},
    {"ticker": "CTAS",  "name": "Cintas",           "sector": "Industrials"},
    {"ticker": "CTSH",  "name": "Cognizant",        "sector": "Information Technology"},
    {"ticker": "DASH",  "name": "DoorDash",         "sector": "Consumer Discretionary"},
    {"ticker": "DDOG",  "name": "Datadog",          "sector": "Information Technology"},
    {"ticker": "DXCM",  "name": "Dexcom",           "sector": "Health Care"},
    {"ticker": "EA",    "name": "Electronic Arts",  "sector": "Communication Services"},
    {"ticker": "EXC",   "name": "Exelon",           "sector": "Utilities"},
    {"ticker": "FANG",  "name": "Diamondback Energy","sector": "Energy"},
    {"ticker": "FAST",  "name": "Fastenal",         "sector": "Industrials"},
    {"ticker": "FTNT",  "name": "Fortinet",         "sector": "Information Technology"},
    {"ticker": "GEHC",  "name": "GE HealthCare",    "sector": "Health Care"},
    {"ticker": "GFS",   "name": "GlobalFoundries",  "sector": "Information Technology"},
    {"ticker": "GILD",  "name": "Gilead Sciences",  "sector": "Health Care"},
    {"ticker": "HON",   "name": "Honeywell",        "sector": "Industrials"},
    {"ticker": "IDXX",  "name": "IDEXX Laboratories","sector": "Health Care"},
    {"ticker": "INTC",  "name": "Intel",            "sector": "Information Technology"},
    {"ticker": "INTU",  "name": "Intuit",           "sector": "Information Technology"},
    {"ticker": "ISRG",  "name": "Intuitive Surgical","sector": "Health Care"},
    {"ticker": "KDP",   "name": "Keurig Dr Pepper", "sector": "Consumer Staples"},
    {"ticker": "KHC",   "name": "Kraft Heinz",      "sector": "Consumer Staples"},
    {"ticker": "KLAC",  "name": "KLA",              "sector": "Information Technology"},
    {"ticker": "LIN",   "name": "Linde",            "sector": "Materials"},
    {"ticker": "LRCX",  "name": "Lam Research",     "sector": "Information Technology"},
    {"ticker": "LULU",  "name": "Lululemon",        "sector": "Consumer Discretionary"},
    {"ticker": "MAR",   "name": "Marriott",         "sector": "Consumer Discretionary"},
    {"ticker": "MCHP",  "name": "Microchip",        "sector": "Information Technology"},
    {"ticker": "MDB",   "name": "MongoDB",          "sector": "Information Technology"},
    {"ticker": "MDLZ",  "name": "Mondelez",         "sector": "Consumer Staples"},
    {"ticker": "MELI",  "name": "MercadoLibre",     "sector": "Consumer Discretionary"},
    {"ticker": "MNST",  "name": "Monster Beverage", "sector": "Consumer Staples"},
    {"ticker": "MRVL",  "name": "Marvell Technology","sector": "Information Technology"},
    {"ticker": "MSTR",  "name": "MicroStrategy",    "sector": "Information Technology"},
    {"ticker": "MU",    "name": "Micron",           "sector": "Information Technology"},
    {"ticker": "NFLX",  "name": "Netflix",          "sector": "Communication Services"},
    {"ticker": "NXPI",  "name": "NXP Semiconductors","sector": "Information Technology"},
    {"ticker": "ODFL",  "name": "Old Dominion Freight","sector": "Industrials"},
    {"ticker": "ON",    "name": "ON Semiconductor", "sector": "Information Technology"},
    {"ticker": "ORLY",  "name": "O'Reilly Automotive","sector": "Consumer Discretionary"},
    {"ticker": "PANW",  "name": "Palo Alto Networks","sector": "Information Technology"},
    {"ticker": "PAYX",  "name": "Paychex",          "sector": "Industrials"},
    {"ticker": "PCAR",  "name": "PACCAR",           "sector": "Industrials"},
    {"ticker": "PDD",   "name": "PDD Holdings",     "sector": "Consumer Discretionary"},
    {"ticker": "PEP",   "name": "PepsiCo",          "sector": "Consumer Staples"},
    {"ticker": "PLTR",  "name": "Palantir",         "sector": "Information Technology"},
    {"ticker": "PYPL",  "name": "PayPal",           "sector": "Financials"},
    {"ticker": "QCOM",  "name": "Qualcomm",         "sector": "Information Technology"},
    {"ticker": "REGN",  "name": "Regeneron",        "sector": "Health Care"},
    {"ticker": "ROP",   "name": "Roper Technologies","sector": "Information Technology"},
    {"ticker": "ROST",  "name": "Ross Stores",      "sector": "Consumer Discretionary"},
    {"ticker": "SBUX",  "name": "Starbucks",        "sector": "Consumer Discretionary"},
    {"ticker": "SNPS",  "name": "Synopsys",         "sector": "Information Technology"},
    {"ticker": "TEAM",  "name": "Atlassian",        "sector": "Information Technology"},
    {"ticker": "TMUS",  "name": "T-Mobile",         "sector": "Communication Services"},
    {"ticker": "TRI",   "name": "Thomson Reuters",  "sector": "Industrials"},
    {"ticker": "TTD",   "name": "The Trade Desk",   "sector": "Information Technology"},
    {"ticker": "TTWO",  "name": "Take-Two Interactive","sector": "Communication Services"},
    {"ticker": "TXN",   "name": "Texas Instruments","sector": "Information Technology"},
    {"ticker": "VRSK",  "name": "Verisk Analytics", "sector": "Industrials"},
    {"ticker": "VRTX",  "name": "Vertex Pharmaceuticals","sector": "Health Care"},
    {"ticker": "WBD",   "name": "Warner Bros. Discovery","sector": "Communication Services"},
    {"ticker": "WDAY",  "name": "Workday",          "sector": "Information Technology"},
    {"ticker": "XEL",   "name": "Xcel Energy",      "sector": "Utilities"},
    {"ticker": "ZS",    "name": "Zscaler",          "sector": "Information Technology"},
]


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize heterogeneous constituent tables into canonical schema."""
    col_map = {}
    for c in df.columns:
        cl = str(c).lower().strip()
        if cl in ("symbol", "ticker"):
            col_map[c] = "ticker"
        elif cl in ("company", "company name", "security", "name"):
            col_map[c] = "name"
        elif "sector" in cl and "sub" not in cl:
            col_map[c] = "sector"
        elif "sub" in cl and "industry" in cl:
            col_map[c] = "sub_industry"
    df = df.rename(columns=col_map)

    if "ticker" not in df.columns:
        raise RuntimeError("ticker column not found")
    if "name" not in df.columns:
        df["name"] = df["ticker"]
    if "sector" not in df.columns:
        df["sector"] = ""
    if "sub_industry" not in df.columns:
        df["sub_industry"] = ""

    df["ticker"] = df["ticker"].astype(str).str.replace(".", "-", regex=False).str.strip()
    df = df[df["ticker"].str.match(r"^[A-Z\-]+$", na=False)]
    return df[["ticker", "name", "sector", "sub_industry"]]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def _fetch_from_stockanalysis() -> pd.DataFrame:
    """Primary: fetch NASDAQ-100 from stockanalysis.com."""
    resp = httpx.get(
        STOCKANALYSIS_URL,
        headers=_BROWSER_HEADERS,
        follow_redirects=True,
        timeout=30.0,
    )
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))
    if not tables:
        raise RuntimeError("no tables on stockanalysis.com NDX page")
    # The main list table has 100+ rows and a Symbol column
    for t in tables:
        cols_lower = [str(c).lower() for c in t.columns]
        has_sym = any("symbol" in c or "ticker" in c for c in cols_lower)
        if has_sym and len(t) >= 80:
            log.info("fetched %d NDX constituents from stockanalysis.com", len(t))
            return _normalize_df(t)
    raise RuntimeError("no matching NDX constituents table on stockanalysis.com")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def _fetch_from_wikipedia() -> pd.DataFrame:
    """Fallback: scrape NASDAQ-100 from Wikipedia."""
    resp = httpx.get(
        NASDAQ100_WIKI_URL,
        headers=_WIKI_HEADERS,
        follow_redirects=True,
        timeout=20.0,
    )
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))
    for t in tables:
        cols_lower = [str(c).lower() for c in t.columns]
        if any("ticker" in c or "symbol" in c for c in cols_lower) and len(t) >= 80:
            log.info("fetched %d NDX constituents from Wikipedia", len(t))
            return _normalize_df(t)
    raise RuntimeError("no matching NDX constituents table on Wikipedia")


def _seed_dataframe() -> pd.DataFrame:
    """Last-resort: return hardcoded seed list."""
    df = pd.DataFrame(NASDAQ100_SEED)
    df["sub_industry"] = ""
    df["ticker"] = df["ticker"].astype(str).str.replace(".", "-", regex=False).str.strip()
    log.warning(
        "using hardcoded NDX seed list rev=%s (%d tickers) — "
        "both live sources failed",
        __SEED_REV, len(df),
    )
    return df[["ticker", "name", "sector", "sub_industry"]]


def _enrich_sectors_from_seed(df: pd.DataFrame) -> pd.DataFrame:
    """Fill missing sector info by joining with the hardcoded seed's sector map."""
    seed_sector = {row["ticker"]: row["sector"] for row in NASDAQ100_SEED}
    seed_name = {row["ticker"]: row["name"] for row in NASDAQ100_SEED}
    # Fill blank sectors from seed; keep live ticker list authoritative
    df = df.copy()
    df["sector"] = df.apply(
        lambda r: r["sector"] if r.get("sector") else seed_sector.get(r["ticker"], ""),
        axis=1,
    )
    # Prefer seed's canonical short name if live source has a long marketing name
    # (but only if ticker exists in seed — this keeps new entrants intact)
    df["name"] = df.apply(
        lambda r: seed_name.get(r["ticker"]) or r["name"],
        axis=1,
    )
    return df


def _fetch_nasdaq100_constituents() -> pd.DataFrame:
    """Try stockanalysis → Wikipedia → hardcoded seed."""
    try:
        df = _fetch_from_stockanalysis()
        return _enrich_sectors_from_seed(df)
    except Exception as exc:  # noqa: BLE001
        log.warning("stockanalysis.com failed: %s — trying Wikipedia", exc)
    try:
        df = _fetch_from_wikipedia()
        return _enrich_sectors_from_seed(df)
    except Exception as exc:  # noqa: BLE001
        log.warning("Wikipedia failed: %s — using hardcoded seed", exc)
    return _seed_dataframe()


def _recent_trading_dates(n: int) -> list[str]:
    """Return the last N US trading dates by checking QQQ data."""
    end = datetime.now()
    start = end - timedelta(days=n + 15)
    qqq = yf.download("QQQ", start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"),
                       progress=False, auto_adjust=True)
    if qqq.empty:
        raise RuntimeError("could not fetch QQQ data to determine trading dates")
    dates = sorted(qqq.index.strftime("%Y-%m-%d").tolist())
    return dates[-n:] if len(dates) >= n else dates


def fetch_all_markets() -> tuple[list[StockMarket], str]:
    """Fetch NASDAQ-100 constituents with latest market data."""
    constituents = _fetch_nasdaq100_constituents()
    tickers = constituents["ticker"].tolist()
    meta_by_ticker = {row["ticker"]: row for _, row in constituents.iterrows()}

    log.info("downloading market data for %d NDX tickers...", len(tickers))
    data = yf.download(
        tickers,
        period="5d",
        progress=False,
        auto_adjust=True,
        group_by="ticker",
        threads=True,
    )

    if data.empty:
        raise RuntimeError("yfinance returned empty data")

    trading_date = data.index[-1].strftime("%Y-%m-%d")
    prev_date = data.index[-2].strftime("%Y-%m-%d") if len(data.index) >= 2 else None

    stocks: list[StockMarket] = []
    for ticker in tickers:
        meta = meta_by_ticker.get(ticker, {})
        try:
            ticker_data = data if len(tickers) == 1 else data[ticker]

            latest = ticker_data.iloc[-1]
            close = float(latest["Close"])
            if close <= 0 or pd.isna(close):
                continue

            volume = int(latest["Volume"]) if not pd.isna(latest["Volume"]) else 0
            trading_value = close * volume

            change_pct = 0.0
            if prev_date and len(ticker_data) >= 2:
                prev_close = float(ticker_data.iloc[-2]["Close"])
                if prev_close > 0 and not pd.isna(prev_close):
                    change_pct = (close - prev_close) / prev_close * 100.0

            stocks.append(
                StockMarket(
                    ticker=ticker,
                    name=meta.get("name", ticker),
                    sector=meta.get("sector", ""),
                    sub_industry=meta.get("sub_industry", ""),
                    close=close,
                    open=float(latest["Open"]) if not pd.isna(latest["Open"]) else 0,
                    high=float(latest["High"]) if not pd.isna(latest["High"]) else 0,
                    low=float(latest["Low"]) if not pd.isna(latest["Low"]) else 0,
                    volume=volume,
                    trading_value=trading_value,
                    market_cap=0,
                    change_pct=change_pct,
                )
            )
        except (KeyError, IndexError):
            continue

    _enrich_market_caps(stocks)

    log.info("fetched %d NASDAQ-100 stocks for %s", len(stocks), trading_date)
    return stocks, trading_date


def _enrich_market_caps(stocks: list[StockMarket]) -> None:
    """Best-effort market cap enrichment."""
    ticker_symbols = [s.ticker for s in stocks[:50]]
    try:
        tickers_obj = yf.Tickers(" ".join(ticker_symbols))
        for s in stocks:
            try:
                info = tickers_obj.tickers.get(s.ticker)
                if info and hasattr(info, "fast_info"):
                    mcap = getattr(info.fast_info, "market_cap", None)
                    if mcap and mcap > 0:
                        s.market_cap = float(mcap)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        log.warning("market cap enrichment failed: %s", exc)


def get_recent_trading_date() -> str:
    dates = _recent_trading_dates(1)
    return dates[-1]


def get_past_trading_date(days_back: int) -> str:
    dates = _recent_trading_dates(days_back + 1)
    if len(dates) <= days_back:
        raise RuntimeError(f"could not find {days_back} past trading days")
    return dates[-(days_back + 1)]
