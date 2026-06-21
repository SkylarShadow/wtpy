"""基于 pytdx 原始接口的历史行情抓取、校验与 ztpy 记录转换。"""

from bisect import bisect_left
from datetime import date, datetime, timedelta

from ztpy.ZtCoreDefs import ZTSBarStruct
from ztpy.apps.datahelper.data.tdx_catalog import is_index as infer_index, period_spec
from ztpy.utils.ztlog import zt_debug, zt_warn


# pytdx K 线接口单次最多取 800 条；最多翻 100 页
PAGE_SIZE = 800
MAX_PAGES = 100
TRANS_PAGE_SIZE = 2000
TRANS_MAX_PAGES = 21


def _request(api, method, *args):
    """调用 pytdx 接口，失败后重试一次并统一记录最终错误。"""
    func = getattr(api, method)
    for attempt in range(2):
        try:
            return func(*args)
        except Exception as exc:
            if attempt:
                zt_warn("{} failed for {}: {}", method, args, exc)
    return None


def parse_bar(item, is_day):
    """将 pytdx K 线字典转换为内部格式，同时过滤畸形价格和成交数据。"""
    try:
        year, month, day = int(item["year"]), int(item["month"]), int(item["day"])
        bar = {
            "date": year * 10000 + month * 100 + day,
            "time": 0 if is_day else int(item.get("hour", 0)) * 100 + int(item.get("minute", 0)),
            "open": float(item["open"]), "high": float(item["high"]),
            "low": float(item["low"]), "close": float(item["close"]),
            "volume": float(item["vol"]), "turnover": float(item["amount"]),
        }
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        zt_debug("Ignore malformed pytdx bar {}: {}", item, exc)
        return None
    if (bar["date"] <= 0 or bar["low"] <= 0 or
            not bar["low"] <= bar["open"] <= bar["high"] or
            not bar["low"] <= bar["close"] <= bar["high"] or
            bar["volume"] < 0 or bar["turnover"] < 0):
        zt_debug("Ignore invalid pytdx bar: {}", item)
        return None
    return bar


def fetch_bars(api, market, code, period, start_date, end_date, is_index=None):
    """从最新记录向历史方向分页抓取指定日期范围的 K 线。

    指数与普通证券使用不同的 pytdx 接口。若目录分类有误导致首批为空，
    会自动尝试另一接口；结果按日期和时间去重、升序返回。
    """
    ktype, _, is_day = period_spec(period)
    if is_index is None:
        is_index = infer_index(market, code)
    primary = "get_index_bars" if is_index else "get_security_bars"
    secondary = "get_security_bars" if is_index else "get_index_bars"
    result = {}
    for page in range(MAX_PAGES):
        # pytdx 的 offset=0 表示最新一页，offset 越大数据越早。
        offset = page * PAGE_SIZE
        rows = _request(api, primary, ktype, market, code, offset, PAGE_SIZE)
        if page == 0 and not rows:
            rows = _request(api, secondary, ktype, market, code, offset, PAGE_SIZE)
            if rows:
                primary = secondary
        if not rows:
            break
        page_dates = []
        for item in rows:
            bar = parse_bar(item, is_day)
            if bar is None:
                continue
            page_dates.append(bar["date"])
            if start_date <= bar["date"] <= end_date:
                result[bar["date"], bar["time"]] = bar
        if page_dates and min(page_dates) < start_date:
            break
        if len(rows) < PAGE_SIZE:
            break
    else:
        zt_warn("Reached pytdx history limit for {} ({} pages)", code, MAX_PAGES)
    return [result[key] for key in sorted(result)], is_day


def encoded_bar_time(bar, is_day):
    """将分钟时间编码为 ztpy 使用的相对 1990 年整数格式。"""
    return 0 if is_day else (bar["date"] - 19900000) * 10000 + bar["time"]


def bars_to_c_buffer(bars, is_day):
    """把内部 K 线列表复制到连续的 ZTSBarStruct C 数组。"""
    buffer = (ZTSBarStruct * len(bars))()
    for dst, src in zip(buffer, bars):
        dst.date = src["date"]
        dst.time = encoded_bar_time(src, is_day)
        dst.open, dst.high = src["open"], src["high"]
        dst.low, dst.close = src["low"], src["close"]
        dst.vol, dst.money = src["volume"], src["turnover"]
        dst.settle = dst.hold = dst.diff = 0
    return buffer


