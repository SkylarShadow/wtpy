from ztpy.apps.datahelper.data.Common import get_index_code_name_list, get_stk_code_name_list
from ztpy.utils.ztlog import zt_debug, zt_error, zt_info, zt_warn
from ztpy.apps.datahelper.DHDefs import BaseDataHelper, DBHelper
from ztpy.ZtCoreDefs import ZTSBarStruct
from datetime import datetime
from pytdx.hq import TdxHq_API
from pytdx.params import TDXParams
from collections import OrderedDict
import json
import os
import pickle


# ========== K线类型映射（统一管理） ==========

PERIOD_TO_KTYPE = {
    "min1":  8,
    "min5":  0,
    "min15": 1,
    "min30": 2,
    "hour1": 3,
    "day":   9,
    "week":  5,
    "month": 6,
    "quarter": 10,
    "year":  11,
}

PERIOD_TO_FILETAG = {
    "min1":  "m1",  "min5":  "m5",  "min15": "m15", "min30": "m30",
    "hour1": "h1",  "day":   "d",   "week":  "w",    "month": "m",
    "quarter": "q", "year":  "y",
}

DAILY_AND_ABOVE_KTYPES = {
    TDXParams.KLINE_TYPE_DAILY, TDXParams.KLINE_TYPE_WEEKLY,
    TDXParams.KLINE_TYPE_MONTHLY, TDXParams.KLINE_TYPE_3MONTH,
    TDXParams.KLINE_TYPE_YEARLY,
}

SUPPORTED_PERIODS = list(PERIOD_TO_KTYPE.keys())


def _get_ktype(period: str) -> int:
    if period not in PERIOD_TO_KTYPE:
        raise Exception(f"Unrecognized period: {period}, supported: {', '.join(SUPPORTED_PERIODS)}")
    return PERIOD_TO_KTYPE[period]


def _get_filetag(period: str) -> str:
    return PERIOD_TO_FILETAG.get(period, period)


def _is_daily_or_above(ktype: int) -> bool:
    return ktype in DAILY_AND_ABOVE_KTYPES


# ========== 代码缓存管理 ==========

_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tdx_cache")
_CACHE_FILE = os.path.join(_CACHE_DIR, "code_cache.pkl")

_stock_code_cache = {}
_index_code_cache = set()
_cache_timestamp = None


def _ensure_cache_dir():
    if not os.path.exists(_CACHE_DIR):
        os.makedirs(_CACHE_DIR, exist_ok=True)


def _save_code_cache():
    _ensure_cache_dir()
    try:
        with open(_CACHE_FILE, "wb") as f:
            pickle.dump({
                "stock_code_cache": _stock_code_cache,
                "index_code_cache": _index_code_cache,
                "cache_timestamp": _cache_timestamp
            }, f)
        zt_info(f"Code cache saved to {_CACHE_FILE}")
    except Exception as e:
        zt_warn(f"Failed to save code cache: {e}")


def _load_code_cache():
    global _stock_code_cache, _index_code_cache, _cache_timestamp
    if not os.path.exists(_CACHE_FILE):
        return False
    try:
        with open(_CACHE_FILE, "rb") as f:
            cache_data = pickle.load(f)
        _stock_code_cache = cache_data.get("stock_code_cache", {})
        _index_code_cache = cache_data.get("index_code_cache", set())
        _cache_timestamp = cache_data.get("cache_timestamp", None)
        zt_info(f"Code cache loaded from {_CACHE_FILE}")
        return True
    except Exception as e:
        zt_warn(f"Failed to load code cache: {e}")
        return False


