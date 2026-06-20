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
import logging



import pickle
from pathlib import Path

# 缓存文件路径
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tdx_cache")
_CACHE_FILE = os.path.join(_CACHE_DIR, "code_cache.pkl")

# 股票代码集合缓存 {market: set(codes)}
_stock_code_cache = {}
# 指数代码集合缓存 set(codes)
_index_code_cache = set()
# 缓存时间戳
_cache_timestamp = None

def _ensure_cache_dir():
    """确保缓存目录存在"""
    if not os.path.exists(_CACHE_DIR):
        os.makedirs(_CACHE_DIR, exist_ok=True)


def _save_code_cache():
    """保存代码缓存到文件"""
    _ensure_cache_dir()
    cache_data = {
        "stock_code_cache": _stock_code_cache,
        "index_code_cache": _index_code_cache,
        "cache_timestamp": _cache_timestamp
    }
    try:
        with open(_CACHE_FILE, "wb") as f:
            pickle.dump(cache_data, f)
        zt_info(f"Code cache saved to {_CACHE_FILE}")
    except Exception as e:
        zt_warn(f"Failed to save code cache: {e}")


def _load_code_cache():
    """从文件加载代码缓存"""
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
    """
    刷新股票和指数代码缓存
    
    优先在线获取，成功则更新缓存并保存到文件
    在线获取失败则尝试读取本地缓存文件
    如果本地缓存也没有，则使用代码规则判断
    @force  是否强制刷新，默认 False（按有效期判断）
    """
    global _stock_code_cache, _index_code_cache, _cache_timestamp
    
    # 检查缓存是否在有效期内（每天刷新一次）
    today = datetime.now().date()
    if not force and _cache_timestamp == today and _stock_code_cache and _index_code_cache:
        zt_debug("Code cache is still valid for today")
        return
    
    if force:
        zt_info("Force refreshing stock/index code cache...")
    else:
        zt_info("Refreshing stock/index code cache...")
    
    online_success = False
    
    try:
        # 尝试在线获取
        from ztpy.apps.datahelper.data.Common import MARKET
        
        # 获取股票代码
        new_stock_cache = {}
        stock_markets = [MARKET.SH, MARKET.SZ]
        
        for market in stock_markets:
            try:
                stock_list = get_stk_code_name_list(market)
                if stock_list:
                    codes = set()
                    for item in stock_list:
                        code = str(item.get("code", "")).strip()
                        if code and code.isdigit() and len(code) == 6:
                            codes.add(code)
                    if codes:
                        new_stock_cache[market] = codes
                        zt_info(f"Got {len(codes)} stock codes for market {market}")
                    else:
                        # 如果该市场获取为空，保留旧缓存
                        if market in _stock_code_cache:
                            new_stock_cache[market] = _stock_code_cache[market]
                            zt_debug(f"Using old cache for market {market} ({len(_stock_code_cache[market])} codes)")
                else:
                    # 获取失败，保留旧缓存
                    if market in _stock_code_cache:
                        new_stock_cache[market] = _stock_code_cache[market]
                        zt_debug(f"Using old cache for market {market} ({len(_stock_code_cache[market])} codes)")
            except Exception as e:
                zt_warn(f"Failed to get stock list for market {market}: {e}")
                # 保留旧缓存
                if market in _stock_code_cache:
                    new_stock_cache[market] = _stock_code_cache[market]
                    zt_debug(f"Using old cache for market {market} after error")
        
        # 获取指数代码
        new_index_cache = set()
        try:
            index_list = get_index_code_name_list()
            if index_list:
                for item in index_list:
                    market_code = item.get("market_code", "")
                    if market_code and len(market_code) >= 4:
                        # market_code 格式如 "SH000001", "SZ399001"
                        code = market_code[2:]  # 去掉市场前缀
                        if code and code.isdigit() and len(code) == 6:
                            new_index_cache.add(code)
                
                zt_info(f"Got {len(new_index_cache)} index codes")
            else:
                # 获取失败，保留旧缓存
                new_index_cache = _index_code_cache.copy()
                zt_debug(f"Using old index cache ({len(new_index_cache)} codes)")
        except Exception as e:
            zt_warn(f"Failed to get index list: {e}")
            new_index_cache = _index_code_cache.copy()
            zt_debug(f"Using old index cache after error ({len(new_index_cache)} codes)")
        
        # 更新缓存
        if new_stock_cache and new_index_cache:
            _stock_code_cache = new_stock_cache
            _index_code_cache = new_index_cache
        
        _cache_timestamp = today
        online_success = True
        
        # 保存到文件
        _save_code_cache()
        
    except Exception as e:
        zt_warn(f"Online refresh failed: {e}")
    
    # 如果在线获取失败，尝试加载本地缓存
    if not online_success:
        if not _stock_code_cache or not _index_code_cache:
            zt_info("Trying to load code cache from file...")
            if not _load_code_cache():
                zt_warn("No cache available, will use rule-based判断")
    
    # 打印缓存状态
    if _stock_code_cache and _index_code_cache:
        total_stocks = sum(len(codes) for codes in _stock_code_cache.values())
        zt_info(f"Cache status: {total_stocks} stocks, {len(_index_code_cache)} indexes")



