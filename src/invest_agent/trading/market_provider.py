"""Completed-session calendars retained after the old morning collector retired."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import exchange_calendars as xcals
import pandas as pd


# Explicit 2026 exchange closures missing from exchange_calendars 4.13.2.
# June 3: https://www.samsungpop.com/ux/kor/customer/notice/notice/noticeViewContent.do?MenuSeqNo=23996
# July 17: https://securities.miraeasset.com/bbs/board/message/view.do?categoryId=1539&messageId=2340801
KR_CLOSURES = {"2026-06-03", "2026-07-17"}


def required_daily_bars(market):
    # Preserve the former 126 display bars + 240(KR)/200(US) warmup - 1.
    return {"KR": 365, "US": 325}[market]


def completed_sessions(market: str, as_of: datetime) -> list[str]:
    if as_of.tzinfo is None:
        raise ValueError("as_of must have timezone")
    cal = xcals.get_calendar("XKRX" if market == "KR" else "XNYS")
    start = (as_of - timedelta(days=550)).date().isoformat()
    end = (as_of + timedelta(days=1)).date().isoformat()
    schedule = cal.schedule.loc[start:end]
    completed = schedule[schedule["close"] + pd.Timedelta(minutes=30) <= pd.Timestamp(as_of)]
    if completed.empty:
        raise ValueError("No completed sessions")
    sessions = [s.date().isoformat() for s in completed.index if market != "KR" or s.date().isoformat() not in KR_CLOSURES]
    if len(sessions) < required_daily_bars(market):
        # Extend the calendar boundary, not the observed prices; preserve the legacy window when sufficient.
        earlier = cal.schedule.loc[:end]
        earlier = earlier[earlier['close'] + pd.Timedelta(minutes=30) <= pd.Timestamp(as_of)]
        sessions = [s.date().isoformat() for s in earlier.index if market != 'KR' or s.date().isoformat() not in KR_CLOSURES][-required_daily_bars(market):]
    if len(sessions) < required_daily_bars(market):
        raise ValueError('Insufficient calendar history')
    return sessions