def _refresh_code_cache(force: bool = False):
    global _stock_code_cache, _index_code_cache, _cache_timestamp
    
    today = datetime.now().date()
    if not force and _cache_timestamp == today and _stock_code_cache and _index_code_cache:
        zt_debug("Code cache is still valid for today")
        return
    
    zt_info("Force refreshing" if force else "Refreshing" + " stock/index code cache...")
    online_success = False
    
    try:
        from ztpy.apps.datahelper.data.Common import MARKET
        
        new_stock_cache = {}
        for market in [MARKET.SH, MARKET.SZ]:
            try:
                stock_list = get_stk_code_name_list(market)
                if stock_list:
                    codes = {str(item["code"]).strip() for item in stock_list 
                            if str(item.get("code", "")).strip().isdigit() and len(str(item.get("code", "")).strip()) == 6}
                    new_stock_cache[market] = codes if codes else _stock_code_cache.get(market, set())
                    zt_info(f"Got {len(codes)} stock codes for market {market}")
                elif market in _stock_code_cache:
                    new_stock_cache[market] = _stock_code_cache[market]
            except Exception as e:
                zt_warn(f"Failed to get stock list for market {market}: {e}")
                if market in _stock_code_cache:
                    new_stock_cache[market] = _stock_code_cache[market]
        
        new_index_cache = set()
        try:
            index_list = get_index_code_name_list()
            if index_list:
                new_index_cache = {item["market_code"][2:] for item in index_list 
                                  if len(item.get("market_code", "")) >= 4 and item["market_code"][2:].isdigit()}
                zt_info(f"Got {len(new_index_cache)} index codes")
            else:
                new_index_cache = _index_code_cache.copy()
        except Exception as e:
            zt_warn(f"Failed to get index list: {e}")
            new_index_cache = _index_code_cache.copy()
        
        if new_stock_cache and new_index_cache:
            _stock_code_cache = new_stock_cache
            _index_code_cache = new_index_cache
        
        _cache_timestamp = today
        online_success = True
        _save_code_cache()
        
    except Exception as e:
        zt_warn(f"Online refresh failed: {e}")
    
    if not online_success and not (_stock_code_cache and _index_code_cache):
        zt_info("Trying to load code cache from file...")
        if not _load_code_cache():
            zt_warn("No cache available, will use rule-based判断")
    
    if _stock_code_cache and _index_code_cache:
        total_stocks = sum(len(codes) for codes in _stock_code_cache.values())
        zt_info(f"Cache status: {total_stocks} stocks, {len(_index_code_cache)} indexes")


SH_STOCK_PREFIXES = {"600", "601", "603", "605", "688", "900"}
SZ_STOCK_PREFIXES = {"000", "001", "002", "003", "004", "200", "300", "301"}
SZ_INDEX_PREFIXES = {"395", "396", "397", "398", "399"}
SH_INDEX_PREFIXES = {"000", "950", "999"}


def _is_index(code: str, market: int) -> bool:
    if not code or not code.isdigit() or len(code) < 6:
        return False
    
    try:
        _refresh_code_cache()
    except Exception as e:
        zt_error(f"Failed to refresh cache in _is_index: {e}")
    
    if _stock_code_cache and _index_code_cache:
        if any(code in codes for codes in _stock_code_cache.values()):
            return False
        if code in _index_code_cache:
            return True
        zt_debug(f"Code {code} not found in cache, using rule-based判断")
    
    return _is_index_by_rule(code, market)


def _is_index_by_rule(code: str, market: int) -> bool:
    if not code or not code.isdigit() or len(code) < 3:
        return False
    
    prefix = code[:3]
    
    if market == TDXParams.MARKET_SH:
        if prefix in SH_STOCK_PREFIXES:
            return False
        if prefix in SH_INDEX_PREFIXES:
            return True
        return True
    elif market == TDXParams.MARKET_SZ:
        if prefix in SZ_STOCK_PREFIXES:
            return False
        if prefix in SZ_INDEX_PREFIXES:
            return True
    return False


def _extract_code_name_and_type(item, market):
    try:
        if isinstance(item, (dict, OrderedDict)):
            code = str(item.get("code", "")).strip()
            name = str(item.get("name", "")).strip()
        else:
            code = str(item[0]).strip() if item else ""
            name = str(item[1]).strip() if len(item) > 1 else ""
        
        if not code:
            return "", "", ""
        
        is_idx = _is_index(code, market)
        if is_idx:
            return code, name, "IDX"
        
        exchg = "SSE" if market == TDXParams.MARKET_SH else "SZSE"
        if (code.startswith("5") and exchg == "SSE") or (code.startswith("159") and exchg == "SZSE"):
            return code, name, "ETF"
        
        return code, name, "STK"
    except Exception:
        return "", "", ""


# ========== 工具函数 ==========

def _std_to_tdx_market(stdCode: str):
    parts = stdCode.split(".")
    exchg = parts[0]
    raw = parts[-1] if len(parts) > 2 else parts[1]
    
    if exchg == "SSE":
        return TDXParams.MARKET_SH, raw
    elif exchg == "SZSE":
        return TDXParams.MARKET_SZ, raw
    else:
        raise NotImplementedError(f"Tdx helper supports only SSE/SZSE equities, got {stdCode}")


