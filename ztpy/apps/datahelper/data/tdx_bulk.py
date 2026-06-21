"""pytdx 各种输出方式共用的全市场 K 线导入编排。"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Lock, local

from ztpy.utils.ztlog import zt_info, zt_warn


def _record_failure(report, std_code, period, exc):
    report["failed"] += 1
    report["failures"].append({
        "code": std_code, "period": period, "error": str(exc),
    })
    zt_warn("Failed to import {} {} bars: {}", std_code, period, exc)


def _record_result(report, writer, std_code, period, result):
    exchange, code, bars, is_day = result
    if bars:
        # writer 始终由调度线程执行，DBHelper 和用户回调不需要具备线程安全性。
        writer(std_code, period, exchange, code, bars, is_day)
        report["succeeded"] += 1
        report["bars"] += len(bars)
    else:
        report["empty"] += 1


def _close_workers(resources):
    for resource in resources:
        close = getattr(resource, "close", None)
        if close is None:
            continue
        try:
            close()
        except Exception as exc:
            zt_warn("Failed to close a TDX bulk worker: {}", exc)


def _import_concurrently(tasks, worker_count, loader_factory, writer, report,
                         progress, failure_limit):
    """使用线程本地长连接下载，并在主调度线程中消费结果。"""
    state = local()
    resources = []
    resources_lock = Lock()

    def load(task):
        if not hasattr(state, "loader"):
            state.loader = loader_factory()
            with resources_lock:
                resources.append(state.loader)
        std_code, period = task
        return state.loader(std_code, period)

    task_iter = iter(tasks)
    in_flight = {}
    consecutive_failures = 0
    executor = ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="ztpy-tdx"
    )
    try:
        # 限制等待队列，避免全市场分钟线下载速度快于落盘时占用过多内存。
        max_in_flight = worker_count * 2

        def submit_next():
            try:
                task = next(task_iter)
            except StopIteration:
                return False
            in_flight[executor.submit(load, task)] = task
            return True

        for _ in range(max_in_flight):
            if not submit_next():
                break

        while in_flight:
            completed, _ = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
            for future in completed:
                std_code, period = in_flight.pop(future)
                try:
                    _record_result(
                        report, writer, std_code, period, future.result()
                    )
                    # 有响应（包括空数据）说明至少一个工作连接仍然可用。
                    consecutive_failures = 0
                except Exception as exc:
                    _record_failure(report, std_code, period, exc)
                    consecutive_failures += 1

                report["completed"] += 1
                if progress is not None:
                    progress(report["completed"], report["total"])

                if (failure_limit is not None and
                        consecutive_failures >= failure_limit):
                    report["stopped"] = True
                    zt_warn("Stopped bulk TDX import after {} consecutive failures",
                            consecutive_failures)
                    for pending in in_flight:
                        pending.cancel()
                    return
                submit_next()
    finally:
        executor.shutdown(wait=True)
        _close_workers(resources)


def import_all_bars(codes, periods, loader, writer, progress=None,
                    failure_limit=20, workers=1, loader_factory=None):
    """依次处理每个代码与周期组合，并返回可序列化的导入报告。

    loader 只负责获取并标准化行情，writer 负责写文件、数据库或回调，
    因而批量控制逻辑不依赖具体存储方式。progress 接收 (completed, total)。
    failure_limit 表示允许的连续失败次数，传 None 可关闭自动中止。
    workers 大于 1 时必须提供 loader_factory，每个线程会通过它建立自己的
    长连接 loader；writer 仍在调用线程中串行执行。
    """
    codes = list(codes)
    periods = [period.lower() for period in periods]
    if failure_limit is not None and failure_limit < 1:
        raise ValueError("failure_limit must be positive or None")
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError("workers must be a positive integer")

    total = len(codes) * len(periods)
    report = {
        "total": total, "completed": 0, "succeeded": 0, "empty": 0,
        "failed": 0, "bars": 0, "stopped": False, "failures": [],
    }
    if total == 0:
        return report

    workers = min(workers, total)
    if workers > 1:
        if loader_factory is None:
            raise ValueError("loader_factory is required when workers > 1")
        tasks = ((std_code, period) for period in periods for std_code in codes)
        _import_concurrently(
            tasks, workers, loader_factory, writer, report, progress,
            failure_limit,
        )
        zt_info("TDX bulk import finished with {} workers: {} bars, {} failed, {} empty",
                workers, report["bars"], report["failed"], report["empty"])
        return report

    # 连续失败通常意味着 TDX 节点已断开；零散失败则可能只是个别证券异常。
    consecutive_failures = 0

    for period in periods:
        for std_code in codes:
            try:
                result = loader(std_code, period)
                _record_result(report, writer, std_code, period, result)
                # 成功取得数据或确认数据为空，都说明当前连接仍可继续使用。
                consecutive_failures = 0
            except Exception as exc:
                _record_failure(report, std_code, period, exc)
                consecutive_failures += 1

            report["completed"] += 1
            if progress is not None:
                progress(report["completed"], total)

            if failure_limit is not None and consecutive_failures >= failure_limit:
                report["stopped"] = True
                zt_warn("Stopped bulk TDX import after {} consecutive failures",
                        consecutive_failures)
                return report

    zt_info("TDX bulk import finished: {} bars, {} failed, {} empty",
            report["bars"], report["failed"], report["empty"])
    return report
