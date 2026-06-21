import unittest
from unittest.mock import patch

from pytdx.params import TDXParams

from tests.datahelper.tdx_fakes import normalized_bar, raw_bar
from ztpy.apps.datahelper.data import tdx_history


class RetryAPI:
    def __init__(self):
        self.calls = 0

    def request(self, value):
        self.calls += 1
        if self.calls == 1:
            raise OSError("temporary")
        return value


class BarsAPI:
    def __init__(self, security_pages=None, index_pages=None):
        self.security_pages = security_pages or {}
        self.index_pages = index_pages or {}
        self.calls = []

    def get_security_bars(self, ktype, market, code, offset, count):
        self.calls.append(("security", market, code, offset, count))
        return self.security_pages.get(offset, [])

    def get_index_bars(self, ktype, market, code, offset, count):
        self.calls.append(("index", market, code, offset, count))
        return self.index_pages.get(offset, [])


class TdxHistoryBarTest(unittest.TestCase):

    def test_request_retries_once(self):
        api = RetryAPI()
        self.assertEqual("ok", tdx_history._request(api, "request", "ok"))
        self.assertEqual(2, api.calls)

    def test_request_returns_none_after_two_failures(self):
        class API:
            def request(self):
                raise OSError("offline")

        self.assertIsNone(tdx_history._request(API(), "request"))

    def test_parse_bar_converts_day_and_minute(self):
        day = tdx_history.parse_bar(raw_bar(), True)
        minute = tdx_history.parse_bar(raw_bar(hour=9, minute=35), False)
        self.assertEqual(20260620, day["date"])
        self.assertEqual(0, day["time"])
        self.assertEqual(935, minute["time"])
        self.assertEqual(100.0, minute["volume"])

    def test_parse_bar_rejects_malformed_and_invalid_values(self):
        invalid = (
            {},
            raw_bar(low=0),
            raw_bar(open_price=12),
            raw_bar(close=12),
            raw_bar(volume=-1),
            raw_bar(amount=-1),
        )
        for item in invalid:
            with self.subTest(item=item):
                self.assertIsNone(tdx_history.parse_bar(item, True))

    def test_fetch_bars_pages_backwards_filters_deduplicates_and_sorts(self):
        api = BarsAPI(security_pages={
            0: [raw_bar(day=20), raw_bar(day=19)],
            2: [raw_bar(day=19, close=10.8), raw_bar(day=18)],
            4: [raw_bar(day=17), raw_bar(day=16)],
        })
        with patch.object(tdx_history, "PAGE_SIZE", 2), \
                patch.object(tdx_history, "MAX_PAGES", 5):
            bars, is_day = tdx_history.fetch_bars(
                api, TDXParams.MARKET_SH, "600000", "day",
                20260618, 20260619, is_index=False,
            )

        self.assertTrue(is_day)
        self.assertEqual([20260618, 20260619], [bar["date"] for bar in bars])
        self.assertEqual(10.8, bars[1]["close"], "older page replaces duplicate key")
        self.assertEqual([0, 2, 4], [call[3] for call in api.calls])

    def test_fetch_bars_uses_index_api_and_falls_back_on_empty_first_page(self):
        api = BarsAPI(security_pages={0: [raw_bar(day=20)]}, index_pages={0: []})
        bars, _ = tdx_history.fetch_bars(
            api, TDXParams.MARKET_SH, "000001", "day",
            20260601, 20260630, is_index=True,
        )
        self.assertEqual(1, len(bars))
        self.assertEqual("index", api.calls[0][0])
        self.assertEqual("security", api.calls[1][0])

    def test_fetch_bars_stops_on_short_page(self):
        api = BarsAPI(security_pages={0: [raw_bar()]})
        bars, _ = tdx_history.fetch_bars(
            api, TDXParams.MARKET_SH, "600000", "day",
            20260601, 20260630, is_index=False,
        )
        self.assertEqual(1, len(bars))
        self.assertEqual(1, len(api.calls))

    def test_encoded_time_and_c_buffer(self):
        bar = normalized_bar(hour=9, minute=35)
        self.assertEqual(0, tdx_history.encoded_bar_time(bar, True))
        self.assertEqual((20260620 - 19900000) * 10000 + 935,
                         tdx_history.encoded_bar_time(bar, False))
        buffer = tdx_history.bars_to_c_buffer([bar], False)
        self.assertEqual(1, len(buffer))
        self.assertEqual(20260620, buffer[0].date)
        self.assertEqual(10.5, buffer[0].close)
        self.assertEqual(100.0, buffer[0].vol)
        self.assertEqual(0.0, buffer[0].settle)


