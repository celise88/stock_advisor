from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import certifi
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
STATIC_DIR = ROOT_DIR / "app" / "static"

load_dotenv(ROOT_DIR / ".env", override=False)

# Ensure HTTP clients that depend on OpenSSL/libcurl can locate a valid CA bundle.
# This helps on macOS environments where system cert discovery can be inconsistent.
_CA_BUNDLE = certifi.where()
os.environ.setdefault("SSL_CERT_FILE", _CA_BUNDLE)
os.environ.setdefault("REQUESTS_CA_BUNDLE", _CA_BUNDLE)
os.environ.setdefault("CURL_CA_BUNDLE", _CA_BUNDLE)


@dataclass
class Settings:
    app_name: str = "Stock Advisor"
    scanner_interval_sec: int = int(os.getenv("SCANNER_INTERVAL_SEC", "45"))
    scanner_market_cap_min: float = float(os.getenv("SCANNER_MARKET_CAP_MIN", "2000000000"))
    scanner_avg_volume_min: float = float(os.getenv("SCANNER_AVG_VOLUME_MIN", "500000"))
    scanner_relative_volume_min: float = float(os.getenv("SCANNER_RELATIVE_VOLUME_MIN", "1.5"))
    disable_yfinance: bool = os.getenv("DISABLE_YFINANCE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    scanner_symbols: List[str] = field(
        default_factory=lambda: [
            "AAPL",
            "MSFT",
            "NVDA",
            "AMD",
            "META",
            "AMZN",
            "GOOGL",
            "IBM",
            "TSLA",
            "PLTR",
            "NFLX",
            "AVGO",
            "MRVL",
            "ARM",
            "SMCI",
            "CRM",
            "ORCL",
            "INTC",
            "QCOM",
            "MU",
            "HPQ",
            "COIN",
            "HOOD",
            "SHOP",
            "UBER",
            "ABNB",
            "SNOW",
            "DASH",
            "CRWD",
            "WDAY",
            "GTLB",
            "RDDT",
            "PANW",
            "OKTA",
            "DDOG",
            "ADBE",
            "PYPL",
            "SOFI",
            "UPST",
            "BBY",
            "GM",
            "MGM",
            "NKE",
            "GIS",
            "DAL",
            "AA",
            "JPM",
            "BAC",
            "WFC",
            "GS",
            "XOM",
            "CVX",
            "LLY",
            "UNH",
            "JNJ",
            "PFE",
            "NVO",
            "IWM",
            "RGTI",
            "QBTS",
            "QUBT",
            "IONQ",
            "IOT",
            "TEM",
            "RKLB",
            "SWKS",
            "FTNT",
            "XYZ",
            "TXN",
            "QCOM",
            "AMAT",
            "LRCX",
            "V",
            "AXP",
            "C",
            "COF",
            "BLK",
            "BX",
            "KKR",
            "CAT",
            "DE",
            "PG",
            "WMT",
            "DIS",
            "BKNG",
            "HD",
            "APP",
            "VZ",
            "T",
            "UAL",
            "JBHT",
            "HOMB",
            "KARO",
            "ISRG",
            "VIST",
            "FNB",
            "FFIN",
            "INDB",
            "CNS",
            "SFNC",
            "TSM",
            "UNH",
            "ISRG",
            "PLD",
            "USB",
            "STT",
            "CFG",

        ]
    )
    
    sec_user_agent: str = os.getenv(
        "SEC_USER_AGENT",
        "stock_advisor/1.0 (contact: developer@example.com)",
    )
    finnhub_api_key: str = os.getenv("FINNHUB_API_KEY", os.getenv("FINHUB_API_KEY", "")).strip()
    alphavantage_api_key: str = os.getenv("ALPHAVANTAGE_API_KEY", "").strip()
    fred_api_key: str = os.getenv("FRED_API_KEY", "").strip()
    api_ninjas_key: str = os.getenv("API_NINJAS_KEY", "").strip()
    schwab_account_id: str = os.getenv("SCHWAB_ACCOUNT_ID", "").strip()
    schwab_api_key: str = os.getenv("SCHWAB_API_KEY", "").strip()
    schwab_app_secret: str = os.getenv("SCHWAB_APP_SECRET", "").strip().strip("'").strip('"')
    schwab_token_path: Path = Path(
        os.getenv("SCHWAB_TOKEN_PATH", str(ROOT_DIR / "schwab_token.json"))
    )


SETTINGS = Settings()
DATA_DIR.mkdir(parents=True, exist_ok=True)