def _is_index(code: str, market: int) -> bool:
    """
    判断是否为指数
    
    优先使用缓存中的精确代码列表，
    如果缓存不可用，则使用代码规则判断
    """
    if not code or not code.isdigit() or len(code) < 6:
        return False
    
    # 尝试刷新缓存
    try:
        _refresh_code_cache()
    except Exception as e:
        zt_error(f"Failed to refresh cache in _is_index: {e}")
    
    # 如果有精确的代码缓存，优先使用
    if _stock_code_cache and _index_code_cache:
        # 检查是否在股票缓存中
        for market_codes in _stock_code_cache.values():
            if code in market_codes:
                return False
        
        # 检查是否在指数缓存中
        if code in _index_code_cache:
            return True
        
        # 如果都不在，使用规则辅助判断
        zt_debug(f"Code {code} not found in cache, using rule-based判断")
    
    # 回退到代码规则判断
    return _is_index_by_rule(code, market)



def _is_index_by_rule(code: str, market: int) -> bool:
    """
    通过代码规则判断是否为指数
    
    规则说明：
    - 上海市场股票：600/601/603/605/688/900 开头
    - 上海市场指数：000/950/999 等开头
    - 深圳市场股票：000/001/002/003/004/200/300/301 开头
    - 深圳市场指数：395/396/397/398/399 等开头
    """
    if not code or not code.isdigit() or len(code) < 3:
        return False
    
    prefix = code[:3]
    
    # 上海市场股票代码前缀
    SH_STOCK_PREFIXES = {"600", "601", "603", "605", "688", "900"}
    # 深圳市场股票代码前缀
    SZ_STOCK_PREFIXES = {"000", "001", "002", "003", "004", "200", "300", "301"}
    # 深圳市场指数代码前缀
    SZ_INDEX_PREFIXES = {"395", "396", "397", "398", "399"}
    # 上海市场指数代码前缀
    SH_INDEX_PREFIXES = {"000", "950", "999"}
    
    if market == TDXParams.MARKET_SH:
        if prefix in SH_STOCK_PREFIXES:
            return False
        if prefix in SH_INDEX_PREFIXES:
            return True
        # 上海市场：除了已知股票前缀，基本都是指数或基金
        return True
    
    elif market == TDXParams.MARKET_SZ:
        if prefix in SZ_STOCK_PREFIXES:
            return False
        if prefix in SZ_INDEX_PREFIXES:
            return True
    
    # 兜底判断
    return False

