"""pytdx 数据助手使用的证券元数据、代码转换与每日目录缓存。"""

from datetime import date, datetime

from pytdx.params import TDXParams

from ztpy.utils.ztlog import zt_info, zt_warn


# ztpy 周期名称 -> (pytdx 周期常量, 文件名后缀, 是否为日线级别)。
PERIODS = {
    "min1": (TDXParams.KLINE_TYPE_1MIN, "m1", False),
    "min5": (TDXParams.KLINE_TYPE_5MIN, "m5", False),
    "min15": (TDXParams.KLINE_TYPE_15MIN, "m15", False),
    "min30": (TDXParams.KLINE_TYPE_30MIN, "m30", False),
    "hour1": (TDXParams.KLINE_TYPE_1HOUR, "h1", False),
    "day": (TDXParams.KLINE_TYPE_RI_K, "d", True),
    "week": (TDXParams.KLINE_TYPE_WEEKLY, "w", True),
    "month": (TDXParams.KLINE_TYPE_MONTHLY, "m", True),
    "quarter": (TDXParams.KLINE_TYPE_3MONTH, "q", True),
    "year": (TDXParams.KLINE_TYPE_YEARLY, "y", True),
}

CORE_INDEXES = {
    "SSE": {
        "000001": "上证指数", "000016": "上证50", "000300": "沪深300",
        "000688": "科创50", "000905": "中证500",
    },
    "SZSE": {
        "399001": "深证成指", "399005": "中小100",
        "399006": "创业板指", "399300": "沪深300",
    },
}

SECURITY_MARKETS = (
    (TDXParams.MARKET_SH, "SSE"),
    (TDXParams.MARKET_SZ, "SZSE"),
)


def period_spec(period):
    """取得周期配置，并对不支持的周期给出明确错误。"""
    try:
        return PERIODS[period.lower()]
    except (AttributeError, KeyError):
        raise ValueError(
            "Unsupported period {!r}; supported periods: {}".format(
                period, ", ".join(PERIODS)
            )
        )


def parse_std_code(std_code):
    """将 SSE.600000 等 ztpy 标准代码转换为 pytdx 市场与六位代码。"""
    parts = std_code.split(".")
    if len(parts) < 2 or not parts[-1]:
        raise ValueError("Invalid standard code: {!r}".format(std_code))

    exchange, code = parts[0].upper(), parts[-1]
    markets = {
        "SSE": TDXParams.MARKET_SH, "SH": TDXParams.MARKET_SH,
        "SZSE": TDXParams.MARKET_SZ, "SZ": TDXParams.MARKET_SZ,
    }
    if exchange not in markets:
        raise NotImplementedError(
            "pytdx helper only supports SSE/SZSE, got {!r}".format(std_code)
        )
    if len(code) != 6 or not code.isdigit():
        raise ValueError("Invalid SSE/SZSE security code: {!r}".format(std_code))
    market = markets[exchange]
    return ("SSE" if market == TDXParams.MARKET_SH else "SZSE", market, code)


def normalize_date(value, default=None):
    """接受 date、datetime 或 YYYYMMDD 整数并返回统一的整数日期。"""
    value = default if value is None else value
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.year * 10000 + value.month * 100 + value.day
    if isinstance(value, int) and 19900101 <= value <= 29991231:
        return value
    raise TypeError("date value must be datetime, date, YYYYMMDD integer, or None")


def date_range(start_date, end_date):
    """规范化起止日期；未指定时默认从 1990-01-01 到今天。"""
    start = normalize_date(start_date, date(1990, 1, 1))
    end = normalize_date(end_date, date.today())
    if start > end:
        raise ValueError("start_date must not be later than end_date")
    return start, end


def is_index(market, code, security_types=None):
    """优先使用精确目录判断指数，目录缺失时再使用保守的代码前缀。"""
    if security_types:
        product = security_types.get((market, code))
        if product is not None:
            return product == "IDX"
    if market == TDXParams.MARKET_SH:
        return code.startswith(("000", "880", "930", "931", "950", "990", "999"))
    return code.startswith(("395", "396", "397", "398", "399"))


#TODO: 需核对
def security_product(market, code):
    """根据交易所与代码前缀识别股票或 ETF，未知产品返回 None。"""
    if market == TDXParams.MARKET_SH:
        if code.startswith(("600", "601", "603", "605", "688", "689", "900")):
            return "STK"
        if code.startswith(("500", "501", "502", "505", "506", "508", "510",
                            "511", "512", "513", "515", "516", "517", "518",
                            "560", "561", "562", "563", "588")):
            return "ETF"
    else:
        if code.startswith(("000", "001", "002", "003", "200", "300", "301")):
            return "STK"
        if code.startswith(("150", "159", "160", "161", "162", "163", "164",
                            "165", "166", "167", "168", "169", "180", "184")):
            return "ETF"
    return None


