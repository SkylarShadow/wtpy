"""基于 pytdx 的 ztpy 数据助手，提供证券目录及历史行情导入能力。"""

import csv
import json
import os
from datetime import date, timedelta
from threading import Lock

from pytdx.hq import TdxHq_API

from ztpy.apps.datahelper.DHDefs import BaseDataHelper, DBHelper
from ztpy.apps.datahelper.data.common_pytdx import search_best_tdx
from ztpy.apps.datahelper.data.tdx_catalog import (
    PERIODS, TdxSecurityCatalog, date_range, is_index,
    normalize_date, parse_std_code, period_spec, security_product,
)
from ztpy.apps.datahelper.data.tdx_bulk import import_all_bars
from ztpy.apps.datahelper.data.tdx_history import (
    adjust_factors, bars_to_c_buffer, encoded_bar_time, fetch_bars,
    fetch_day_minute_time, fetch_day_transactions, fetch_minute_time,
    fetch_transactions,
)
from ztpy.utils.ztlog import zt_info


# 兼容旧版 DHTdx.py 曾暴露的私有名称，避免已有调用代码立即失效。
_PERIODS = PERIODS
_period_spec = period_spec
_parse_std_code = parse_std_code
_is_index = is_index
_security_product = security_product
_date_range = date_range
_fetch_bars = fetch_bars
_adjust_factors = adjust_factors
_encoded_time = encoded_bar_time
_to_c_buffer = bars_to_c_buffer


def _notify_progress(progress, current, total):
    if progress is not None:
        progress(current, total)