def _extract_code_name_and_type(item, market):
    """
    从 get_security_list 返回的 item 中提取 code、name 和产品类型
    
    返回: (code, name, product_type)
    """
    try:
        if isinstance(item, (dict, OrderedDict)):
            code = str(item.get("code", "")).strip()
            name = str(item.get("name", "")).strip()
        else:
            code = str(item[0]).strip() if item else ""
            name = str(item[1]).strip() if len(item) > 1 else ""
        
        if not code:
            return "", "", ""
        
        # 判断产品类型
        is_idx = _is_index(code, market)
        
        if is_idx:
            pid = "IDX"
        else:
            exchg = "SSE" if market == TDXParams.MARKET_SH else "SZSE"
            # ETF 判断
            if code.startswith("5") and exchg == "SSE":
                pid = "ETF"
            elif code.startswith("159") and exchg == "SZSE":
                pid = "ETF"
            else:
                pid = "STK"
        
        return code, name, pid
        
    except Exception:
        return "", "", ""


def _std_to_tdx_market(stdCode: str):
    """
    将标准代码转换为 pytdx 的 market 和 code
    返回: (market, code) 其中 market: TDXParams.MARKET_SZ(0)=深圳, TDXParams.MARKET_SH(1)=上海
    """
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
    """
    解析 pytdx 返回的 bar 数据项
    返回: dict with keys: date, time, open, high, low, close, vol, amount
    返回 None 表示解析失败
    """
    try:
        if isinstance(item, (dict, OrderedDict)):
            # pytdx 返回的 OrderedDict 格式
            year = int(item.get("year", 0))
            month = int(item.get("month", 0))
            day = int(item.get("day", 0))
            date_int = year * 10000 + month * 100 + day
            
            # 判断是否为日线及以上周期
            hour = int(item.get("hour", 0))
            minute = int(item.get("minute", 0))
            
            if ktype in [TDXParams.KLINE_TYPE_DAILY, TDXParams.KLINE_TYPE_WEEKLY,
                        TDXParams.KLINE_TYPE_MONTHLY, TDXParams.KLINE_TYPE_3MONTH,
                        TDXParams.KLINE_TYPE_YEARLY]:
                time_val = 0
            else:
                # 分钟线：组合小时和分钟为 HHMM 格式
                time_val = hour * 100 + minute
            
            return {
                "date": date_int,
                "time": time_val,
                "open": float(item.get("open", 0)),
                "high": float(item.get("high", 0)),
                "low": float(item.get("low", 0)),
                "close": float(item.get("close", 0)),
                "vol": float(item.get("vol", 0)),
                "amount": float(item.get("amount", 0))
            }
        else:
            # tuple 格式备选处理
            if len(item) < 4:
                return None
            
            y, m, d = int(item[0]), int(item[1]), int(item[2])
            date_int = y * 10000 + m * 100 + d
            
            if ktype in [TDXParams.KLINE_TYPE_DAILY, TDXParams.KLINE_TYPE_WEEKLY,
                        TDXParams.KLINE_TYPE_MONTHLY, TDXParams.KLINE_TYPE_3MONTH,
                        TDXParams.KLINE_TYPE_YEARLY]:
                time_val = 0
                o_idx, h_idx, l_idx, c_idx, v_idx, a_idx = 3, 4, 5, 6, 7, 8
            else:
                hour = int(item[3]) if len(item) > 3 else 0
                minute = int(item[4]) if len(item) > 4 else 0
                time_val = hour * 100 + minute
                o_idx, h_idx, l_idx, c_idx, v_idx, a_idx = 5, 6, 7, 8, 9, 10
            
            o = float(item[o_idx]) if len(item) > o_idx else 0
            h = float(item[h_idx]) if len(item) > h_idx else 0
            l = float(item[l_idx]) if len(item) > l_idx else 0
            c = float(item[c_idx]) if len(item) > c_idx else 0
            v = float(item[v_idx]) if len(item) > v_idx else 0
            a = float(item[a_idx]) if len(item) > a_idx else 0
            
            return {
                "date": date_int,
                "time": time_val,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "vol": v,
                "amount": a
            }
    except Exception as e:
        zt_debug(f"Parse bar item failed: {e}")
        return None


