"""External provider pricing (USD), as of 2026-05."""

PRICING = {
    "fishaudio_tts": {"unit": "utf8_byte", "price_per_million": 15.00},
    "fishaudio_asr": {"unit": "audio_second", "price_per_hour": 0.36},
    "claude_haiku_4_5_input": {"unit": "token", "price_per_million": 1.00},
    "claude_haiku_4_5_output": {"unit": "token", "price_per_million": 5.00},
    "claude_haiku_4_5_cache_write": {"unit": "token", "price_per_million": 1.25},
    "claude_haiku_4_5_cache_read": {"unit": "token", "price_per_million": 0.10},
    # Groq qwen3-32b (organic gray-zone arbiter), pricing as of 2026-06-11
    "groq_qwen3_32b_input": {"unit": "token", "price_per_million": 0.29},
    "groq_qwen3_32b_output": {"unit": "token", "price_per_million": 0.59},
}


def calc_cost(kind: str, amount: float) -> float:
    """Compute USD cost for `amount` units of resource `kind`."""
    if kind not in PRICING:
        return 0.0
    price = PRICING[kind]
    unit = price["unit"]
    if unit == "utf8_byte":
        return amount * price["price_per_million"] / 1_000_000
    if unit == "audio_second":
        return amount / 3600.0 * price["price_per_hour"]
    if unit == "audio_minute":
        return amount / 60.0 * price["price_per_minute"]
    if unit == "token":
        return amount * price["price_per_million"] / 1_000_000
    return 0.0
