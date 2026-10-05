"""Regression sequences for premature 100%, queue changes and lost final stats."""
import unittest
import threading
from types import SimpleNamespace
from urllib.error import URLError
from bridge.progress import Progress, finish_progress
from bridge.rclone import Transfer


class ProgressTests(unittest.TestCase):
    def ready(self, progress):
        progress.observe({"source": "sync/sync.go:984", "msg": "Checks finished, now starting transfers"})

    def test_growing_scan_does_not_report_100_percent(self):
        progress = Progress("bisync")
        for count in (4, 15, 152):
            progress.observe({"source": "accounting/stats.go:549", "msg": "", "stats": {
                "bytes": count * 1024, "totalBytes": count * 1024,
                "transfers": count, "totalTransfers": count, "listed": count * 10,
            }})
            self.assertIsNone(progress.snapshot()["percent"])
            self.assertEqual(progress.snapshot()["phase"], "scanning")

    def test_percentage_requires_a_fresh_completed_queue(self):
        progress = Progress("copy")
        progress.update_stats({"bytes": 100, "totalBytes": 100})
        self.ready(progress)
        self.assertIsNone(progress.snapshot()["percent"])
        progress.update_stats({"bytes": 100, "totalBytes": 400})
        self.assertEqual(progress.snapshot()["percent"], 25)
        progress.update_stats({"bytes": 399.99, "totalBytes": 400})
        self.assertEqual(progress.snapshot()["percent"], 99.9)
        progress.update_stats({"bytes": 400, "totalBytes": 400})
        self.assertIsNone(progress.snapshot()["percent"])
        self.assertEqual(progress.snapshot()["phase"], "finishing")

    def test_second_direction_starts_a_new_unknown_queue(self):
        progress = Progress("bisync")
        progress.observe({"source": "bisync/resync.go:104", "msg": "- \x1b[34mPath2\x1b[0m Resync is copying files to - Path1"})
        self.ready(progress)
        progress.update_stats({"bytes": 50, "totalBytes": 100})
        self.assertEqual(progress.snapshot()["direction"], "nextcloud_to_icloud")
        self.assertEqual(progress.snapshot()["percent"], 50)
        progress.observe({"source": "bisync/resync.go:123", "msg": "- Path1 Resync is copying files to - Path2"})
        self.assertEqual(progress.snapshot()["direction"], "icloud_to_nextcloud")
        self.assertIsNone(progress.snapshot()["percent"])
        progress.update_stats({"bytes": 100, "totalBytes": 100})
        self.assertIsNone(progress.snapshot()["percent"])

    def test_rc_unavailable_retains_log_statistics(self):
        progress = Progress("copy")
        progress.observe({"source": "accounting/stats.go:549", "msg": "", "stats": {
            "bytes": 65536, "totalBytes": 65536, "transfers": 1, "totalTransfers": 1,
        }})
        transfer = Transfer.__new__(Transfer)
        transfer.telemetry, transfer.lock, transfer.port = progress, threading.Lock(), 12345
        class Offline:
            def open(self, *args, **kwargs):
                raise URLError("RC process exited")
        transfer.rc = SimpleNamespace(auth="test", opener=Offline())
        self.assertEqual(transfer.stats()["bytes"], 65536)
        self.assertEqual(transfer.stats()["transfers"], 1)

    def test_file_directory_failure_is_never_success(self):
        progress = Progress("bisync")
        progress.observe({"source": "operations/copy.go:347", "level": "error", "msg": "Failed to copy: is a file not a directory",
                          "object": "Documents/Report.key/Data/image.png"})
        progress.update_stats({"checks": 1735, "totalChecks": 1735, "errors": 235, "fatalError": True, "bytes": 0})
        run = {"status": "failed", "stats": progress.stats, "progress": progress.snapshot()}
        finish_progress(run)
        self.assertIsNone(run["progress"]["percent"])
        self.assertEqual(run["progress"]["phase"], "failed")
        self.assertEqual(run["progress"]["problem"], {"code": "file_directory_conflict", "path": "Documents/Report.key", "iwork_package": True})
        self.assertEqual(run["stats"]["errors"], 235)

    def test_success_with_no_transfers_still_finishes_at_100(self):
        run = {"status": "success", "stats": {"bytes": 0, "totalBytes": 0, "transferring": [{"name": "old"}]}, "progress": {"phase": "scanning", "percent": None}}
        finish_progress(run)
        self.assertEqual(run["progress"]["percent"], 100)
        self.assertEqual(run["progress"]["scope"], "run")
        self.assertEqual(run["stats"]["transferring"], [])

    def test_incomplete_samples_do_not_zero_counters(self):
        progress = Progress("copy")
        progress.update_stats({"bytes": 10, "totalBytes": 40, "eta": None, "listed": 2})
        self.ready(progress)
        progress.update_stats({"bytes": None, "speed": float("nan")})
        progress.update_stats({})
        self.assertEqual(progress.stats["bytes"], 10)
        self.assertIsNone(progress.stats["eta"])
        self.assertEqual(progress.stats["listed"], 2)


if __name__ == "__main__":
    unittest.main()
