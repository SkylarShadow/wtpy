import csv
import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import ztpy.apps.datahelper.DHTdx2 as dhtdx_module
from tests.datahelper.tdx_fakes import RecordingDB, normalized_bar
from ztpy.apps.datahelper.DHTdx2 import DHTdx, _recent_date_range


class FakeConnectionAPI:
    connect_result = True
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.connect_args = None
        self.closed = False
        type(self).instances.append(self)

    def connect(self, host, port, time_out=5):
        self.connect_args = (host, port, time_out)
        return self.connect_result

    def close(self):
        self.closed = True


def authenticated_helper():
    helper = DHTdx()
    helper.api = MagicMock()
    helper.isAuthed = True
    return helper


class DHTdxAuthenticationTest(unittest.TestCase):

    def setUp(self):
        FakeConnectionAPI.instances = []
        FakeConnectionAPI.connect_result = True

    def test_methods_require_authentication(self):
        helper = DHTdx()
        with self.assertRaises(RuntimeError):
            helper.getCodeList()
        with self.assertRaises(RuntimeError):
            helper.dmpBars([], lambda *args: None)

    @patch.object(dhtdx_module, "TdxHq_API", FakeConnectionAPI)
    def test_auth_with_explicit_server_and_close(self):
        helper = DHTdx()
        helper.auth("127.0.0.1", 7710, time_out=3)
        api = FakeConnectionAPI.instances[-1]
        self.assertTrue(helper.isAuthed)
        self.assertEqual(("127.0.0.1", 7710, 3), api.connect_args)
        self.assertTrue(api.kwargs["heartbeat"])
        self.assertTrue(api.kwargs["auto_retry"])

        helper.close()
        self.assertTrue(api.closed)
        self.assertFalse(helper.isAuthed)
        self.assertIsNone(helper.api)

    @patch.object(dhtdx_module, "search_best_tdx")
    @patch.object(dhtdx_module, "TdxHq_API", FakeConnectionAPI)
    def test_auth_selects_best_server(self, search):
        search.return_value = [
            (True, 0.01, "1.2.3.4", 7709),
            (True, 0.02, "2.3.4.5", 7710),
        ]
        helper = DHTdx()
        helper.auth()
        self.assertEqual("1.2.3.4", helper.host)
        self.assertEqual(7709, helper.port)
        self.assertEqual([("1.2.3.4", 7709), ("2.3.4.5", 7710)], helper.hosts)

    @patch.object(dhtdx_module, "search_best_tdx", return_value=[])
    def test_auth_fails_when_no_server_exists(self, _):
        with self.assertRaises(ConnectionError):
            DHTdx().auth()

    @patch.object(dhtdx_module, "TdxHq_API", FakeConnectionAPI)
    def test_failed_connection_closes_api(self):
        FakeConnectionAPI.connect_result = False
        with self.assertRaises(ConnectionError):
            DHTdx().auth("127.0.0.1")
        self.assertTrue(FakeConnectionAPI.instances[-1].closed)

    def test_auth_is_idempotent(self):
        helper = authenticated_helper()
        old_api = helper.api
        helper.auth("127.0.0.1")
        self.assertIs(old_api, helper.api)


