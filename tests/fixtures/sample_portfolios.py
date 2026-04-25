"""Sample portfolios for dashboard testing"""


def sample_small_portfolio():
    """5-holding portfolio for quick testing"""
    return {
        "summary": {
            "total_value": 100000,
            "position_count": 5,
            "as_of": "2026-04-17",
        },
        "top_holdings": [
            {"symbol": "AAPL", "value": 30000, "pct": 30.0},
            {"symbol": "MSFT", "value": 25000, "pct": 25.0},
            {"symbol": "GOOGL", "value": 20000, "pct": 20.0},
            {"symbol": "AMZN", "value": 15000, "pct": 15.0},
            {"symbol": "NVDA", "value": 10000, "pct": 10.0},
        ],
        "sector_weights": {"Technology": 100.0},
    }


def sample_medium_portfolio():
    """30-holding diversified portfolio"""
    return {
        "summary": {
            "total_value": 2600000,
            "position_count": 30,
            "as_of": "2026-04-17",
        },
        "top_holdings": [
            {"symbol": "SPY", "value": 800000, "pct": 30.8},
            {"symbol": "BND", "value": 600000, "pct": 23.1},
            {"symbol": "AAPL", "value": 260000, "pct": 10.0},
        ],
        "sector_weights": {
            "Technology": 30.8,
            "Finance": 18.0,
            "Healthcare": 12.0,
            "Consumer": 9.2,
        },
    }


def sample_large_portfolio():
    """200-holding concentrated portfolio for stress testing"""
    holdings = []
    for i in range(200):
        holdings.append(
            {
                "symbol": f"STOCK{i:03d}",
                "value": 13000 / 200,  # $2.6M total
                "pct": 0.5,
            }
        )

    return {
        "summary": {
            "total_value": 2600000,
            "position_count": 200,
            "as_of": "2026-04-17",
        },
        "top_holdings": holdings[:10],
        "sector_weights": {"Diversified": 100.0},
    }


def sample_performance():
    """Performance metrics"""
    return {
        "returns": {
            "1d": 0.002,
            "1w": 0.015,
            "1m": 0.042,
            "3m": 0.105,
            "1y": 0.187,
        },
        "sharpe": 0.72,
        "volatility": 0.12,
        "max_drawdown": -0.15,
    }


def sample_bonds():
    """Bond analysis"""
    return {
        "summary": {
            "total_value": 600000,
            "position_count": 15,
            "avg_ytm": 0.045,
            "avg_duration": 5.2,
        },
        "ladder": [
            {"maturity": "2027", "value": 100000},
            {"maturity": "2029", "value": 150000},
            {"maturity": "2031", "value": 200000},
            {"maturity": "2033", "value": 150000},
        ],
    }


def sample_empty_data():
    """Empty/None data for edge case testing"""
    return None
