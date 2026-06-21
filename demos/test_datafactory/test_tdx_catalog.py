import unittest
from datetime import date, datetime
from unittest.mock import patch

import pandas as pd
from pytdx.params import TDXParams

from tests.datahelper.tdx_fakes import DirectoryAPI
from ztpy.apps.datahelper.data.tdx_catalog import (
    TdxSecurityCatalog,
    date_range,
    is_index,
    normalize_date,
    parse_std_code,
    period_spec,
    security_product,
)


class TdxCatalogConversionTest(unittest.TestCase):

    def test_period_spec_supports_case_and_rejects_unknown_period(self):
        self.assertEqual("d", period_spec("DAY")[1])
        self.assertTrue(period_spec("day")[2])
        self.assertFalse(period_spec("min1")[2])
        with self.assertRaises(ValueError):
            period_spec("tick")
        with self.assertRaises(ValueError):
            period_spec(None)

    def test_parse_standard_code_and_aliases(self):
        self.assertEqual(("SSE", TDXParams.MARKET_SH, "600000"),
                         parse_std_code("sse.600000"))
        self.assertEqual(("SSE", TDXParams.MARKET_SH, "600000"),
                         parse_std_code("SH.600000"))
        self.assertEqual(("SZSE", TDXParams.MARKET_SZ, "000001"),
                         parse_std_code("sz.000001"))

    def test_parse_standard_code_rejects_bad_values(self):
        for value in ("600000", "SSE.", "SSE.60000", "SSE.A00001"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_std_code(value)
        with self.assertRaises(NotImplementedError):
            parse_std_code("BJ.920001")

    def test_normalize_date_and_range(self):
        self.assertEqual(20260621, normalize_date(date(2026, 6, 21)))
        self.assertEqual(20260621, normalize_date(datetime(2026, 6, 21, 9, 30)))
        self.assertEqual(20260621, normalize_date(20260621))
        self.assertEqual((19900101, 20260621), date_range(None, 20260621))
        with self.assertRaises(TypeError):
            normalize_date("2026-06-21")
        with self.assertRaises(TypeError):
            normalize_date(2026)
        with self.assertRaises(ValueError):
            date_range(20260622, 20260621)

    def test_index_detection_prefers_exact_metadata(self):
        exact = {(TDXParams.MARKET_SH, "000001"): "STK",
                 (TDXParams.MARKET_SH, "600000"): "IDX"}
        self.assertFalse(is_index(TDXParams.MARKET_SH, "000001", exact))
        self.assertTrue(is_index(TDXParams.MARKET_SH, "600000", exact))
        self.assertTrue(is_index(TDXParams.MARKET_SH, "000001"))
        self.assertTrue(is_index(TDXParams.MARKET_SZ, "399001"))
        self.assertFalse(is_index(TDXParams.MARKET_SZ, "000001"))

    def test_product_detection(self):
        cases = (
            (TDXParams.MARKET_SH, "600000", "STK"),
            (TDXParams.MARKET_SH, "688001", "STK"),
            (TDXParams.MARKET_SH, "510300", "ETF"),
            (TDXParams.MARKET_SZ, "000001", "STK"),
            (TDXParams.MARKET_SZ, "300001", "STK"),
            (TDXParams.MARKET_SZ, "159915", "ETF"),
            (TDXParams.MARKET_SZ, "399001", None),
        )
        for market, code, expected in cases:
            with self.subTest(code=code):
                self.assertEqual(expected, security_product(market, code))


class TdxSecurityCatalogTest(unittest.TestCase):

    def test_loads_paged_live_directory_and_tolerates_one_market_failure(self):
        api = DirectoryAPI(
            counts={TDXParams.MARKET_SH: 3, TDXParams.MARKET_SZ: 1},
            pages={
                (TDXParams.MARKET_SH, 0): [
                    {"code": "600000", "name": "A"},
                    {"code": "600001", "name": "B"},
                ],
                (TDXParams.MARKET_SH, 2): [{"code": "600002", "name": "C"}],
            },
            failures={("list", TDXParams.MARKET_SZ, 0)},
        )
        rows = TdxSecurityCatalog._load_tdx_securities(api)
        self.assertEqual(3, len(rows))
        self.assertEqual([(TDXParams.MARKET_SH, 0), (TDXParams.MARKET_SH, 2),
                          (TDXParams.MARKET_SZ, 0)], api.list_calls)

    @patch("ztpy.apps.datahelper.data.common.get_index_code_name_list")
    @patch("ztpy.apps.datahelper.data.common.get_stk_code_name_list")
    def test_refresh_merges_exact_lists_and_live_directory(self, stock_list, index_list):
        stock_list.side_effect = lambda market: (
            [{"code": "600000", "name": "浦发银行"}]
            if market == "SH" else [{"code": "000001", "name": "平安银行"}]
        )
        index_list.return_value = [
            {"market_code": "SH000001", "name": "上证指数"},
            {"market_code": "BAD", "name": "invalid"},
        ]
        api = DirectoryAPI(
            counts={TDXParams.MARKET_SH: 3, TDXParams.MARKET_SZ: 2},
            pages={
                (TDXParams.MARKET_SH, 0): [
                    {"code": "600000", "name": "TDX name"},
                    {"code": "000001", "name": "上证指数"},
                    {"code": "510300", "name": "300ETF"},
                ],
                (TDXParams.MARKET_SZ, 0): [
                    {"code": "000001", "name": "平安银行"},
                    {"code": "399001", "name": "深证成指"},
                ],
            },
        )
        catalog = TdxSecurityCatalog()
        catalog.refresh(api=api)

        self.assertEqual("STK", catalog.types[(TDXParams.MARKET_SH, "600000")])
        self.assertEqual("IDX", catalog.types[(TDXParams.MARKET_SH, "000001")])
        self.assertEqual("ETF", catalog.types[(TDXParams.MARKET_SH, "510300")])
        self.assertEqual("IDX", catalog.types[(TDXParams.MARKET_SZ, "399001")])
        self.assertEqual("TDX name", catalog.names[(TDXParams.MARKET_SH, "600000")])
        self.assertEqual(date.today(), catalog.cache_date)
        self.assertEqual(date.today(), catalog.live_cache_date)

        calls = list(api.list_calls)
        catalog.refresh(api=api)
        self.assertEqual(calls, api.list_calls, "daily live cache should prevent refetch")

    def test_filtering_sorting_and_standard_codes(self):
        catalog = TdxSecurityCatalog()
        catalog.types = {
            (TDXParams.MARKET_SZ, "159915"): "ETF",
            (TDXParams.MARKET_SH, "600000"): "STK",
            (TDXParams.MARKET_SH, "000001"): "IDX",
        }
        catalog.names = {(TDXParams.MARKET_SH, "600000"): "浦发银行"}
        catalog.cache_date = date.today()

        self.assertEqual(["SSE.600000"], catalog.codes())
        self.assertEqual(["SSE.000001", "SSE.600000"],
                         catalog.codes(has_index=True))
        self.assertEqual(["SSE.600000", "SZSE.159915"],
                         catalog.codes(products=None))
        self.assertEqual(["SZSE.159915"], catalog.codes(products="ETF"))
        indexes = catalog.securities(has_index=True, has_stock=False)
        self.assertEqual(["000001"], [item["code"] for item in indexes])

    def test_resolve_index_uses_cached_exact_type(self):
        catalog = TdxSecurityCatalog()
        catalog.types[(TDXParams.MARKET_SH, "000001")] = "STK"
        catalog.cache_date = date.today()
        self.assertFalse(catalog.resolve_is_index(TDXParams.MARKET_SH, "000001"))


class StockReferenceListRegressionTest(unittest.TestCase):

    @patch("ztpy.apps.datahelper.data.common.zt_info")
    @patch("ztpy.apps.datahelper.data.common.ak.stock_info_sz_name_code")
    def test_shenzhen_list_uses_logger_without_console_encoding_dependency(
            self, get_list, log):
        from ztpy.apps.datahelper.data.common import MARKET, get_stk_code_name_list

        get_list.side_effect = [
            pd.DataFrame({"A股代码": ["000001"], "A股简称": ["平安银行"]}),
            pd.DataFrame({"A股代码": ["200001"], "A股简称": ["深证B"]}),
        ]
        result = get_stk_code_name_list(MARKET.SZ)

        self.assertEqual(["000001", "200001"], [item["code"] for item in result])
        log.assert_called_once_with("获取深圳证券交易所股票数量: {}", 2)


if __name__ == "__main__":
    unittest.main()
