import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from scanstation.pdf import build_pdf
from scanstation.spool import Spool

NOW = datetime(2026, 10, 28, 21, 0, tzinfo=timezone.utc)


class PdfTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pdfimages"), "poppler not installed")
    def test_pdf_pages_are_not_recompressed(self):
        out = Path(tempfile.mkdtemp()) / "d.pdf"
        build_pdf([Image.new("RGB", (2480, 3508), (250, 250, 250)), Image.new("L", (900, 2900), 255)], out)
        rows = subprocess.run(["pdfimages", "-list", str(out)], capture_output=True, text=True).stdout.splitlines()[2:]
        self.assertEqual(len(rows), 2)
        for row in rows:
            cols = row.split()
            self.assertEqual(cols[8], "jpeg")       # enc: the JPEG we wrote, embedded as is
            self.assertEqual(cols[12], "300")       # x-ppi
        self.assertEqual(rows[1].split()[5], "gray")


class SpoolTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_new_doc_is_pending(self):
        s = Spool(self.root)
        d = s.new(b"%PDF-1.4", pages=2, now=NOW)
        self.assertTrue(d.filename.startswith("scanstation-20261028-"))
        self.assertEqual(d.state, "new")
        self.assertEqual([x.id for x in s.pending()], [d.id])
        self.assertEqual((s.root / d.id / "document.pdf").read_bytes(), b"%PDF-1.4")

    def test_state_written_atomically(self):
        s = Spool(self.root)
        d = s.new(b"x", pages=1, now=NOW)
        path = s.root / d.id / "state.json"
        stop, bad = threading.Event(), []

        def reader():
            while not stop.is_set():
                try:
                    json.loads(path.read_text())
                except json.JSONDecodeError:
                    bad.append(1)
                except FileNotFoundError:
                    bad.append(2)

        t = threading.Thread(target=reader)
        t.start()
        for i in range(300):
            d = s.update(d, attempts=i, last_error="x" * (i % 50))
        stop.set()
        t.join()
        self.assertEqual(bad, [])

    def test_spool_resumes_after_restart(self):
        d = Spool(self.root).new(b"x", pages=3, now=NOW)
        Spool(self.root).update(d, state="uploaded", task_id="t1")
        again = Spool(self.root).pending()
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0].task_id, "t1")
        self.assertEqual(again[0].pages, 3)

    def test_prune_keeps_unfinished(self):
        s = Spool(self.root)
        old = NOW - timedelta(days=31)
        done = s.update(s.new(b"x", 1, now=old), state="done", finished_at=old.isoformat())
        failsafe = s.update(s.new(b"x", 1, now=old), state="failsafe")
        rejected = s.update(s.new(b"x", 1, now=old), state="rejected")
        recent = s.update(s.new(b"x", 1, now=NOW), state="done", finished_at=NOW.isoformat())
        self.assertEqual(s.prune(NOW), 1)
        left = {p.name for p in s.root.iterdir()}
        self.assertNotIn(done.id, left)
        self.assertTrue({failsafe.id, rejected.id, recent.id} <= left)
        self.assertNotIn(recent.id, [d.id for d in s.pending()])


if __name__ == "__main__":
    unittest.main()
