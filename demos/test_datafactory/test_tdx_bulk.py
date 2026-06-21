import unittest
from threading import Lock, get_ident
from time import sleep

from ztpy.apps.datahelper.data.tdx_bulk import import_all_bars


class TdxBulkImportTest(unittest.TestCase):

    def test_collects_success_empty_failure_and_progress(self):
        written = []
        progress = []

        def loader(code, period):
            if code == "SSE.600001":
                return "SSE", "600001", [], period == "day"
            if code == "SSE.600002" and period == "min5":
                raise RuntimeError("broken security")
            return "SSE", code.split(".")[1], [{"date": 20260620}], period == "day"

        report = import_all_bars(
            ["SSE.600000", "SSE.600001", "SSE.600002"],
            ["DAY", "min5"], loader,
            lambda *args: written.append(args),
            progress=lambda current, total: progress.append((current, total)),
        )

        self.assertEqual(6, report["total"])
        self.assertEqual(6, report["completed"])
        self.assertEqual(3, report["succeeded"])
        self.assertEqual(2, report["empty"])
        self.assertEqual(1, report["failed"])
        self.assertEqual(3, report["bars"])
        self.assertFalse(report["stopped"])
        self.assertEqual("SSE.600002", report["failures"][0]["code"])
        self.assertEqual("min5", report["failures"][0]["period"])
        self.assertEqual(3, len(written))
        self.assertEqual((6, 6), progress[-1])

    def test_stops_after_consecutive_failures(self):
        calls = []

        def loader(code, period):
            calls.append(code)
            raise ConnectionError("server unavailable")

        report = import_all_bars(
            ["SSE.600000", "SSE.600001", "SSE.600002"], ["day"],
            loader, lambda *args: None, failure_limit=2,
        )

        self.assertTrue(report["stopped"])
        self.assertEqual(2, report["completed"])
        self.assertEqual(2, report["failed"])
        self.assertEqual(["SSE.600000", "SSE.600001"], calls)

    def test_success_and_empty_reset_failure_counter(self):
        outcomes = iter([RuntimeError("one"), [], RuntimeError("two"), [{"x": 1}]])

        def loader(code, period):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return "SSE", code[-6:], outcome, True

        report = import_all_bars(
            ["SSE.600000", "SSE.600001", "SSE.600002", "SSE.600003"],
            ["day"], loader, lambda *args: None, failure_limit=2,
        )
        self.assertFalse(report["stopped"])
        self.assertEqual(2, report["failed"])
        self.assertEqual(1, report["empty"])

    def test_empty_inputs_and_disabled_limit(self):
        report = import_all_bars([], ["day"], None, None, failure_limit=None)
        self.assertEqual(0, report["total"])
        self.assertEqual(0, report["completed"])

    def test_rejects_invalid_failure_limit(self):
        with self.assertRaises(ValueError):
            import_all_bars([], [], None, None, failure_limit=0)

    def test_concurrent_workers_use_private_loaders_and_main_thread_writer(self):
        main_thread = get_ident()
        created = []
        writer_threads = []
        loader_threads = set()
        lock = Lock()

        class Loader:
            def __init__(self):
                self.closed = False

            def __call__(self, code, period):
                with lock:
                    loader_threads.add(get_ident())
                sleep(0.02)
                return "SSE", code[-6:], [{"date": 20260620}], True

            def close(self):
                self.closed = True

        def factory():
            resource = Loader()
            with lock:
                created.append(resource)
            return resource

        report = import_all_bars(
            ["SSE.{:06d}".format(value) for value in range(8)], ["day"],
            None, lambda *args: writer_threads.append(get_ident()),
            workers=3, loader_factory=factory,
        )

        self.assertEqual(8, report["succeeded"])
        self.assertGreaterEqual(len(loader_threads), 2)
        self.assertLessEqual(len(created), 3)
        self.assertTrue(all(resource.closed for resource in created))
        self.assertEqual({main_thread}, set(writer_threads))

    def test_concurrent_mode_requires_factory(self):
        with self.assertRaises(ValueError):
            import_all_bars(
                ["SSE.600000", "SSE.600001"], ["day"],
                lambda *args: None, lambda *args: None, workers=2,
            )

    def test_rejects_invalid_worker_count(self):
        for workers in (0, -1, True, 1.5):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                import_all_bars([], [], None, None, workers=workers)


if __name__ == "__main__":
    unittest.main()
