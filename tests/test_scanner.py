import subprocess
import tempfile
import unittest
from pathlib import Path

from scanstation.scanner import ScanError, button_pressed, reset_usb, scan

OPTIONS = [
    "--source", "ADF Duplex", "--mode", "Color", "--resolution", "300",
    "--page-width", "221.121", "--page-height", "876.695", "--ald=yes",
    "--swdeskew=no", "--swcrop=no", "--swskip", "0",
    "--df-action", "Stop", "--df-length=yes", "--format=tiff",
]


class FakeRun:
    """Stands in for subprocess.run: writes the given number of pages, returns the given stderr."""

    def __init__(self, pages=0, stderr="", code=0):
        self.pages, self.stderr, self.code, self.calls = pages, stderr, code, []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        batch = next((a for a in args if a.startswith("--batch=")), None)
        if batch:
            pattern = batch.split("=", 1)[1]
            for i in range(1, self.pages + 1):
                Path(pattern % i).write_bytes(b"tiff")
        return subprocess.CompletedProcess(args, self.code, stdout="", stderr=self.stderr)


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_scan_builds_exact_options(self):
        run = FakeRun(pages=2, stderr="Batch terminated, 2 pages scanned")
        pages = scan(self.dir, device="fujitsu:ScanSnap S1500:1", run=run)
        args = run.calls[-1]
        for opt in OPTIONS:
            self.assertIn(opt, args)
        self.assertIn("--df-thickness=yes", args)
        self.assertEqual([p.name for p in pages], ["p001.tif", "p002.tif"])
        scan(self.dir, device="fujitsu:ScanSnap S1500:1", thickness_check=False, run=run)
        self.assertIn("--df-thickness=no", run.calls[-1])

    def test_jam_maps_to_scan_error(self):
        run = FakeRun(pages=1, stderr="scanimage: sane_read: Document feeder jammed", code=6)
        with self.assertRaises(ScanError) as e:
            scan(self.dir, device="d", run=run)
        self.assertEqual(e.exception.kind, "jam")
        self.assertEqual(list(self.dir.glob("*.tif")), [])

    def test_empty_feeder(self):
        run = FakeRun(pages=0, stderr="scanimage: sane_start: Document feeder out of documents")
        with self.assertRaises(ScanError) as e:
            scan(self.dir, device="d", run=run)
        self.assertEqual(e.exception.kind, "no_paper")
        run = FakeRun(pages=2, stderr="scanimage: sane_start: Document feeder out of documents\nBatch terminated, 2 pages scanned")
        self.assertEqual(len(scan(self.dir, device="d", run=run)), 2)

    def test_odd_page_count_is_an_error(self):
        # Duplex always gives front and back; an odd count means a page went missing.
        run = FakeRun(pages=3, stderr="Batch terminated, 3 pages scanned")
        with self.assertRaises(ScanError) as e:
            scan(self.dir, device="d", run=run)
        self.assertEqual(e.exception.kind, "other")

    def test_review_scan_timeout_is_a_scan_error(self):
        def hang(args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
        with self.assertRaises(ScanError) as e:
            scan(self.dir, device="d", run=hang)
        self.assertEqual(e.exception.kind, "other")


class ButtonTest(unittest.TestCase):
    def test_review_button_poll_has_timeout(self):
        seen = {}
        def run(args, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(args, 0, "", "")
        button_pressed(device="d", run=run)
        self.assertLessEqual(seen.get("timeout", 999), 15)

    def test_button_parse(self):
        pressed = "    --scan[=(yes|no)] [yes] [hardware]\n    --page-loaded[=(yes|no)] [no] [hardware]"
        idle = "    --scan[=(yes|no)] [no] [hardware]"
        self.assertTrue(button_pressed(device="d", run=lambda *a, **k: subprocess.CompletedProcess(a, 0, pressed, "")))
        self.assertFalse(button_pressed(device="d", run=lambda *a, **k: subprocess.CompletedProcess(a, 0, idle, "")))

    def test_button_poll_raises_when_device_wont_open(self):
        # 29 Oct: a wedged scanner failed every poll with this, and the station never noticed.
        err = "scanimage: open of device d failed: Invalid argument"
        with self.assertRaises(ScanError):
            button_pressed(device="d", run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", err))


class ResetTest(unittest.TestCase):
    def _sysfs(self, root, name, vendor, product, bus, dev):
        d = Path(root) / name
        d.mkdir()
        for f, v in (("idVendor", vendor), ("idProduct", product), ("busnum", bus), ("devnum", dev)):
            (d / f).write_text(v + "\n")

    def test_resets_the_s1500_node(self):
        with tempfile.TemporaryDirectory() as root:
            self._sysfs(root, "1-2", "1d6b", "0002", "1", "1")
            self._sysfs(root, "1-1", "04c5", "11a2", "1", "9")
            reset = []
            self.assertTrue(reset_usb(sysfs=root, do_reset=reset.append))
            self.assertEqual(reset, ["/dev/bus/usb/001/009"])

    def test_nothing_to_reset_when_off_the_bus(self):
        with tempfile.TemporaryDirectory() as root:
            self._sysfs(root, "1-2", "1d6b", "0002", "1", "1")
            reset = []
            self.assertFalse(reset_usb(sysfs=root, do_reset=reset.append))
            self.assertEqual(reset, [])

    def test_failed_reset_is_false(self):
        with tempfile.TemporaryDirectory() as root:
            self._sysfs(root, "1-1", "04c5", "11a2", "1", "9")

            def boom(path):
                raise PermissionError(path)
            self.assertFalse(reset_usb(sysfs=root, do_reset=boom))


if __name__ == "__main__":
    unittest.main()
