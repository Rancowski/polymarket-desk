from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    dry_run: bool = _b("DRY_RUN", True)
    private_key: str = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
    funder: str = os.getenv("POLYMARKET_FUNDER", "").strip()
    signature_type: int = _i("SIGNATURE_TYPE", 0)
    chain_id: int = _i("CHAIN_ID", 137)
    poly_api_key: str = os.getenv("POLY_API_KEY", "").strip()
    poly_api_secret: str = os.getenv("POLY_API_SECRET", "").strip()
    poly_api_passphrase: str = os.getenv("POLY_API_PASSPHRASE", "").strip()
    xai_api_key: str = os.getenv("XAI_API_KEY", "").strip()
    grok_model: str = os.getenv("GROK_MODEL", "grok-4.6").strip()
    max_position_pct: float = _f("MAX_POSITION_PCT", 0.12)
    min_net_edge: float = _f("MIN_NET_EDGE", 0.012)
    model_haircut: float = _f("MODEL_HAIRCUT", 0.0)
    kelly_fraction: float = _f("KELLY_FRACTION", 0.25)
    xai_prepaid_usd: float = _f("XAI_PREPAID_USD", 0)
    xai_management_key: str = os.getenv("XAI_MANAGEMENT_KEY", "").strip()
    xai_team_id: str = os.getenv("XAI_TEAM_ID", "default").strip()
    max_open_positions: int = _i("MAX_OPEN_POSITIONS", 3)
    max_category_pct: float = _f("MAX_CATEGORY_PCT", 0.40)
    daily_loss_halt_pct: float = _f("DAILY_LOSS_HALT_PCT", 0.06)
    weekly_loss_halt_pct: float = _f("WEEKLY_LOSS_HALT_PCT", 0.15)
    min_liquidity_usd: float = _f("MIN_LIQUIDITY_USD", 1500)
    min_volume_24h_usd: float = _f("MIN_VOLUME_24H_USD", 500)
    min_book_multiple: float = _f("MIN_BOOK_MULTIPLE", 3)
    max_spread: float = _f("MAX_SPREAD", 0.08)
    loop_seconds: int = _i("CYCLE_SECONDS", 0) or _i("LOOP_SECONDS", 300)
    estimate_batch: int = _i("ESTIMATE_BATCH", 12)
    live_search: bool = _b("LIVE_SEARCH", False)
    paper_bankroll_usd: float = _f("PAPER_BANKROLL_USD", 1000)
    dashboard_port: int = _i("DASHBOARD_PORT", 8788)
    dashboard_token: str = os.getenv("DASHBOARD_TOKEN", "").strip()
    data_dir: Path = ROOT / "data"
    halt_file: Path = ROOT / "HALT"

    @property
    def clob_host(self) -> str:
        return "https://clob.polymarket.com"

    @property
    def gamma_host(self) -> str:
        return "https://gamma-api.polymarket.com"


settings = Settings()

FEE_RATE = {
    "crypto": 0.07,
    "sports": 0.05,
    "finance": 0.04,
    "politics": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "tech": 0.04,
    "mentions": 0.04,
    "geopolitics": 0.0,
    "other": 0.05,
}

SKIP_QUESTION_PATTERNS = (
    "up or down",
    "up/down",
    "15m",
    "15 min",
    "5m",
    "5 min",
    "5-minute",
    "15-minute",
    "next 15",
    "next 5 minute",
)