def _parse_bar_item(item, ktype: int):
    try:
        if isinstance(item, (dict, OrderedDict)):
            year, month, day = int(item.get("year", 0)), int(item.get("month", 0)), int(item.get("day", 0))
            date_int = year * 10000 + month * 100 + day
            time_val = 0 if _is_daily_or_above(ktype) else int(item.get("hour", 0)) * 100 + int(item.get("minute", 0))
            return {
                "date": date_int, "time": time_val,
                "open": float(item.get("open", 0)), "high": float(item.get("high", 0)),
                "low": float(item.get("low", 0)), "close": float(item.get("close", 0)),
                "vol": float(item.get("vol", 0)), "amount": float(item.get("amount", 0))
            }
        else:
            if len(item) < 4:
                return None
            y, m, d = int(item[0]), int(item[1]), int(item[2])
            date_int = y * 10000 + m * 100 + d
            
            if _is_daily_or_above(ktype):
                time_val = 0
                o, h, l, c, v, a = item[3:9] if len(item) > 8 else (0,)*6
            else:
                time_val = (int(item[3]) if len(item) > 3 else 0) * 100 + (int(item[4]) if len(item) > 4 else 0)
                o, h, l, c, v, a = (item[i] if len(item) > i else 0 for i in range(5, 11))
            
            return {
                "date": date_int, "time": time_val,
                "open": float(o), "high": float(h), "low": float(l), "close": float(c),
                "vol": float(v), "amount": float(a)
            }
    except Exception as e:
        zt_debug(f"Parse bar item failed: {e}")
        return None


def _fetch_bars(api, ktype: int, market: int, code: str, 
                start_int: int, end_int: int, is_index: bool = False) -> list:
    all_bars = []
    pos = 0
    count = 800
    
    while True:
        try:
            bars = api.get_index_bars(ktype, market, code, pos, count) if is_index else \
                   api.get_security_bars(ktype, market, code, pos, count)
        except Exception as e:
            zt_warn(f"get_bars failed for code={code} pos={pos}: {e}")
            break
        
        if not bars:
            break
        
        for item in bars:
            parsed = _parse_bar_item(item, ktype)
            if parsed and parsed["date"] and start_int <= parsed["date"] <= end_int:
                all_bars.append(parsed)
        
        if len(bars) < count:
            break
        pos += len(bars)
        if pos > 100000:
            zt_warn(f"Too many bars fetched for code={code}, breaking")
            break
    
    # 去重排序
    seen = {}
    for bar in all_bars:
        key = (bar["date"], bar["time"])
        if key not in seen:
            seen[key] = bar
    return sorted(seen.values(), key=lambda x: (x["date"], x["time"]))


# ========== 复权因子提取（公共函数） ==========

def _extract_adjust_factors(xdxr_data) -> list:
    """从 get_xdxr_info 返回数据中提取复权因子列表"""
    factors = []
    if not xdxr_data:
        return [{"date": 19900101, "factor": 1.0}]
    
    for item in xdxr_data:
        try:
            if isinstance(item, dict):
                date = item.get("date", item.get("exdate", ""))
                factor = item.get("factor", item.get("adj_factor", 1.0))
            else:
                date = str(item[0]) if len(item) > 0 else ""
                factor = float(item[-1]) if len(item) > 0 else 1.0
            
            if date:
                date_int = int(str(date).replace("-", ""))
                factors.append({"date": date_int, "factor": float(factor)})
        except Exception as e:
            zt_debug(f"Parse xdxr item failed: {e}")
            continue
    
    if not factors:
        factors.append({"date": 19900101, "factor": 1.0})
    
    factors.sort(key=lambda x: x["date"])
    
    # 去重连续的相同因子
    cleaned = []
    prev = None
    for f in factors:
        if f["factor"] != prev:
            cleaned.append(f)
            prev = f["factor"]
    return cleaned


# ========== Bar 数据处理（公共函数） ==========

def _bars_to_dict_list(bars: list, exchg: str, code: str, is_day: bool) -> list:
    """将 bar 列表转换为数据库写入格式"""
    result = []
    for bar in bars:
        time_val = 0 if is_day else bar["time"] + (bar["date"] - 19900000) * 10000
        result.append({
            "exchange": exchg, "code": code,
            "date": bar["date"], "time": time_val,
            "open": bar["open"], "high": bar["high"],
            "low": bar["low"], "close": bar["close"],
            "volume": bar["vol"], "turnover": bar["amount"]
        })
    return result