class TdxAdjustmentFactorTest(unittest.TestCase):

    def test_returns_neutral_factor_when_events_are_missing_or_request_fails(self):
        class EmptyAPI:
            def get_xdxr_info(self, market, code):
                return []

        class BrokenAPI:
            def get_xdxr_info(self, market, code):
                raise OSError("failed")

        expected = [{"date": 19900101, "factor": 1.0}]
        self.assertEqual(expected, tdx_history.adjust_factors(EmptyAPI(), 1, "600000"))
        self.assertEqual(expected, tdx_history.adjust_factors(BrokenAPI(), 1, "600000"))

    @patch.object(tdx_history, "fetch_bars")
    def test_calculates_cumulative_factor_and_ignores_bad_events(self, fetch):
        fetch.return_value = ([
            {"date": 20260618, "close": 10.0},
            {"date": 20260620, "close": 9.0},
        ], True)

        class API:
            def get_xdxr_info(self, market, code):
                return [
                    {"category": 2, "year": 2026, "month": 6, "day": 19},
                    {"category": 1, "year": "bad", "month": 6, "day": 19},
                    {"category": 1, "year": 2026, "month": 6, "day": 19,
                     "songzhuangu": 1, "peigu": 0, "peigujia": 0, "fenhong": 1},
                ]

        factors = tdx_history.adjust_factors(API(), TDXParams.MARKET_SH, "600000")
        self.assertEqual(19900101, factors[0]["date"])
        self.assertEqual(20260619, factors[1]["date"])
        self.assertAlmostEqual(1.1111111111, factors[1]["factor"])
        fetch.assert_called_once()


class TdxIntradayHistoryTest(unittest.TestCase):

    def test_trading_dates_excludes_weekend(self):
        self.assertEqual([20260619, 20260622],
                         tdx_history.trading_dates(20260619, 20260622))

    def test_transactions_restore_old_to_new_order_and_synthesize_seconds(self):
        class API:
            def get_history_transaction_data(self, market, code, offset, count, day):
                if offset == 0:
                    return [
                        {"time": "09:31", "price": 10.2, "vol": 3, "buyorsell": 1},
                        {"time": "09:31", "price": 10.3, "vol": 4, "buyorsell": 0},
                    ]
                if offset == 2:
                    return [{"time": "09:30", "price": 10.0, "vol": 2,
                             "buyorsell": 2}]
                return []

        with patch.object(tdx_history, "TRANS_PAGE_SIZE", 2):
            records = tdx_history.fetch_day_transactions(
                API(), TDXParams.MARKET_SH, "600000", 20260619,
            )
        self.assertEqual([93002, 93102, 93105], [item["time"] for item in records])
        self.assertEqual([0, 1, 2], [item["index"] for item in records])
        self.assertEqual(20260619093002, records[0]["datetime"])

    def test_transactions_skip_invalid_and_overflowed_records(self):
        rows = [{"time": "09:30", "price": 10, "vol": 1}] * 21
        rows.extend([
            {"time": "bad", "price": 10, "vol": 1},
            {"time": "09:31", "price": 0, "vol": 1},
            {"time": "09:32", "price": 10, "vol": -1},
        ])

        class API:
            def get_history_transaction_data(self, *args):
                return rows

        records = tdx_history.fetch_day_transactions(API(), 1, "600000", 20260619)
        self.assertEqual(20, len(records))

    @patch.object(tdx_history, "fetch_day_transactions")
    def test_fetch_transactions_aggregates_weekdays(self, fetch_day):
        fetch_day.side_effect = lambda api, market, code, day: [{"date": day}]
        records = tdx_history.fetch_transactions(None, 1, "600000", 20260619, 20260622)
        self.assertEqual([20260619, 20260622], [item["date"] for item in records])

    def test_minute_labels_cover_240_points(self):
        self.assertEqual(240, len(tdx_history.MINUTE_LABELS))
        self.assertEqual(930, tdx_history.MINUTE_LABELS[0])
        self.assertEqual(1459, tdx_history.MINUTE_LABELS[-1])

    def test_minute_time_rejects_incomplete_day(self):
        class API:
            def get_history_minute_time_data(self, market, code, day):
                return [{"price": 10, "vol": 1}] * 239

        self.assertEqual([], tdx_history.fetch_day_minute_time(API(), 1, "600000", 20260619))

    def test_minute_time_converts_valid_rows_and_skips_bad_rows(self):
        rows = [{"price": 10.0, "vol": 1.0} for _ in range(240)]
        rows[1] = {"price": 0, "vol": 1}
        rows[2] = {"price": "bad", "vol": 1}

        class API:
            def get_history_minute_time_data(self, market, code, day):
                return rows

        records = tdx_history.fetch_day_minute_time(API(), 1, "600000", 20260619)
        self.assertEqual(238, len(records))
        self.assertEqual(930, records[0]["time"])
        self.assertEqual(202606190930, records[0]["datetime"])

    @patch.object(tdx_history, "fetch_day_minute_time")
    def test_fetch_minute_time_aggregates_weekdays(self, fetch_day):
        fetch_day.side_effect = lambda api, market, code, day: [{"date": day}]
        records = tdx_history.fetch_minute_time(None, 1, "600000", 20260619, 20260622)
        self.assertEqual([20260619, 20260622], [item["date"] for item in records])


if __name__ == "__main__":
    unittest.main()