def adjust_factors(api, market, code):
    """根据通达信除权除息事件和事件前收盘价计算累计复权因子。"""
    try:
        events = api.get_xdxr_info(market, code) or []
    except Exception as exc:
        zt_warn("Failed to get corporate actions for {}: {}", code, exc)
        return [{"date": 19900101, "factor": 1.0}]

    actions = []
    for event in events:
        try:
            if int(event.get("category", 1)) != 1:
                continue
            action_date = (int(event["year"]) * 10000 + int(event["month"]) * 100 +
                           int(event["day"]))
            actions.append((action_date, event))
        except (KeyError, TypeError, ValueError):
            continue
    if not actions:
        return [{"date": 19900101, "factor": 1.0}]

    daily, _ = fetch_bars(api, market, code, "day", 19900101,
                          datetime.now().year * 10000 + 1231, is_index=False)
    dates = [bar["date"] for bar in daily]
    # 以 1.0 为初始基准，按事件日期正序累积前复权比例的倒数。
    factors = [{"date": 19900101, "factor": 1.0}]
    cumulative = 1.0
    for action_date, event in sorted(actions):
        pos = bisect_left(dates, action_date) - 1
        if pos < 0:
            continue
        previous_close = daily[pos]["close"]
        gift = float(event.get("songzhuangu") or 0.0) / 10.0
        rights = float(event.get("peigu") or 0.0) / 10.0
        rights_price = float(event.get("peigujia") or 0.0)
        cash = float(event.get("fenhong") or 0.0) / 10.0
        denominator = previous_close * (1.0 + gift + rights)
        ratio = ((previous_close - cash + rights * rights_price) / denominator
                 if denominator > 0 else 0.0)
        if ratio <= 0:
            continue
        cumulative /= ratio
        if abs(cumulative - factors[-1]["factor"]) > 1e-10:
            factors.append({"date": action_date, "factor": round(cumulative, 10)})
    return factors


def trading_dates(start_date, end_date):
    """生成日期范围内的工作日；实际休市日会由空响应自然跳过。"""
    start = date(start_date // 10000, start_date // 100 % 100, start_date % 100)
    end = date(end_date // 10000, end_date // 100 % 100, end_date % 100)
    result = []
    while start <= end:
        if start.weekday() < 5:
            result.append(start.year * 10000 + start.month * 100 + start.day)
        start += timedelta(days=1)
    return result


def fetch_day_transactions(api, market, code, trading_date):
    """分页读取单日逐笔成交，并转换为带近似秒序的 ztpy 记录。"""
    pages = []
    for page in range(TRANS_MAX_PAGES):
        rows = _request(api, "get_history_transaction_data", market, code,
                        page * TRANS_PAGE_SIZE, TRANS_PAGE_SIZE, trading_date)
        if not rows:
            break
        pages.insert(0, rows)
        if len(rows) < TRANS_PAGE_SIZE:
            break

    # TDX 历史逐笔只有分钟粒度，沿用 Hikyuu 思路以 3 秒间隔构造稳定顺序。
    records, previous_minute, second = [], None, 2
    for item in (record for page in pages for record in page):
        try:
            hour, minute = (int(value) for value in str(item["time"]).split(":")[:2])
            hhmm = hour * 100 + minute
            if hhmm != previous_minute:
                second = 0 if hhmm == 1500 else 2
                previous_minute = hhmm
            else:
                second += 3
            if second > 59:
                continue
            price, volume = float(item["price"]), float(item["vol"])
            if price <= 0 or volume < 0:
                continue
            hhmmss = hhmm * 100 + second
            records.append({
                "date": trading_date, "time": hhmmss,
                "datetime": trading_date * 1000000 + hhmmss,
                "price": price, "volume": volume,
                "side": int(item.get("buyorsell", 0)), "index": len(records),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return records


def fetch_transactions(api, market, code, start_date, end_date):
    """按工作日汇总日期范围内的历史逐笔成交。"""
    result = []
    for trading_date in trading_dates(start_date, end_date):
        result.extend(fetch_day_transactions(api, market, code, trading_date))
    return result


def _minute_labels():
    """生成 A 股连续竞价时段对应的 240 个 HHMM 标签。"""
    labels = []
    for hour, start, stop in ((9, 30, 60), (10, 0, 60), (11, 0, 30),
                              (13, 0, 60), (14, 0, 60)):
        labels.extend(hour * 100 + minute for minute in range(start, stop))
    return labels


MINUTE_LABELS = _minute_labels()


def fetch_day_minute_time(api, market, code, trading_date):
    """读取单日 240 点分时；记录不完整时整日丢弃以避免时间错位。"""
    rows = _request(api, "get_history_minute_time_data", market, code, trading_date)
    if not rows:
        return []
    if len(rows) != len(MINUTE_LABELS):
        zt_warn("Ignore incomplete minute-time data for {} on {}: {} records",
                code, trading_date, len(rows))
        return []
    records = []
    for hhmm, item in zip(MINUTE_LABELS, rows):
        try:
            price, volume = float(item["price"]), float(item["vol"])
            if price > 0 and volume >= 0:
                records.append({
                    "date": trading_date, "time": hhmm,
                    "datetime": trading_date * 10000 + hhmm,
                    "price": price, "volume": volume,
                })
        except (KeyError, TypeError, ValueError):
            continue
    return records


def fetch_minute_time(api, market, code, start_date, end_date):
    """按工作日汇总日期范围内的历史 240 点分时数据。"""
    result = []
    for trading_date in trading_dates(start_date, end_date):
        result.extend(fetch_day_minute_time(api, market, code, trading_date))
    return result