def _bars_to_zts_struct(bars: list, is_day: bool) -> list:
    """将 bar 列表转换为 ZTSBarStruct 列表"""
    bar_list = []
    for bar in bars:
        cur_bar = ZTSBarStruct()
        cur_bar.date = bar["date"]
        cur_bar.time = 0 if is_day else bar["time"] + (bar["date"] - 19900000) * 10000
        cur_bar.open = bar["open"]
        cur_bar.high = bar["high"]
        cur_bar.low = bar["low"]
        cur_bar.close = bar["close"]
        cur_bar.vol = bar["vol"]
        cur_bar.money = bar["amount"]
        cur_bar.settle = 0
        cur_bar.hold = 0
        cur_bar.diff = 0
        bar_list.append(cur_bar)
    return bar_list


def _copy_bars_to_buffer(bar_list: list):
    """将 ZTSBarStruct 列表复制到 C 数组缓冲区"""
    BUFFER = ZTSBarStruct * len(bar_list)
    buffer = BUFFER()
    for i, src in enumerate(bar_list):
        dst = buffer[i]
        for field in ["date", "time", "open", "high", "low", "close", "settle", "money", "vol", "hold", "diff"]:
            setattr(dst, field, getattr(src, field))
    return buffer


# ========== K线获取与处理（公共函数） ==========

def _fetch_and_process_bars(api, stdCode: str, period: str, 
                             start_int: int, end_int: int) -> tuple:
    """
    获取并处理K线数据
    返回: (exchg, code, bars, is_day) 或 (None, None, None, None)
    """
    try:
        market, code = _std_to_tdx_market(stdCode)
    except NotImplementedError:
        zt_warn(f"Skip unsupported code {stdCode}")
        return None, None, None, None
    
    exchg = stdCode.split(".")[0]
    ktype = _get_ktype(period)
    is_day = _is_daily_or_above(ktype)
    is_index = _is_index(code, market)
    
    bars = _fetch_bars(api, ktype, market, code, start_int, end_int, is_index)
    return exchg, code, bars, is_day


# ========== 复权因子处理（公共函数） ==========

def _process_adjust_factors(api, codes: list) -> dict:
    """处理复权因子，返回 {exchg: {code: [factors]}} 格式"""
    stocks = {"SSE": {}, "SZSE": {}}
    
    for count, stdCode in enumerate(codes, 1):
        try:
            market, code = _std_to_tdx_market(stdCode)
        except NotImplementedError:
            zt_warn(f"Skip unsupported code {stdCode}")
            continue
        
        exchg = stdCode.split(".")[0]
        zt_info(f"Fetching adjust factors of {stdCode}({count}/{len(codes)})...")
        
        try:
            xdxr_data = api.get_xdxr_info(market, code)
            factors = _extract_adjust_factors(xdxr_data)
        except Exception as e:
            zt_warn(f"Failed to get xdxr info for {stdCode}: {e}")
            factors = [{"date": 19900101, "factor": 1.0}]
        
        if exchg not in stocks:
            stocks[exchg] = {}
        stocks[exchg][code] = factors
    
    return stocks


# ========== DHTdx 类 ==========

