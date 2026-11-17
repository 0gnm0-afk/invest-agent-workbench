"""Retained profile and completed-price validation helpers."""
from __future__ import annotations

import math


def validate_profile(profile: dict):
    if profile.get("status") != "review_only":
        raise ValueError("Only review_only screening profiles are supported")
    for market in ("KR", "US"):
        rule = profile[market]
        for key in ("ma_period", "slope_window", "transition_window", "rs_period", "activity_window"):
            value = rule[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"Invalid {market}.{key}")
        for key in ("min_rs", "min_activity_ratio", "min_average_value"):
            value = rule[key]
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError(f"Invalid {market}.{key}")
        if rule["min_activity_ratio"] < 0 or rule["min_average_value"] < 0:
            raise ValueError("Activity thresholds cannot be negative")


def check_bars(bars: list[dict], expected_sessions: list[str]):
    if not bars:
        raise ValueError("no_bars")
    dates = [b["date"] for b in bars]
    if dates != sorted(set(dates)):
        raise ValueError("duplicate_or_unordered_dates")
    if dates[-1] != expected_sessions[-1]:
        raise ValueError("stale_last_session")
    expected = [d for d in expected_sessions if d >= dates[0]]
    if dates != expected:
        raise ValueError("missing_or_non_session_bars")
    for bar in bars:
        for key in ("open", "high", "low", "close", "volume"):
            value = bar[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid_bar_value")
        if bar["low"] <= 0 or not bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]:
            raise ValueError("invalid_ohlc")


def usable_tail(bars: list[dict], sessions: list[str]) -> list[dict]:
    """Use only a contiguous valid suffix, never fill or compress missing bars."""
    dates=[b["date"] for b in bars]
    if dates != sorted(set(dates)):
        raise ValueError("duplicate_or_unordered_dates")
    by_date={b["date"]:b for b in bars}
    tail=[]
    for day in reversed(sessions):
        bar=by_date.get(day)
        if bar is None:
            break
        try:
            check_bars([bar],[day])
        except ValueError:
            break
        tail.append(bar)
    if not tail:
        raise ValueError("stale_or_invalid_last_session")
    return list(reversed(tail))
