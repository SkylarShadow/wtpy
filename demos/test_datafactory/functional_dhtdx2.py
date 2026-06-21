"""End-to-end functional test for DHTdx2 against a real TDX server.

This file is intentionally not named test_*.py, so normal unit-test discovery
never performs network requests. Run it directly and inspect summary.json plus
the generated CSV/JSON files under the selected output directory.
"""

import argparse
import csv
import json
import os
import sys
from datetime import date, timedelta

# Running this file directly puts tests/datahelper at sys.path[0]. Prefer the
# current checkout over any older ztpy package installed in the environment.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from ztpy.apps.datahelper.DHTdx2 import DHTdx
from ztpy.apps.datahelper.data.tdx_bulk import import_all_bars


DEFAULT_CODES = ("SSE.600000", "SZSE.000001", "SSE.000001")
BAR_FIELDS = {
    "exchange", "code", "date", "time", "open", "high", "low", "close",
    "volume", "turnover",
}


def yyyymmdd(value):
    return value.year * 10000 + value.month * 100 + value.day


class FunctionalDB:
    """DBHelper-compatible sink that validates records without requiring MySQL."""

    def __init__(self):
        self.period_counts = {}
        self.samples = {}
        self.factor_count = 0

    def writeBars(self, bars, period="day"):
        if not bars:
            raise AssertionError("DBHelper received an empty bar batch")
        for bar in bars:
            missing = BAR_FIELDS.difference(bar)
            if missing:
                raise AssertionError("bar is missing fields: {}".format(sorted(missing)))
            if not (bar["low"] <= bar["open"] <= bar["high"] and
                    bar["low"] <= bar["close"] <= bar["high"]):
                raise AssertionError("invalid OHLC values: {!r}".format(bar))
        self.period_counts[period] = self.period_counts.get(period, 0) + len(bars)
        self.samples.setdefault(period, bars[0])

    def writeFactors(self, factors):
        for exchange in factors.values():
            for items in exchange.values():
                self.factor_count += len(items)


def assert_csv_has_data(filename, expected_header):
    if not os.path.isfile(filename):
        raise AssertionError("expected output file does not exist: {}".format(filename))
    with open(filename, "r", encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream)
        rows = list(reader)
    if not rows or rows[0] != list(expected_header):
        raise AssertionError("unexpected CSV header in {}".format(filename))
    if len(rows) < 2:
        raise AssertionError("CSV contains no data rows: {}".format(filename))
    return len(rows) - 1


def recent_weekdays(days=15):
    current = date.today() - timedelta(days=1)
    result = []
    while len(result) < days:
        if current.weekday() < 5:
            result.append(yyyymmdd(current))
        current -= timedelta(days=1)
    return result


def probe_intraday(helper, code):
    result = {
        "code": code,
        "transactions": {"available": False, "date": None, "count": 0},
        "minute_time": {"available": False, "date": None, "count": 0},
    }
    for trading_date in recent_weekdays():
        if not result["transactions"]["available"]:
            rows = helper.getTransactions(code, trading_date)
            if rows:
                result["transactions"] = {
                    "available": True, "date": trading_date, "count": len(rows),
                }
        if not result["minute_time"]["available"]:
            rows = helper.getMinuteTime(code, trading_date)
            if rows:
                result["minute_time"] = {
                    "available": True, "date": trading_date, "count": len(rows),
                }
        if (result["transactions"]["available"] and
                result["minute_time"]["available"]):
            break
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("ZTPY_TDX_HOST"),
                        help="TDX host; omit to use automatic best-server discovery")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("ZTPY_TDX_PORT", "7709")))
    parser.add_argument("--output", default=os.path.join(
        "tests", "output", "dhtdx2_functional"))
    parser.add_argument("--codes", nargs="+", default=list(DEFAULT_CODES))
    parser.add_argument("--periods", nargs="+", default=["day"])
    parser.add_argument("--days", type=int, default=60,
                        help="calendar-day history window for sample bar checks")
    parser.add_argument("--strict-intraday", action="store_true",
                        help="fail when historical transaction/minute data is unavailable")
    parser.add_argument("--workers", type=int, default=10,
                        help="parallel TDX connections used by concurrent checks")
    parser.add_argument("--full-market", action="store_true",
                        help="also export all discovered stocks; this can take a long time")
    return parser.parse_args(argv)