class DHTdx(BaseDataHelper):

    def __init__(self):
        BaseDataHelper.__init__(self)
        self.api = None
        self.host = "sztdx.gtjas.com"
        self.port = 7709
        zt_info("Tdx helper has been created.")

    def __check__(self):
        if not self.isAuthed or self.api is None:
            raise Exception("This module has not authorized yet!")

    def auth(self, host: str = None, port: int = None, **kwargs):
        if host:
            self.host = host
        if port:
            self.port = port
        
        try:
            self.api = TdxHq_API(heartbeat=True, auto_retry=True)
            if not self.api.connect(self.host, self.port, time_out=5):
                raise Exception(f"Failed to connect to {self.host}:{self.port}")
            self.isAuthed = True
            zt_info(f"Tdx has been authorized, connected to {self.host}:{self.port}")
        except Exception as e:
            zt_error(f"Failed to connect to TDX server {self.host}:{self.port}: {e}")
            raise

    def dmpCodeListToFile(self, filename: str, hasIndex: bool = True, hasStock: bool = True):
        self.__check__()
        
        try:
            _refresh_code_cache()
        except Exception as e:
            zt_warn(f"Failed to refresh code cache: {e}")
        
        stocks = {"SSE": {}, "SZSE": {}}
        
        for market, exchg in [(TDXParams.MARKET_SH, "SSE"), (TDXParams.MARKET_SZ, "SZSE")]:
            start = 0
            page_size = 200
            while True:
                try:
                    items = self.api.get_security_list(market, start)
                except Exception as e:
                    zt_warn(f"get_security_list failed for market={market} start={start}: {e}")
                    break
                
                if not items:
                    break
                
                for item in items:
                    code, name, pid = _extract_code_name_and_type(item, market)
                    if not code or not pid:
                        continue
                    if (pid == "IDX" and not hasIndex) or (pid in ("STK", "ETF") and not hasStock):
                        continue
                    stocks[exchg][code] = {"exchg": exchg, "code": code, "name": name, "product": pid}
                
                start += len(items)
                if len(items) < page_size:
                    break
        
        zt_info(f"Writing code list into file {filename}...")
        os.makedirs(os.path.dirname(filename) or ".", exist_ok=True)
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(stocks, f, sort_keys=True, indent=4, ensure_ascii=False)

    def dmpAdjFactorsToFile(self, codes: list, filename: str):
        self.__check__()
        zt_info("Fetching adjust factors using get_xdxr_info...")
        stocks = _process_adjust_factors(self.api, codes)
        
        zt_info(f"Writing adjust factors into file {filename}...")
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(stocks, f, sort_keys=True, indent=4, ensure_ascii=False)

    def dmpAdjFactorsToDB(self, dbHelper: DBHelper, codes: list):
        self.__check__()
        zt_info("Fetching adjust factors for database using get_xdxr_info...")
        stocks = _process_adjust_factors(self.api, codes)
        
        zt_info("Writing adjust factors into database...")
        dbHelper.writeFactors(stocks)

    def dmpBarsToFile(self, folder: str, codes: list, start_date: datetime = None, 
                      end_date: datetime = None, period: str = "day"):
        self.__check__()
        os.makedirs(folder, exist_ok=True)
        
        start_date = start_date or datetime(1990, 1, 1)
        end_date = end_date or datetime.now()
        filetag = _get_filetag(period)
        start_int = int(start_date.strftime("%Y%m%d"))
        end_int = int(end_date.strftime("%Y%m%d"))
        
        for count, stdCode in enumerate(codes, 1):
            exchg, code, bars, is_day = _fetch_and_process_bars(self.api, stdCode, period, start_int, end_int)
            if not bars:
                continue
            
            zt_info(f"Fetching {period} bars of {stdCode}({count}/{len(codes)})...")
            
            content = "date,time,open,high,low,close,volume,turnover\n"
            for bar in bars:
                time_str = "0" if is_day else str(bar["time"])
                content += f"{bar['date']},{time_str},{bar['open']},{bar['high']},{bar['low']},{bar['close']},{int(bar['vol'])},{int(bar['amount'])}\n"
            
            filepath = os.path.join(folder, f"{stdCode}_{filetag}.csv")
            zt_info(f"Writing bars into file {filepath}...")
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

    def dmpBarsToDB(self, dbHelper: DBHelper, codes: list, start_date: datetime = None,
                    end_date: datetime = None, period: str = "day"):
        self.__check__()
        
        start_date = start_date or datetime(1990, 1, 1)
        end_date = end_date or datetime.now()
        start_int = int(start_date.strftime("%Y%m%d"))
        end_int = int(end_date.strftime("%Y%m%d"))
        
        for count, stdCode in enumerate(codes, 1):
            exchg, code, bars, is_day = _fetch_and_process_bars(self.api, stdCode, period, start_int, end_int)
            if not bars:
                continue
            
            zt_info(f"Fetching {period} bars of {stdCode}({count}/{len(codes)})...")
            db_bars = _bars_to_dict_list(bars, exchg, code, is_day)
            
            zt_info(f"Writing {len(db_bars)} bars into database...")
            dbHelper.writeBars(db_bars, period)

    def dmpBars(self, codes: list, cb, start_date: datetime = None, 
                end_date: datetime = None, period: str = "day"):
        self.__check__()
        
        start_date = start_date or datetime(1990, 1, 1)
        end_date = end_date or datetime.now()
        start_int = int(start_date.strftime("%Y%m%d"))
        end_int = int(end_date.strftime("%Y%m%d"))
        
        for count, stdCode in enumerate(codes, 1):
            exchg, code, bars, is_day = _fetch_and_process_bars(self.api, stdCode, period, start_int, end_int)
            if not bars:
                continue
            
            zt_info(f"Fetching {period} bars of {stdCode}({count}/{len(codes)})...")
            bar_list = _bars_to_zts_struct(bars, is_day)
            buffer = _copy_bars_to_buffer(bar_list)
            
            cb(exchg, code, buffer, len(bar_list), period)