def _recent_date_range(start_date, end_date, default_days):
    """补齐逐笔/分时接口的默认日期范围，并统一为 YYYYMMDD 整数。"""
    end = normalize_date(end_date, date.today())
    if start_date is not None:
        return date_range(start_date, end)
    end_value = date(end // 10000, end // 100 % 100, end % 100)
    start_value = end_value - timedelta(days=default_days - 1)
    return normalize_date(start_value), end


class _TdxBarLoader:
    """并发工作线程独占的 TDX 连接与 K 线加载器。"""

    def __init__(self, host, port, timeout, security_types, start_date, end_date):
        # 与 Hikyuu 的导入任务一致，工作连接不启用心跳和内部四轮重试。
        # 网络异常直接抛给批量调度器，避免坏节点长时间占住工作线程。
        self.api = TdxHq_API(raise_exception=True)
        try:
            connected = self.api.connect(host, int(port), time_out=timeout)
        except Exception:
            self.api.close()
            raise
        if not connected:
            self.api.close()
            raise ConnectionError(
                "Failed to connect TDX bulk worker to {}:{}".format(host, port)
            )
        self.security_types = security_types
        self.start_date = start_date
        self.end_date = end_date

    def __call__(self, std_code, period):
        exchange, market, code = parse_std_code(std_code)
        bars, is_day = fetch_bars(
            self.api, market, code, period, self.start_date, self.end_date,
            is_index=is_index(market, code, self.security_types),
        )
        return exchange, code, bars, is_day

    def close(self):
        self.api.close()


class DHTdx(BaseDataHelper):
    """下载沪深证券目录、K 线、复权因子、逐笔及分时历史数据。"""

    def __init__(self):
        super().__init__()
        self.api = None
        self.host = None
        self.port = None
        self.hosts = []
        self.worker_timeout = 5
        self.catalog = TdxSecurityCatalog()
        zt_info("Tdx helper has been created.")

    @property
    def _security_types(self):
        return self.catalog.types

    @_security_types.setter
    def _security_types(self, value):
        self.catalog.types = value

    @property
    def _security_names(self):
        return self.catalog.names

    @_security_names.setter
    def _security_names(self, value):
        self.catalog.names = value

    @property
    def _security_cache_date(self):
        return self.catalog.cache_date

    @_security_cache_date.setter
    def _security_cache_date(self, value):
        self.catalog.cache_date = value

    def __check__(self):
        if not self.isAuthed or self.api is None:
            raise RuntimeError("Tdx helper has not been authorized")

    def auth(self, host=None, port=None, **kwargs):
        """连接指定 TDX 服务器；未指定服务器时自动选择当前最优节点。"""
        if self.isAuthed:
            return
        if host is None:
            zt_info("Searching for an available TDX server...")
            servers = search_best_tdx()
            if not servers:
                raise ConnectionError("No available TDX quote server was found")
            self.hosts = [(item[2], int(item[3])) for item in servers]
            host, port = self.hosts[0]
        elif port is None:
            port = 7709
            self.hosts = [(host, int(port))]
        else:
            self.hosts = [(host, int(port))]

        api = TdxHq_API(heartbeat=True, auto_retry=True)
        timeout = kwargs.pop("time_out", 5)
        if not api.connect(host, int(port), time_out=timeout):
            api.close()
            raise ConnectionError("Failed to connect to {}:{}".format(host, port))
        self.api, self.host, self.port = api, host, int(port)
        self.worker_timeout = timeout
        self.isAuthed = True
        zt_info("Connected to TDX server {}:{}", self.host, self.port)

    def close(self):
        if self.api is not None:
            self.api.close()
        self.api = None
        self.isAuthed = False

    def _refresh_security_types(self, force=False):
        self.catalog.refresh(force)

    def _resolve_is_index(self, market, code):
        return self.catalog.resolve_is_index(market, code)

    def _load_bars(self, std_code, period, start_date, end_date):
        """解析 ztpy 标准代码，并按证券类型选择正确的 pytdx K 线接口。"""
        exchange, market, code = parse_std_code(std_code)
        start, end = date_range(start_date, end_date)
        bars, is_day = fetch_bars(
            self.api, market, code, period, start, end,
            is_index=self._resolve_is_index(market, code),
        )
        return exchange, code, bars, is_day

    def _bar_loader_factory(self, start_date, end_date):
        """创建线程安全的轮询工厂，每个调用者获得独立 TDX 长连接。"""
        hosts = tuple(self.hosts or ((self.host, self.port),))
        security_types = dict(self.catalog.types)
        state = {"index": 0}
        lock = Lock()

        def create_loader():
            with lock:
                host, port = hosts[state["index"] % len(hosts)]
                state["index"] += 1
            return _TdxBarLoader(
                host, port, self.worker_timeout, security_types,
                start_date, end_date,
            )

        return create_loader

    # 兼容早期 DHTdx2 中直接调用 _load 的代码。
    _load = _load_bars

    def getCodeList(self, hasIndex=False, hasStock=True, products=("STK",),
                    force=False):
        """返回 TDX 目录中的 ztpy 标准代码。

        products 用于限定产品类型，常用值为 STK、ETF；hasIndex=True 时会
        自动追加 IDX。force=True 会忽略当天缓存并重新同步证券目录。
        """
        self.__check__()
        return self.catalog.codes(
            api=self.api, has_index=hasIndex, has_stock=hasStock,
            products=products, force=force,
        )

    def dmpCodeListToFile(self, filename, hasIndex=True, hasStock=True):
        """按 ztpy 代码表格式将沪深证券元数据写入 JSON 文件。"""
        self.__check__()
        securities = {"SSE": {}, "SZSE": {}}
        for item in self.catalog.securities(
                api=self.api, has_index=hasIndex, has_stock=hasStock):
            securities[item["exchg"]][item["code"]] = item

        os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)
        with open(filename, "w", encoding="utf-8") as stream:
            json.dump(securities, stream, sort_keys=True, indent=4, ensure_ascii=False)

    def dmpBarsToFile(self, folder, codes, start_date=None, end_date=None,
                      period="day", progress=None):
        """将指定代码列表的单一周期 K 线分别写入 CSV 文件。"""
        self.__check__()
        _, filetag, _ = period_spec(period)
        os.makedirs(folder, exist_ok=True)
        total = len(codes)
        for index, std_code in enumerate(codes):
            zt_info("Fetching {} bars of {} ({}/{})...", period, std_code, index + 1, total)
            exchange, code, bars, is_day = self._load_bars(
                std_code, period, start_date, end_date
            )
            path = os.path.join(folder, "{}.{}_{}.csv".format(exchange, code, filetag))
            with open(path, "w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(("date", "time", "open", "high", "low", "close",
                                 "volume", "turnover"))
                for bar in bars:
                    time = "0" if is_day else "{:02d}:{:02d}:00".format(
                        bar["time"] // 100, bar["time"] % 100
                    )
                    writer.writerow((bar["date"], time, bar["open"], bar["high"],
                                     bar["low"], bar["close"], bar["volume"],
                                     bar["turnover"]))
            _notify_progress(progress, index, total)

    def dmpBarsToDB(self, dbHelper, codes, start_date=None, end_date=None,
                    period="day", progress=None):
        """通过 ztpy DBHelper 写入指定代码列表的单一周期 K 线。"""
        self.__check__()
        period_spec(period)
        total = len(codes)
        for index, std_code in enumerate(codes):
            exchange, code, bars, is_day = self._load_bars(
                std_code, period, start_date, end_date
            )
            records = [{
                "exchange": exchange, "code": code, "date": bar["date"],
                "time": encoded_bar_time(bar, is_day), "open": bar["open"],
                "high": bar["high"], "low": bar["low"], "close": bar["close"],
                "volume": bar["volume"], "turnover": bar["turnover"],
            } for bar in bars]
            if records:
                dbHelper.writeBars(records, period)
            _notify_progress(progress, index, total)

    def dmpBars(self, codes, cb, start_date=None, end_date=None,
                period="day", progress=None):
        """将指定代码的 K 线转换为 ZTSBarStruct 数组后交给回调函数。"""
        self.__check__()
        period_spec(period)
        total = len(codes)
        for index, std_code in enumerate(codes):
            exchange, code, bars, is_day = self._load_bars(
                std_code, period, start_date, end_date
            )
            if bars:
                cb(exchange, code, bars_to_c_buffer(bars, is_day), len(bars), period)
            _notify_progress(progress, index, total)

    def _dmp_all_bars(self, writer, start_date=None, end_date=None,
                      periods=("day",), hasIndex=False, products=("STK",),
                      progress=None, failure_limit=20, workers=4):
        """全市场导入编排；workers 控制独立 TDX 工作连接数量。"""
        self.__check__()
        periods = (periods,) if isinstance(periods, str) else tuple(periods)
        for period in periods:
            period_spec(period)
        if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
            raise ValueError("workers must be a positive integer")
        codes = self.getCodeList(
            hasIndex=hasIndex, hasStock=True, products=products,
        )
        zt_info("Importing {} securities for periods {} with {} workers",
                len(codes), periods, workers)

        def loader(std_code, period):
            return self._load_bars(std_code, period, start_date, end_date)

        loader_factory = None
        if workers > 1 and codes and periods:
            start, end = date_range(start_date, end_date)
            loader_factory = self._bar_loader_factory(start, end)

        return import_all_bars(
            codes, periods, loader, writer, progress=progress,
            failure_limit=failure_limit, workers=workers,
            loader_factory=loader_factory,
        )

    def dmpAllBarsToFile(self, folder, start_date=None, end_date=None,
                         periods=("day",), hasIndex=False, products=("STK",),
                         progress=None, failure_limit=20, workers=4):
        """并发下载全部目标证券，并写入 ztpy 风格 CSV 文件。"""
        os.makedirs(folder, exist_ok=True)

        def writer(_, period, exchange, code, bars, is_day):
            _, filetag, _ = period_spec(period)
            path = os.path.join(folder, "{}.{}_{}.csv".format(exchange, code, filetag))
            with open(path, "w", newline="", encoding="utf-8") as stream:
                csv_writer = csv.writer(stream)
                csv_writer.writerow(("date", "time", "open", "high", "low",
                                     "close", "volume", "turnover"))
                for bar in bars:
                    bar_time = "0" if is_day else "{:02d}:{:02d}:00".format(
                        bar["time"] // 100, bar["time"] % 100
                    )
                    csv_writer.writerow((
                        bar["date"], bar_time, bar["open"], bar["high"],
                        bar["low"], bar["close"], bar["volume"], bar["turnover"],
                    ))

        return self._dmp_all_bars(
            writer, start_date=start_date, end_date=end_date, periods=periods,
            hasIndex=hasIndex, products=products, progress=progress,
            failure_limit=failure_limit, workers=workers,
        )

    def dmpAllBarsToDB(self, dbHelper, start_date=None, end_date=None,
                       periods=("day",), hasIndex=False, products=("STK",),
                       progress=None, failure_limit=20, workers=4):
        """并发下载全部目标证券，并通过主线程的 DBHelper 串行写入。"""
        def writer(_, period, exchange, code, bars, is_day):
            records = [{
                "exchange": exchange, "code": code, "date": bar["date"],
                "time": encoded_bar_time(bar, is_day), "open": bar["open"],
                "high": bar["high"], "low": bar["low"], "close": bar["close"],
                "volume": bar["volume"], "turnover": bar["turnover"],
            } for bar in bars]
            dbHelper.writeBars(records, period)

        return self._dmp_all_bars(
            writer, start_date=start_date, end_date=end_date, periods=periods,
            hasIndex=hasIndex, products=products, progress=progress,
            failure_limit=failure_limit, workers=workers,
        )

    def dmpAllBars(self, cb, start_date=None, end_date=None,
                   periods=("day",), hasIndex=False, products=("STK",),
                   progress=None, failure_limit=20, workers=4):
        """并发下载全部目标证券，并在主线程中调用 ztpy 回调。"""
        def writer(_, period, exchange, code, bars, is_day):
            cb(exchange, code, bars_to_c_buffer(bars, is_day), len(bars), period)

        return self._dmp_all_bars(
            writer, start_date=start_date, end_date=end_date, periods=periods,
            hasIndex=hasIndex, products=products, progress=progress,
            failure_limit=failure_limit, workers=workers,
        )

    def _factor_data(self, codes):
        result = {"SSE": {}, "SZSE": {}}
        for index, std_code in enumerate(codes, 1):
            exchange, market, code = parse_std_code(std_code)
            zt_info("Fetching adjust factors of {} ({}/{})...", std_code, index, len(codes))
            result[exchange][code] = adjust_factors(self.api, market, code)
        return result

    def dmpAdjFactorsToFile(self, codes, filename):
        self.__check__()
        factors = self._factor_data(codes)
        os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)
        with open(filename, "w", encoding="utf-8") as stream:
            json.dump(factors, stream, sort_keys=True, indent=4, ensure_ascii=False)

    def dmpAdjFactorsToDB(self, dbHelper: DBHelper, codes):
        self.__check__()
        dbHelper.writeFactors(self._factor_data(codes))

    def getTransactions(self, stdCode, trading_date):
        """Return one trading day's historical transactions as dictionaries."""
        self.__check__()
        _, market, code = parse_std_code(stdCode)
        trading_date = normalize_date(trading_date)
        return fetch_day_transactions(self.api, market, code, trading_date)

    def getMinuteTime(self, stdCode, trading_date):
        """Return one trading day's 240-point minute-time history."""
        self.__check__()
        _, market, code = parse_std_code(stdCode)
        trading_date = normalize_date(trading_date)
        return fetch_day_minute_time(self.api, market, code, trading_date)

    def dmpTransactionsToFile(self, folder, codes, start_date=None, end_date=None,
                              progress=None):
        """Export historical transaction records to one CSV per security."""
        self.__check__()
        start, end = _recent_date_range(start_date, end_date, 30)
        os.makedirs(folder, exist_ok=True)
        total = len(codes)
        for index, std_code in enumerate(codes):
            exchange, market, code = parse_std_code(std_code)
            records = fetch_transactions(self.api, market, code, start, end)
            path = os.path.join(folder, "{}.{}_trans.csv".format(exchange, code))
            with open(path, "w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(
                    stream, fieldnames=("date", "time", "datetime", "price",
                                        "volume", "side", "index")
                )
                writer.writeheader()
                writer.writerows(records)
            _notify_progress(progress, index, total)

    def dmpMinuteTimeToFile(self, folder, codes, start_date=None, end_date=None,
                            progress=None):
        """Export historical 240-point minute-time records to CSV files."""
        self.__check__()
        start, end = _recent_date_range(start_date, end_date, 9000)
        os.makedirs(folder, exist_ok=True)
        total = len(codes)
        for index, std_code in enumerate(codes):
            exchange, market, code = parse_std_code(std_code)
            records = fetch_minute_time(self.api, market, code, start, end)
            path = os.path.join(folder, "{}.{}_time.csv".format(exchange, code))
            with open(path, "w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(
                    stream, fieldnames=("date", "time", "datetime", "price", "volume")
                )
                writer.writeheader()
                writer.writerows(records)
            _notify_progress(progress, index, total)