class DHTdxCodeAndBarTest(unittest.TestCase):

    def setUp(self):
        self.helper = authenticated_helper()

    def test_get_code_list_forwards_filters(self):
        self.helper.catalog.codes = MagicMock(return_value=["SSE.600000"])
        result = self.helper.getCodeList(
            hasIndex=True, hasStock=False, products="ETF", force=True,
        )
        self.assertEqual(["SSE.600000"], result)
        self.helper.catalog.codes.assert_called_once_with(
            api=self.helper.api, has_index=True, has_stock=False,
            products="ETF", force=True,
        )

    def test_dump_code_list_json(self):
        self.helper.catalog.securities = MagicMock(return_value=[
            {"exchg": "SSE", "code": "600000", "name": "浦发银行",
             "product": "STK"},
            {"exchg": "SZSE", "code": "399001", "name": "深证成指",
             "product": "IDX"},
        ])
        with tempfile.TemporaryDirectory() as folder:
            filename = os.path.join(folder, "nested", "stocks.json")
            self.helper.dmpCodeListToFile(filename, hasIndex=True, hasStock=True)
            with open(filename, "r", encoding="utf-8") as stream:
                payload = json.load(stream)
        self.assertEqual("浦发银行", payload["SSE"]["600000"]["name"])
        self.assertEqual("IDX", payload["SZSE"]["399001"]["product"])

    @patch.object(dhtdx_module, "fetch_bars")
    def test_load_bars_parses_dates_and_routes_index(self, fetch):
        self.helper.catalog.resolve_is_index = MagicMock(return_value=True)
        fetch.return_value = ([normalized_bar()], True)
        exchange, code, bars, is_day = self.helper._load_bars(
            "SSE.000001", "day", date(2026, 6, 1), 20260630,
        )
        self.assertEqual("SSE", exchange)
        self.assertEqual("000001", code)
        self.assertTrue(is_day)
        self.assertEqual(1, len(bars))
        fetch.assert_called_once_with(
            self.helper.api, 1, "000001", "day", 20260601, 20260630,
            is_index=True,
        )

    def test_dump_day_bars_to_csv(self):
        self.helper._load_bars = MagicMock(return_value=(
            "SSE", "600000", [normalized_bar()], True,
        ))
        progress = []
        with tempfile.TemporaryDirectory() as folder:
            self.helper.dmpBarsToFile(
                folder, ["SSE.600000"], period="day",
                progress=lambda current, total: progress.append((current, total)),
            )
            filename = os.path.join(folder, "SSE.600000_d.csv")
            with open(filename, "r", encoding="utf-8", newline="") as stream:
                rows = list(csv.reader(stream))
        self.assertEqual("time", rows[0][1])
        self.assertEqual("0", rows[1][1])
        self.assertEqual([(0, 1)], progress)

    def test_dump_minute_bars_to_csv(self):
        self.helper._load_bars = MagicMock(return_value=(
            "SZSE", "000001", [normalized_bar(hour=9, minute=35)], False,
        ))
        with tempfile.TemporaryDirectory() as folder:
            self.helper.dmpBarsToFile(folder, ["SZSE.000001"], period="min5")
            with open(os.path.join(folder, "SZSE.000001_m5.csv"),
                      "r", encoding="utf-8", newline="") as stream:
                rows = list(csv.reader(stream))
        self.assertEqual("09:35:00", rows[1][1])

    def test_dump_bars_to_db_encodes_time_and_skips_empty(self):
        self.helper._load_bars = MagicMock(side_effect=[
            ("SSE", "600000", [normalized_bar(hour=9, minute=35)], False),
            ("SSE", "600001", [], False),
        ])
        db = RecordingDB()
        self.helper.dmpBarsToDB(
            db, ["SSE.600000", "SSE.600001"], period="min5",
        )
        self.assertEqual(1, len(db.bar_writes))
        period, rows = db.bar_writes[0]
        self.assertEqual("min5", period)
        self.assertEqual((20260620 - 19900000) * 10000 + 935, rows[0]["time"])

    def test_dump_bars_callback_receives_c_buffer(self):
        self.helper._load_bars = MagicMock(return_value=(
            "SSE", "600000", [normalized_bar()], True,
        ))
        calls = []
        self.helper.dmpBars(["SSE.600000"], lambda *args: calls.append(args))
        exchange, code, buffer, count, period = calls[0]
        self.assertEqual(("SSE", "600000", 1, "day"),
                         (exchange, code, count, period))
        self.assertEqual(10.5, buffer[0].close)

    def test_invalid_period_is_rejected_before_loading(self):
        with self.assertRaises(ValueError):
            self.helper.dmpBars([], lambda *args: None, period="tick")


class DHTdxBulkOutputTest(unittest.TestCase):

    def setUp(self):
        self.helper = authenticated_helper()
        self.helper.getCodeList = MagicMock(return_value=["SSE.600000"])

        def load(code, period, start, end):
            is_day = period == "day"
            bar = normalized_bar() if is_day else normalized_bar(hour=9, minute=35)
            return "SSE", "600000", [bar], is_day

        self.helper._load_bars = MagicMock(side_effect=load)

    def test_dump_all_bars_to_db_for_multiple_periods(self):
        db = RecordingDB()
        progress = []
        report = self.helper.dmpAllBarsToDB(
            db, periods=("day", "min5"), hasIndex=True,
            products=("STK", "ETF"),
            progress=lambda current, total: progress.append((current, total)),
            workers=1,
        )
        self.assertEqual(2, report["succeeded"])
        self.assertEqual(2, report["bars"])
        self.assertEqual(["day", "min5"], [item[0] for item in db.bar_writes])
        self.assertEqual((2, 2), progress[-1])
        self.helper.getCodeList.assert_called_once_with(
            hasIndex=True, hasStock=True, products=("STK", "ETF"),
        )

    def test_dump_all_bars_to_files(self):
        with tempfile.TemporaryDirectory() as folder:
            report = self.helper.dmpAllBarsToFile(
                folder, periods=("day", "min5"), workers=1,
            )
            files = sorted(os.listdir(folder))
        self.assertEqual(["SSE.600000_d.csv", "SSE.600000_m5.csv"], files)
        self.assertEqual(2, report["succeeded"])

    def test_dump_all_bars_callback(self):
        calls = []
        report = self.helper.dmpAllBars(
            lambda *args: calls.append(args), periods="day", workers=1,
        )
        self.assertEqual(1, report["succeeded"])
        self.assertEqual(1, calls[0][3])
        self.assertEqual("day", calls[0][4])

    def test_bulk_rejects_unknown_period_before_catalog_access(self):
        with self.assertRaises(ValueError):
            self.helper.dmpAllBars(
                lambda *args: None, periods=("tick",), workers=1,
            )
        self.helper.getCodeList.assert_not_called()

    @patch.object(dhtdx_module, "_TdxBarLoader")
    def test_worker_factory_round_robins_available_hosts(self, loader_class):
        self.helper.hosts = [("fast", 7709), ("backup", 7710)]
        self.helper.worker_timeout = 3
        self.helper.catalog.types = {(1, "600000"): "STK"}
        factory = self.helper._bar_loader_factory(20260101, 20260630)

        factory()
        factory()
        factory()

        hosts = [(call.args[0], call.args[1]) for call in loader_class.call_args_list]
        self.assertEqual([("fast", 7709), ("backup", 7710), ("fast", 7709)], hosts)
        self.assertTrue(all(call.args[2] == 3 for call in loader_class.call_args_list))

    def test_bulk_rejects_invalid_workers_before_catalog_access(self):
        with self.assertRaises(ValueError):
            self.helper.dmpAllBars(lambda *args: None, workers=0)
        self.helper.getCodeList.assert_not_called()


