import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from label_porter.config import load_config
from label_porter.engine import run
from label_porter.formats import DataError
from label_porter.storage import KST, output_session


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "one.json").write_text('{"id":"a","label":"old"}', encoding="utf-8")
        self.result = self.root / "result.json"
        self.result.write_text('{"source_id":"a","corrected_label":"new"}', encoding="utf-8")
        self.output = self.root / "output"
        self.config = load_config(Path(__file__).resolve().parents[1] / "rules" / "generic.json")
        self.clock = patch("label_porter.storage.now", return_value=datetime(2026, 10, 19, 23, 59, tzinfo=KST))
        self.fake_now = self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def execute(self, **kwargs):
        return run(self.source, [self.result], self.output, self.config, **kwargs)

    def test_date_sequence_resets_on_next_day_and_never_overwrites(self):
        first = self.execute()
        first_hashes = {p.relative_to(first.directory): p.read_bytes() for p in first.directory.rglob("*") if p.is_file()}
        self.execute(preview=True)
        second = self.execute()
        self.assertEqual(first.directory, self.output / "2026-10-19" / "001")
        self.assertEqual(second.directory, self.output / "2026-10-19" / "002")
        self.fake_now.return_value = datetime(2026, 10, 20, 0, 1, tzinfo=KST)
        third = self.execute()
        self.assertEqual(third.directory, self.output / "2026-10-20" / "001")
        self.assertEqual(first_hashes, {p.relative_to(first.directory): p.read_bytes() for p in first.directory.rglob("*") if p.is_file()})

    def test_preview_reuses_one_folder_refreshes_reports_and_removes_stale_partition_index(self):
        first = self.execute(preview=True)
        self.assertTrue((first.directory / "reports" / "partition_index.jsonl").is_file())
        self.result.write_text('{"source_id":"a","corrected_label":"old"}', encoding="utf-8")
        second = self.execute(preview=True, split_output=False)
        self.assertEqual(first.directory, second.directory)
        self.assertEqual(list(self.output.iterdir()), [self.output / "미리보기"])
        summary = json.loads((second.directory / "reports" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["source_status_counts"], {"unchanged": 1})
        self.assertEqual((second.directory / "reports" / "changes.jsonl").read_text(encoding="utf-8"), "")
        self.assertFalse((second.directory / "reports" / "partition_index.jsonl").exists())
        self.assertFalse((second.directory / "data").exists())

    def test_cancelled_preview_replaces_old_success_and_can_be_retried(self):
        first = self.execute(preview=True)
        event = threading.Event()
        event.set()
        cancelled = self.execute(preview=True, cancel=event)
        self.assertEqual(cancelled.summary["status"], "cancelled")
        self.assertEqual(json.loads((first.directory / "run_status.json").read_text(encoding="utf-8"))["status"], "cancelled")
        self.assertFalse((first.directory / "reports" / "partition_index.jsonl").exists())
        self.assertEqual(self.execute(preview=True).summary["status"], "completed")

    def test_existing_unmanaged_preview_folder_is_preserved(self):
        directory = self.output / "미리보기"
        directory.mkdir(parents=True)
        keep = directory / "keep.json"
        keep.write_bytes(b"do not modify")
        with self.assertRaises(DataError):
            self.execute(preview=True)
        self.assertEqual(keep.read_bytes(), b"do not modify")
        self.assertFalse((directory / "reports").exists())

    def test_inputs_inside_reused_preview_are_rejected_before_writing(self):
        preview = self.execute(preview=True).directory
        self.result = preview / "rule.json"
        before = self.result.read_bytes()
        with self.assertRaises(DataError):
            self.execute(preview=True)
        self.assertEqual(self.result.read_bytes(), before)
        self.result = self.root / "result.json"
        self.source = preview
        with self.assertRaises(DataError):
            self.execute(preview=True)
        self.assertEqual((preview / "rule.json").read_bytes(), before)

    def test_concurrent_preview_is_rejected_and_lock_released(self):
        with output_session(self.output, True, self.source, [self.result]):
            with self.assertRaises(DataError):
                self.execute(preview=True)
        self.assertEqual(self.execute(preview=True).summary["status"], "completed")

    def test_concurrent_runs_allocate_unique_sequential_folders(self):
        def reserve(_):
            with output_session(self.output, False, self.source, [self.result]) as directory:
                return directory.name
        with ThreadPoolExecutor(max_workers=4) as pool:
            names = list(pool.map(reserve, range(8)))
        self.assertEqual(sorted(names), [f"{n:03d}" for n in range(1, 9)])

    def test_existing_dates_numbers_and_legacy_output_folders_remain_intact(self):
        date_dir = self.output / "2026-10-19"
        date_dir.mkdir(parents=True)
        (date_dir / "999").write_bytes(b"keep")
        legacy = self.output / "run_20261019_old"
        legacy.mkdir()
        (legacy / "keep.txt").write_bytes(b"keep")
        result = self.execute()
        self.assertEqual(result.directory.name, "1000")
        self.assertEqual((date_dir / "999").read_bytes(), b"keep")
        self.assertEqual((legacy / "keep.txt").read_bytes(), b"keep")

    def test_hardlinked_report_cannot_modify_external_file(self):
        directory = self.execute(preview=True).directory
        report = directory / "reports" / "records.csv"
        outside = self.root / "outside.csv"
        outside.write_bytes(b"keep outside unchanged")
        report.unlink()
        try:
            os.link(outside, report)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaises(DataError):
            self.execute(preview=True)
        self.assertEqual(outside.read_bytes(), b"keep outside unchanged")


if __name__ == "__main__":
    unittest.main()