class TdxSecurityCatalog:
    """按交易所维护的每日证券缓存"""

    def __init__(self):
        self.types = {}
        self.names = {}
        self.cache_date = None
        self.live_cache_date = None

    @staticmethod
    def _load_tdx_securities(api):
        """分页读取 TDX 沪深证券目录，单个市场失败不会影响另一个市场。"""
        result = []
        for market, exchange in SECURITY_MARKETS:
            try:
                total = int(api.get_security_count(market) or 0)
            except Exception as exc:
                zt_warn("Failed to get {} security count: {}", exchange, exc)
                total = 0

            start = 0
            while True:
                try:
                    rows = api.get_security_list(market, start) or []
                except Exception as exc:
                    zt_warn("Failed to get {} security list at {}: {}",
                            exchange, start, exc)
                    break
                if not rows:
                    break
                result.extend((market, item) for item in rows)
                start += len(rows)
                if (total and start >= total) or (not total and len(rows) < 1000):
                    break
        return result

    def refresh(self, force=False, api=None):
        """合并权威股票/指数清单与 TDX 实时目录。

        基础清单提供更可靠的股票和指数分类，TDX 目录负责补充名称以及
        ETF 等产品。两类数据分别按天缓存，避免批量导入时重复访问网络。
        """
        today = date.today()
        if force:
            self.live_cache_date = None
        cache_ready = self.cache_date == today and bool(self.types)
        live_ready = api is None or self.live_cache_date == today
        if not force and cache_ready and live_ready:
            return

        # 同一天已有基础目录时直接复制，在其上补充实时 TDX 数据。
        types = {} if force or not cache_ready else dict(self.types)
        names = {} if force or not cache_ready else dict(self.names)
        if not types:
            # 股票和指数来源分开导入，避免仅凭重叠的代码前缀误判类型。
            try:
                from ztpy.apps.datahelper.data.common import (
                    MARKET, get_index_code_name_list, get_stk_code_name_list,
                )
                for market_name, market in ((MARKET.SH, TDXParams.MARKET_SH),
                                            (MARKET.SZ, TDXParams.MARKET_SZ)):
                    for item in get_stk_code_name_list(market_name) or []:
                        code = str(item.get("code", "")).strip()
                        if len(code) == 6 and code.isdigit():
                            key = market, code
                            types[key] = "STK"
                            names[key] = str(item.get("name", "")).strip()

                for item in get_index_code_name_list() or []:
                    market_code = str(item.get("market_code", "")).upper()
                    market = {"SH": TDXParams.MARKET_SH,
                              "SZ": TDXParams.MARKET_SZ}.get(market_code[:2])
                    code = market_code[2:]
                    if market is not None and len(code) == 6 and code.isdigit():
                        key = market, code
                        types[key] = "IDX"
                        names[key] = str(item.get("name", "")).strip()
            except Exception as exc:
                zt_warn("Failed to refresh stock/index reference lists: {}", exc)

        if api is not None and (force or self.live_cache_date != today):
            # 仅为基础清单中没有的证券推断类型，不覆盖已经确认的分类。
            live_securities = self._load_tdx_securities(api)
            for market, item in live_securities:
                code = str(item.get("code", "")).strip()
                if len(code) != 6 or not code.isdigit():
                    continue
                key = market, code
                names[key] = str(item.get("name", "")).strip() or names.get(key, "")
                if key not in types:
                    product = "IDX" if is_index(market, code) else security_product(market, code)
                    if product:
                        types[key] = product
            if live_securities:
                self.live_cache_date = today

        if types:
            self.types, self.names = types, names
            stocks = sum(value == "STK" for value in types.values())
            indexes = sum(value == "IDX" for value in types.values())
            zt_info("Loaded {} stocks and {} indexes for type detection", stocks, indexes)
        elif not self.types:
            zt_warn("Stock/index reference lists are empty; using code rules")
        self.cache_date = today

    def resolve_is_index(self, market, code):
        """使用当前缓存判断给定 pytdx 市场代码是否为指数。"""
        self.refresh()
        return is_index(market, code, self.types)

    def securities(self, api=None, has_index=True, has_stock=True,
                   products=None, force=False):
        """返回经过类型过滤并按交易所、代码排序的证券信息字典。"""
        self.refresh(force=force, api=api)
        if isinstance(products, str):
            products = (products,)
        wanted = None if products is None else {value.upper() for value in products}
        result = []
        exchanges = dict(SECURITY_MARKETS)
        for (market, code), product in self.types.items():
            if product == "IDX" and not has_index:
                continue
            if product != "IDX" and not has_stock:
                continue
            if wanted is not None and product not in wanted:
                continue
            result.append({
                "exchg": exchanges[market],
                "code": code,
                "name": self.names.get((market, code), ""),
                "product": product,
            })
        return sorted(result, key=lambda item: (item["exchg"], item["code"]))

    def codes(self, api=None, has_index=False, has_stock=True,
              products=("STK",), force=False):
        """返回 SSE.600000 形式的代码列表，默认只包含股票。"""
        if isinstance(products, str):
            products = (products,)
        if products is not None and has_index:
            products = tuple(products) + ("IDX",)
        return ["{}.{}".format(item["exchg"], item["code"])
                for item in self.securities(
                    api=api, has_index=has_index, has_stock=has_stock,
                    products=products, force=force,
                )]
