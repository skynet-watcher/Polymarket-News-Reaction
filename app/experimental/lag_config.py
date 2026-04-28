"""
**Experimental — not connected to live gating or the signal pipeline; research use only.**

Lag configuration: per-category observation windows.

CATEGORY_OBSERVATION_HOURS controls how far forward from signal_time the lag
module looks for price threshold crossings. Categories map to Market.market_type.
Markets with faster-moving underlying data (crypto) warrant shorter windows;
slower fundamentals (approval polls, weather) warrant longer ones.

NOT wired into the live pipeline yet. These values will be read by compute_lag
once market_type is populated in the Market table.
"""

from __future__ import annotations

# market_type → max observation window in hours.
# Key is the string stored in Market.market_type.
CATEGORY_OBSERVATION_HOURS: dict[str, int] = {
    "CRYPTO_HOURLY": 4,
    "WEATHER_DAILY": 48,
    "TSA_DATA": 72,
    "SPORTS": 24,
    "TRUMP_APPROVAL": 96,
    "TRUMP_SOCIAL": 6,
    "GOVT_ACTION": 48,
    "BOX_OFFICE": 72,
    "BILLBOARD": 168,  # weekly chart cycle
    "POP_CULTURE": 48,
    "OTHER": 24,
}

# Fallback when market_type is NULL or unrecognised.
DEFAULT_OBSERVATION_HOURS: int = 24

# Control-group markets are included in lag scoring but excluded from bias
# analysis. Any market with Market.is_control_market == True is control.
CONTROL_GROUP_MARKET_TYPES: frozenset[str] = frozenset(
    {
        "CRYPTO_HOURLY",  # high-frequency; baseline for speed comparison
    }
)