def _fetch_bars(api, ktype: int, market: int, code: str, 
                start_int: int, end_int: int, is_index: bool = False) -> list:
    """
    分页获取 K 线数据，并过滤日期范围
    返回按时间升序排列的 bar 列表
    """
    all_bars = []
    pos = 0
    count = 800  # 每次请求800根K线（最大值）
    
    while True:
        try:
            # 指数使用专门的接口获取K线
            if is_index:
                bars = api.get_index_bars(ktype, market, code, pos, count)
            else:
                bars = api.get_security_bars(ktype, market, code, pos, count)
        except Exception as e:
            zt_warn(f"get_bars failed for code={code} pos={pos}: {e}")
            break
        
        if not bars:
            break
        
        # 解析每条 bar
        for item in bars:
            parsed = _parse_bar_item(item, ktype)
            if parsed is None or parsed["date"] == 0:
                continue
            
            # 日期范围过滤
            if parsed["date"] < start_int:
                # pytdx 返回降序，如果日期小于起始日期，后面的数据更早，可以直接跳出
                # 但为了安全，收集所有数据最后统一过滤
                continue
            if parsed["date"] > end_int:
                continue
            
            all_bars.append(parsed)
        
        # 判断是否还有更多数据
        actual_count = len(bars)
        if actual_count < count:
            break
        
        pos += actual_count
        
        # 安全检查：避免无限循环
        if pos > 100000:
            zt_warn(f"Too many bars fetched for code={code}, breaking")
            break
    
    # 去重并按时间升序排列（pytdx 返回的是降序）
    seen = set()
    unique_bars = []
    for bar in all_bars:
        key = (bar["date"], bar["time"])
        if key not in seen:
            seen.add(key)
            unique_bars.append(bar)
    
    unique_bars.sort(key=lambda x: (x["date"], x["time"]))
    
    return unique_bars


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
        """
        连接 TDX 服务器
        @host   TDX 行情服务器地址
        @port   TDX 行情服务器端口
        """
        if host:
            self.host = host
        if port:
            self.port = port
        
        try:
            # 启用心跳和自动重连
            self.api = TdxHq_API(heartbeat=True, auto_retry=True)
            ok = self.api.connect(self.host, self.port, time_out=5)
            if not ok:
                raise Exception(f"Failed to connect to {self.host}:{self.port}")
            
            self.isAuthed = True
            zt_info(f"Tdx has been authorized, connected to {self.host}:{self.port}")
        except Exception as e:
            zt_error(f"Failed to connect to TDX server {self.host}:{self.port}: {e}")
            raise

    def dmpCodeListToFile(self, filename: str, hasIndex: bool = True, hasStock: bool = True):
        """
        导出代码列表到 JSON 文件
        """
        self.__check__()
        
        # 刷新代码缓存，以便更精确判断
        try:
            _refresh_code_cache()
        except Exception as e:
            zt_warn(f"Failed to refresh code cache: {e}")
        
        stocks = {
            "SSE": {},
            "SZSE": {}
        }
        
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
                    
                    # 根据参数过滤
                    if pid == "IDX" and not hasIndex:
                        continue
                    if pid in ("STK", "ETF") and not hasStock:
                        continue
                    
                    sInfo = {
                        "exchg": exchg,
                        "code": code,
                        "name": name,
                        "product": pid
                    }
                    stocks[exchg][code] = sInfo
                
                actual_count = len(items)
                start += actual_count
                if actual_count < page_size:
                    break
    
        zt_info(f"Writing code list into file {filename}...")
        os.makedirs(os.path.dirname(filename) if os.path.dirname(filename) else ".", exist_ok=True)
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(stocks, f, sort_keys=True, indent=4, ensure_ascii=False)


    def dmpAdjFactorsToFile(self, codes: list, filename: str):
        """
        导出复权因子到文件
        使用 pytdx 的 get_xdxr_info 接口获取除权除息信息
        """
        self.__check__()
        zt_info("Fetching adjust factors using get_xdxr_info...")
        
        stocks = {"SSE": {}, "SZSE": {}}
        
        count = 0
        total = len(codes)
        
        for stdCode in codes:
            try:
                market, code = _std_to_tdx_market(stdCode)
            except NotImplementedError:
                zt_warn(f"Skip unsupported code {stdCode}")
                continue
            
            exchg = stdCode.split(".")[0]
            count += 1
            
            zt_info(f"Fetching adjust factors of {stdCode}({count}/{total})...")
            
            try:
                # get_xdxr_info 返回除权除息信息
                xdxr_data = self.api.get_xdxr_info(market, code)
                
                factors = []
                if xdxr_data:
                    for item in xdxr_data:
                        try:
                            if isinstance(item, dict):
                                date = item.get("date", item.get("exdate", ""))
                                factor = item.get("factor", item.get("adj_factor", 1.0))
                            else:
                                # 通常是 (date, ..., factor, ...) 格式
                                # 具体字段需要根据实际返回格式调整
                                date = str(item[0]) if len(item) > 0 else ""
                                factor = float(item[-1]) if len(item) > 0 else 1.0
                            
                            if date:
                                date_int = int(str(date).replace("-", ""))
                                factors.append({
                                    "date": date_int,
                                    "factor": float(factor)
                                })
                        except Exception as e:
                            zt_debug(f"Parse xdxr item failed: {e}")
                            continue
                
                # 如果没有获取到复权因子，添加默认值
                if not factors:
                    factors.append({"date": 19900101, "factor": 1.0})
                
                # 按日期排序
                factors.sort(key=lambda x: x["date"])
                
                # 去除连续的重复因子（与 Tushare 实现保持一致）
                cleaned_factors = []
                prev_factor = None
                for f in factors:
                    if f["factor"] != prev_factor:
                        cleaned_factors.append(f)
                        prev_factor = f["factor"]
                
                if exchg not in stocks:
                    stocks[exchg] = {}
                stocks[exchg][code] = cleaned_factors
                
            except Exception as e:
                zt_warn(f"Failed to get xdxr info for {stdCode}: {e}")
                # 如果获取失败，写入默认因子
                if exchg not in stocks:
                    stocks[exchg] = {}
                stocks[exchg][code] = [{"date": 19900101, "factor": 1.0}]
        
        zt_info(f"Writing adjust factors into file {filename}...")
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(stocks, f, sort_keys=True, indent=4, ensure_ascii=False)

    def dmpBarsToFile(self, folder: str, codes: list, start_date: datetime = None, 
                      end_date: datetime = None, period: str = "day"):
        """
        导出 K 线到 CSV 文件
        """
        self.__check__()
        os.makedirs(folder, exist_ok=True)
        
        if start_date is None:
            start_date = datetime(1990, 1, 1)
        if end_date is None:
            end_date = datetime.now()
        
        # 周期映射（使用 pytdx 的正确 K 线类型）
        # 根据文档: 0=5分钟, 7/8=1分钟, 9=日K线
        kmap = {
            "day": 9,      # TDXParams.KLINE_TYPE_DAILY = 9
            "min1": 8,     # TDXParams.KLINE_TYPE_1MIN = 8
            "min5": 0,     # TDXParams.KLINE_TYPE_5MIN = 0
        }
        if period not in kmap:
            raise Exception(f"Unrecognized period: {period}, supported: day, min1, min5")
        ktype = kmap[period]
        
        is_day = (period == "day")
        filetag = "d" if is_day else ("m1" if period == "min1" else "m5")
        
        start_int = int(start_date.strftime("%Y%m%d"))
        end_int = int(end_date.strftime("%Y%m%d"))
        
        count = 0
        total = len(codes)
        
        for stdCode in codes:
            try:
                market, code = _std_to_tdx_market(stdCode)
            except NotImplementedError:
                zt_warn(f"Skip unsupported code {stdCode}")
                continue
            
            exchg = stdCode.split(".")[0]
            count += 1
            
            zt_info(f"Fetching {period} bars of {stdCode}({count}/{total})...")
            
            is_index = _is_index(code, market)
            bars = _fetch_bars(self.api, ktype, market, code, start_int, end_int, is_index)
            
            # 构建 CSV 内容
            content = "date,time,open,high,low,close,volume,turnover\n"
            for bar in bars:
                date_str = str(bar["date"])
                time_str = str(bar["time"]) if not is_day else "0"
                o = str(bar["open"])
                h = str(bar["high"])
                l = str(bar["low"])
                c = str(bar["close"])
                v = str(int(bar["vol"]))
                t = str(int(bar["amount"]))
                
                content += f"{date_str},{time_str},{o},{h},{l},{c},{v},{t}\n"
            
            filename = f"{stdCode}_{filetag}.csv"
            filepath = os.path.join(folder, filename)
            zt_info(f"Writing bars into file {filepath}...")
            
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

    def dmpAdjFactorsToDB(self, dbHelper: DBHelper, codes: list):
        """
        将复权因子写入数据库
        """
        self.__check__()
        zt_info("Fetching adjust factors for database using get_xdxr_info...")
        
        stocks = {"SSE": {}, "SZSE": {}}
        
        count = 0
        total = len(codes)
        
        for stdCode in codes:
            try:
                market, code = _std_to_tdx_market(stdCode)
            except NotImplementedError:
                zt_warn(f"Skip unsupported code {stdCode}")
                continue
            
            exchg = stdCode.split(".")[0]
            count += 1
            
            zt_info(f"Fetching adjust factors of {stdCode}({count}/{total})...")
            
            try:
                xdxr_data = self.api.get_xdxr_info(market, code)
                
                factors = []
                if xdxr_data:
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
                                factors.append({
                                    "date": date_int,
                                    "factor": float(factor)
                                })
                        except Exception:
                            continue
                
                if not factors:
                    factors.append({"date": 19900101, "factor": 1.0})
                
                factors.sort(key=lambda x: x["date"])
                
                cleaned_factors = []
                prev_factor = None
                for f in factors:
                    if f["factor"] != prev_factor:
                        cleaned_factors.append(f)
                        prev_factor = f["factor"]
                
                if exchg not in stocks:
                    stocks[exchg] = {}
                stocks[exchg][code] = cleaned_factors
                
            except Exception as e:
                zt_warn(f"Failed to get xdxr info for {stdCode}: {e}")
                if exchg not in stocks:
                    stocks[exchg] = {}
                stocks[exchg][code] = [{"date": 19900101, "factor": 1.0}]
        
        zt_info("Writing adjust factors into database...")
        dbHelper.writeFactors(stocks)

    def dmpBarsToDB(self, dbHelper: DBHelper, codes: list, start_date: datetime = None,
                    end_date: datetime = None, period: str = "day"):
        """
        导出 K 线到数据库
        """
        self.__check__()
        
        if start_date is None:
            start_date = datetime(1990, 1, 1)
        if end_date is None:
            end_date = datetime.now()
        
        kmap = {
            "day": 9,
            "min1": 8,
            "min5": 0,
        }
        if period not in kmap:
            raise Exception(f"Unrecognized period: {period}")
        ktype = kmap[period]
        
        is_day = (period == "day")
        
        start_int = int(start_date.strftime("%Y%m%d"))
        end_int = int(end_date.strftime("%Y%m%d"))
        
        count = 0
        total = len(codes)
        
        for stdCode in codes:
            try:
                market, code = _std_to_tdx_market(stdCode)
            except NotImplementedError:
                zt_warn(f"Skip unsupported code {stdCode}")
                continue
            
            exchg = stdCode.split(".")[0]
            count += 1
            
            zt_info(f"Fetching {period} bars of {stdCode}({count}/{total})...")
            
            is_index = _is_index(code, market)
            bars = _fetch_bars(self.api, ktype, market, code, start_int, end_int, is_index)
            
            # 转换为数据库格式（使用 ZTSBarStruct 的字段名）
            db_bars = []
            for bar in bars:
                if is_day:
                    time_val = 0
                else:
                    # 分钟线时间格式：time + (date - 19900000) * 10000
                    time_val = bar["time"] + (bar["date"] - 19900000) * 10000
                
                db_bars.append({
                    "exchange": exchg,
                    "code": code,
                    "date": bar["date"],
                    "time": time_val,
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["vol"],     # ZTSBarStruct 中使用 vol 字段
                    "turnover": bar["amount"]  # ZTSBarStruct 中使用 money 字段
                })
            
            zt_info(f"Writing {len(db_bars)} bars into database...")
            dbHelper.writeBars(db_bars, period)

    def dmpBars(self, codes: list, cb, start_date: datetime = None, 
                end_date: datetime = None, period: str = "day"):
        """
        通过回调函数传递 K 线数据
        @cb   回调函数: cb(exchg, code, buffer, count, period)
        其中 buffer 是 ZTSBarStruct 数组      
        """
        self.__check__()
        
        if start_date is None:
            start_date = datetime(1990, 1, 1)
        if end_date is None:
            end_date = datetime.now()
        
        kmap = {
            "day": 9,
            "min1": 8,
            "min5": 0,
        }
        if period not in kmap:
            raise Exception(f"Unrecognized period: {period}")
        ktype = kmap[period]
        
        is_day = (period == "day")
        
        start_int = int(start_date.strftime("%Y%m%d"))
        end_int = int(end_date.strftime("%Y%m%d"))
        
        count = 0
        total = len(codes)
        
        for stdCode in codes:
            try:
                market, code = _std_to_tdx_market(stdCode)
            except NotImplementedError:
                zt_warn(f"Skip unsupported code {stdCode}")
                continue
            
            exchg = stdCode.split(".")[0]
            count += 1
            
            zt_info(f"Fetching {period} bars of {stdCode}({count}/{total})...")
            
            is_index = _is_index(code, market)
            bars = _fetch_bars(self.api, ktype, market, code, start_int, end_int, is_index)
            
            if not bars:
                continue
            
            # 构建 ZTSBarStruct 数组
            bar_list = []
            for bar in bars:
                cur_bar = ZTSBarStruct()
                cur_bar.date = bar["date"]
                
                if is_day:
                    # 日线：time 字段为 0（根据 to_tuple 中 flag=1 时 time = self.date，但这里设置为 0）
                    # 参考 Baostock 实现，日线 time 设为 0
                    cur_bar.time = 0
                else:
                    # 分钟线：time = bar["time"] + (date - 19900000) * 10000
                    # 这与 to_tuple 中 flag=0 时的逻辑一致：time = self.time + 199000000000
                    cur_bar.time = bar["time"] + (bar["date"] - 19900000) * 10000
                
                cur_bar.open = bar["open"]
                cur_bar.high = bar["high"]
                cur_bar.low = bar["low"]
                cur_bar.close = bar["close"]
                cur_bar.vol = bar["vol"]       # ZTSBarStruct 使用 vol 字段
                cur_bar.money = bar["amount"]  # ZTSBarStruct 使用 money 字段
                
                # 以下字段 pytdx 不提供，设为 0
                cur_bar.settle = 0
                cur_bar.hold = 0
                cur_bar.diff = 0
                
                bar_list.append(cur_bar)
            
            # 创建 C 数组并复制数据
            from ctypes import addressof
            BUFFER = ZTSBarStruct * len(bar_list)
            buffer = BUFFER()
            for i in range(len(bar_list)):
                src = bar_list[i]
                dst = buffer[i]
                dst.date = src.date
                dst.time = src.time
                dst.open = src.open
                dst.high = src.high
                dst.low = src.low
                dst.close = src.close
                dst.settle = src.settle
                dst.money = src.money
                dst.vol = src.vol
                dst.hold = src.hold
                dst.diff = src.diff
            
            cb(exchg, code, buffer, len(bar_list), period)