def run(args):
    if args.days < 7:
        raise ValueError("--days must be at least 7")
    os.makedirs(args.output, exist_ok=True)
    sample_dir = os.path.join(args.output, "sample_bars")
    os.makedirs(sample_dir, exist_ok=True)

    end_value = date.today()
    start_value = end_value - timedelta(days=args.days)
    start_date, end_date = yyyymmdd(start_value), yyyymmdd(end_value)
    helper = DHTdx()
    summary = {
        "server": {}, "date_range": [start_date, end_date],
        "catalog": {}, "csv": {}, "db": {}, "callback": {},
        "factors": {}, "intraday": {}, "full_market": None,
        "workers": args.workers, "concurrent": {},
    }

    try:
        if args.host:
            helper.auth(host=args.host, port=args.port)
        else:
            helper.auth()
        summary["server"] = {"host": helper.host, "port": helper.port}

        stock_codes = helper.getCodeList(hasIndex=False, products=("STK",))
        index_codes = helper.getCodeList(
            hasIndex=True, hasStock=False, products=(),
        )
        if not stock_codes:
            raise AssertionError("live security catalog contains no stocks")
        if not index_codes:
            raise AssertionError("live security catalog contains no indexes")
        summary["catalog"] = {
            "stocks": len(stock_codes), "indexes": len(index_codes),
            "first_stock": stock_codes[0], "first_index": index_codes[0],
        }

        catalog_file = os.path.join(args.output, "stocks.json")
        helper.dmpCodeListToFile(catalog_file, hasIndex=True, hasStock=True)
        with open(catalog_file, "r", encoding="utf-8") as stream:
            catalog_payload = json.load(stream)
        if not catalog_payload.get("SSE") or not catalog_payload.get("SZSE"):
            raise AssertionError("stocks.json does not contain both SSE and SZSE")

        db = FunctionalDB()
        callback_counts = {}

        def on_bars(exchange, code, buffer, count, period):
            if count <= 0 or len(buffer) != count:
                raise AssertionError("invalid callback buffer for {}.{}".format(exchange, code))
            first = buffer[0]
            if first.low <= 0 or first.high < first.low:
                raise AssertionError("invalid callback OHLC for {}.{}".format(exchange, code))
            callback_counts[period] = callback_counts.get(period, 0) + count

        for period in args.periods:
            helper.dmpBarsToFile(
                sample_dir, args.codes, start_date, end_date, period=period,
            )
            helper.dmpBarsToDB(
                db, args.codes, start_date, end_date, period=period,
            )
            helper.dmpBars(
                args.codes, on_bars, start_date, end_date, period=period,
            )

            suffix = {"day": "d", "min5": "m5", "min1": "m1"}.get(period)
            if suffix is None:
                from ztpy.apps.datahelper.data.tdx_catalog import period_spec
                suffix = period_spec(period)[1]
            for std_code in args.codes:
                exchange, code = std_code.split(".")[0], std_code.split(".")[-1]
                filename = os.path.join(
                    sample_dir, "{}.{}_{}.csv".format(exchange, code, suffix),
                )
                row_count = assert_csv_has_data(filename, (
                    "date", "time", "open", "high", "low", "close",
                    "volume", "turnover",
                ))
                summary["csv"]["{}:{}".format(std_code, period)] = row_count

        summary["db"] = {
            "period_counts": db.period_counts, "samples": db.samples,
        }
        summary["callback"] = callback_counts
        for period in args.periods:
            if db.period_counts.get(period, 0) <= 0:
                raise AssertionError("DBHelper path wrote no {} bars".format(period))
            if callback_counts.get(period, 0) <= 0:
                raise AssertionError("callback path received no {} bars".format(period))

        concurrent_counts = {}

        def consume_concurrent(_, period, exchange, code, bars, is_day):
            if not bars:
                raise AssertionError(
                    "concurrent loader returned no bars for {}.{}".format(
                        exchange, code
                    )
                )
            concurrent_counts[period] = concurrent_counts.get(period, 0) + len(bars)

        concurrent_report = import_all_bars(
            args.codes, args.periods, None, consume_concurrent,
            workers=args.workers,
            loader_factory=helper._bar_loader_factory(start_date, end_date),
        )
        expected_tasks = len(args.codes) * len(args.periods)
        if (concurrent_report["succeeded"] != expected_tasks or
                concurrent_report["failed"] or concurrent_report["empty"]):
            raise AssertionError(
                "concurrent download smoke test failed: {!r}".format(
                    concurrent_report
                )
            )
        summary["concurrent"] = {
            "report": concurrent_report, "period_counts": concurrent_counts,
        }

        factor_code = next((code for code in args.codes if not code.endswith("000001")),
                           args.codes[0])
        factor_file = os.path.join(args.output, "factors.json")
        helper.dmpAdjFactorsToFile([factor_code], factor_file)
        with open(factor_file, "r", encoding="utf-8") as stream:
            factor_payload = json.load(stream)
        exchange, code = factor_code.split(".")[0], factor_code.split(".")[-1]
        factors = factor_payload.get(exchange, {}).get(code, [])
        if not factors:
            raise AssertionError("no adjustment factor result for {}".format(factor_code))
        helper.dmpAdjFactorsToDB(db, [factor_code])
        if db.factor_count <= 0:
            raise AssertionError("DBHelper path received no adjustment factors")
        summary["factors"] = {
            "code": factor_code, "file_count": len(factors),
            "db_count": db.factor_count,
        }

        intraday_code = next((code for code in args.codes if code.startswith("SSE.6")),
                             args.codes[0])
        summary["intraday"] = probe_intraday(helper, intraday_code)
        if args.strict_intraday:
            if not summary["intraday"]["transactions"]["available"]:
                raise AssertionError("historical transaction data is unavailable")
            if not summary["intraday"]["minute_time"]["available"]:
                raise AssertionError("historical minute-time data is unavailable")

        if args.full_market:
            full_dir = os.path.join(args.output, "full_market")
            summary["full_market"] = helper.dmpAllBarsToFile(
                full_dir, start_date, end_date, periods=tuple(args.periods),
                hasIndex=False, products=("STK",),
                progress=lambda current, total: print(
                    "\rFull market: {}/{}".format(current, total),
                    end="", flush=True,
                ),
                workers=args.workers,
            )
            print()
            if summary["full_market"]["failed"]:
                raise AssertionError("full-market import contains failures")
    finally:
        helper.close()

    summary_file = os.path.join(args.output, "summary.json")
    with open(summary_file, "w", encoding="utf-8") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2, sort_keys=True)
    return summary_file, summary


def main(argv=None):
    args = parse_args(argv)
    try:
        summary_file, summary = run(args)
    except Exception as exc:
        print("FUNCTIONAL TEST FAILED: {}".format(exc), file=sys.stderr)
        return 1
    print("FUNCTIONAL TEST PASSED")
    print("Server: {host}:{port}".format(**summary["server"]))
    print("Catalog: {stocks} stocks, {indexes} indexes".format(**summary["catalog"]))
    print("Summary: {}".format(os.path.abspath(summary_file)))
    if not summary["intraday"]["transactions"]["available"]:
        print("Note: historical transaction data was unavailable on this server")
    if not summary["intraday"]["minute_time"]["available"]:
        print("Note: historical minute-time data was unavailable on this server")
    return 0


if __name__ == "__main__":
    sys.exit(main())
