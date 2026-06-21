"""Reusable test doubles for the pytdx data-helper tests."""


def raw_bar(day=20, month=6, year=2026, hour=0, minute=0,
            open_price=10.0, high=11.0, low=9.0, close=10.5,
            volume=100.0, amount=1000.0):
    return {
        "year": year, "month": month, "day": day,
        "hour": hour, "minute": minute,
        "open": open_price, "high": high, "low": low, "close": close,
        "vol": volume, "amount": amount,
    }


def normalized_bar(day=20, hour=0, minute=0):
    return {
        "date": 20260600 + day,
        "time": hour * 100 + minute,
        "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
        "volume": 100.0, "turnover": 1000.0,
    }


class RecordingDB:
    def __init__(self):
        self.bar_writes = []
        self.factor_writes = []

    def writeBars(self, bars, period="day"):
        self.bar_writes.append((period, bars))

    def writeFactors(self, factors):
        self.factor_writes.append(factors)


class DirectoryAPI:
    """Small paged TDX security directory with call recording."""

    def __init__(self, pages=None, counts=None, failures=None):
        self.pages = pages or {}
        self.counts = counts or {}
        self.failures = failures or set()
        self.list_calls = []

    def get_security_count(self, market):
        if ("count", market) in self.failures:
            raise OSError("count failed")
        return self.counts.get(market, 0)

    def get_security_list(self, market, start):
        self.list_calls.append((market, start))
        if ("list", market, start) in self.failures:
            raise OSError("list failed")
        return self.pages.get((market, start), [])