class DHTdxFactorsAndIntradayTest(unittest.TestCase):

    def setUp(self):
        self.helper = authenticated_helper()

    @patch.object(dhtdx_module, "adjust_factors")
    def test_dump_adjustment_factors_to_file_and_db(self, adjust):
        adjust.return_value = [{"date": 19900101, "factor": 1.0}]
        codes = ["SSE.600000", "SZSE.000001"]
        with tempfile.TemporaryDirectory() as folder:
            filename = os.path.join(folder, "factors.json")
            self.helper.dmpAdjFactorsToFile(codes, filename)
            with open(filename, "r", encoding="utf-8") as stream:
                payload = json.load(stream)
        self.assertIn("600000", payload["SSE"])
        self.assertIn("000001", payload["SZSE"])

        db = RecordingDB()
        self.helper.dmpAdjFactorsToDB(db, ["SSE.600000"])
        self.assertEqual(1, len(db.factor_writes))

    @patch.object(dhtdx_module, "fetch_day_transactions")
    @patch.object(dhtdx_module, "fetch_day_minute_time")
    def test_single_day_intraday_accessors(self, minute, transactions):
        transactions.return_value = [{"price": 10}]
        minute.return_value = [{"price": 11}]
        self.assertEqual([{"price": 10}],
                         self.helper.getTransactions("SSE.600000", date(2026, 6, 19)))
        self.assertEqual([{"price": 11}],
                         self.helper.getMinuteTime("SZSE.000001", 20260619))
        transactions.assert_called_once_with(self.helper.api, 1, "600000", 20260619)
        minute.assert_called_once_with(self.helper.api, 0, "000001", 20260619)

    @patch.object(dhtdx_module, "fetch_transactions")
    def test_dump_transactions_csv(self, fetch):
        fetch.return_value = [{
            "date": 20260619, "time": 93002, "datetime": 20260619093002,
            "price": 10.0, "volume": 2.0, "side": 1, "index": 0,
        }]
        with tempfile.TemporaryDirectory() as folder:
            self.helper.dmpTransactionsToFile(
                folder, ["SSE.600000"], 20260619, 20260619,
            )
            with open(os.path.join(folder, "SSE.600000_trans.csv"),
                      "r", encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
        self.assertEqual("10.0", rows[0]["price"])

    @patch.object(dhtdx_module, "fetch_minute_time")
    def test_dump_minute_time_csv(self, fetch):
        fetch.return_value = [{
            "date": 20260619, "time": 930, "datetime": 202606190930,
            "price": 10.0, "volume": 2.0,
        }]
        with tempfile.TemporaryDirectory() as folder:
            self.helper.dmpMinuteTimeToFile(
                folder, ["SZSE.000001"], 20260619, 20260619,
            )
            with open(os.path.join(folder, "SZSE.000001_time.csv"),
                      "r", encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
        self.assertEqual("930", rows[0]["time"])

    def test_recent_date_range_uses_default_days(self):
        self.assertEqual((20260601, 20260630),
                         _recent_date_range(None, 20260630, 30))
        self.assertEqual((20260610, 20260630),
                         _recent_date_range(20260610, 20260630, 30))


@unittest.skipUnless(os.environ.get("ZTPY_RUN_TDX_LIVE_TESTS") == "1",
                     "set ZTPY_RUN_TDX_LIVE_TESTS=1 to run live TDX checks")
class DHTdxLiveIntegrationTest(unittest.TestCase):
    """Optional smoke test against a real TDX server.

    ZTPY_TDX_HOST and ZTPY_TDX_PORT may select a server. Without a host the
    helper performs its normal best-server discovery.
    """

    def test_download_recent_daily_bars(self):
        helper = DHTdx()
        host = os.environ.get("ZTPY_TDX_HOST")
        port = int(os.environ.get("ZTPY_TDX_PORT", "7709"))
        try:
            if host:
                helper.auth(host=host, port=port)
            else:
                helper.auth()
            exchange, code, bars, is_day = helper._load_bars(
                "SSE.600000", "day", 20260101, 20261231,
            )
            self.assertEqual(("SSE", "600000", True), (exchange, code, is_day))
            self.assertTrue(bars)
            self.assertTrue(all(20260101 <= bar["date"] <= 20261231 for bar in bars))
        finally:
            helper.close()


if __name__ == "__main__":
    unittest.main()
