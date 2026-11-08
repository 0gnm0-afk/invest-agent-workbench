"""Lightweight frozen rule catalog; safe for context reads without NumPy imports."""
POLICY = 'research-indicators-v1'
FINAL_RESEARCH = {
    'commit': '54256ca5721c7862d45a8cf9d52d35ada9e175ed',
    'uptrend': 'https://github.com/0gnm0-afk/invest-trend-research-final/blob/54256ca5721c7862d45a8cf9d52d35ada9e175ed/final/uptrend-interpretation-ko.md',
    'downtrend': 'https://github.com/0gnm0-afk/invest-trend-research-final/blob/54256ca5721c7862d45a8cf9d52d35ada9e175ed/final/downtrend-interpretation-ko.md',
    'scope': 'Final 15-year retrospective signal evidence; not trading profitability proof',
    'buy_priority': ['cci_recovery', 'cci-100', 'rsi30', 'macd_rsi40'],
    'buy_cci_rsi_combination': 'not_established_in_final_research',
}
PROPOSED_RULES = ['cci_recovery', 'cci-100', 'rsi30', 'macd_signal_down', 'rsi60_down', 'macd_rsi60']
RULES = {
    'rsi30': ('buy', 'RSI14 30 상향 교차'),
    'cci-100': ('buy', 'CCI20 -100 상향 교차'),
    'cci_recovery': ('buy', 'CCI20 -100 상향 교차·SMA20 하락'),
    'macd_signal_down': ('sell', 'MACD 시그널 하향 교차'),
    'rsi60_down': ('sell', 'RSI14 60 하향 교차'),
    'macd_rsi60': ('sell', 'MACD 하향 교차·최근 3거래일 RSI60 이탈'),
}
