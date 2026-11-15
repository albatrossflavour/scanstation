import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from scanstation.scanner import ScanError
from scanstation.service import Station
from scanstation.spool import Spool

T0 = datetime(2026, 10, 28, 21, 0, tzinfo=timezone.utc)


class FakeScanner:
    def __init__(self):
        self.presses = []      # button readings, consumed one per poll
        self.result = 2        # pages to "scan", or a ScanError to raise
        self.calls = []
        self.during_scan = None

    def button(self):
        return self.presses.pop(0) if self.presses else False

    def scan(self, outdir, thickness_check=True):
        self.calls.append(thickness_check)
        if self.during_scan:
            self.during_scan()
        if isinstance(self.result, ScanError):
            raise self.result
        paths = []
        for i in range(self.result):
            p = Path(outdir) / f"p{i + 1:03d}.tif"
            Image.new("RGB", (300, 400), "white").save(p)
            paths.append(p)
        return paths


def fake_process(paths):
    """Two real pages per sheet, none blank, unless the test swaps this out."""
    return [Image.new("L", (300, 400), 255) for _ in paths], []


class StationTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.scanner = FakeScanner()
        self.sent = []
        self.stepped = []
        self.station = Station(
            scanner=self.scanner, process=fake_process, spool=Spool(self.dir / "spool"),
            notify=lambda kind, doc, detail="": self.sent.append((kind, detail)) or True,
            step=lambda doc, now: self.stepped.append(doc.id) or doc, work_dir=self.dir / "work")

    def tick(self, seconds=0):
        self.station.tick(T0 + timedelta(seconds=seconds))

    def test_press_scans_processes_and_spools(self):
        self.scanner.presses = [True]
        self.tick()
        pending = self.station.spool.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].pages, 2)
        self.assertTrue(self.station.spool.pdf(pending[0]).read_bytes().startswith(b"%PDF"))
        self.assertEqual(self.stepped, [pending[0].id])  # worked in the same tick

    def test_every_scan_archived_when_configured(self):
        archived = []
        self.station.archive = lambda pdf, name: archived.append((Path(pdf).read_bytes()[:4], name)) or True
        self.scanner.presses = [True]
        self.tick()
        doc = self.station.spool.pending()[0]
        self.assertEqual(archived, [(b"%PDF", doc.filename)])

    def test_archive_failure_does_not_stop_the_scan(self):
        self.station.archive = lambda pdf, name: (_ for _ in ()).throw(OSError("share down"))
        self.scanner.presses = [True]
        self.tick()
        self.assertEqual(len(self.station.spool.pending()), 1)

    def test_restart_mid_scan_sends_hold(self):
        # A scan cut off by a restart or power cut must not end in silence.
        def die(outdir, thickness_check=True):
            raise KeyboardInterrupt  # stands in for the process being killed mid-scan
        self.scanner.scan = die
        self.scanner.presses = [True]
        with self.assertRaises(KeyboardInterrupt):
            self.tick()
        again = Station(scanner=FakeScanner(), process=fake_process, spool=self.station.spool,
                        notify=lambda kind, doc, detail="": self.sent.append((kind, detail)) or True,
                        step=lambda doc, now: doc, work_dir=self.dir / "work")
        again.recover()
        self.assertEqual(self.sent, [("hold_scan", "the station restarted in the middle of a scan, rescan the last document")])
        self.sent.clear()
        again.recover()
        self.assertEqual(self.sent, [])

    def test_held_button_scans_once(self):
        self.scanner.presses = [True, True, True, False, True]
        for s in range(5):
            self.tick(s)
        self.assertEqual(len(self.scanner.calls), 2)

    def test_press_during_scan_is_ignored(self):
        self.scanner.during_scan = lambda: self.scanner.presses.extend([True])
        self.scanner.presses = [True]
        self.tick(0)
        self.tick(1)
        self.assertEqual(len(self.scanner.calls), 1)

    def test_jam_sends_hold_and_arms_card_retry(self):
        self.scanner.result = ScanError("jam", "Document feeder jammed")
        self.scanner.presses = [True]
        self.tick(0)
        self.assertEqual(self.sent, [("hold_scan", "jam or double feed, rescan the whole document. A card goes through on the next press")])
        self.assertEqual(self.station.spool.pending(), [])
        self.scanner.result = 2
        self.scanner.presses = [False, True]
        self.tick(30)
        self.tick(60)
        self.assertEqual(self.scanner.calls, [True, False])
        self.scanner.presses = [False, True]
        self.tick(70)
        self.tick(80)
        self.assertEqual(self.scanner.calls, [True, False, True])

    def test_card_retry_expires(self):
        self.scanner.result = ScanError("jam")
        self.scanner.presses = [True]
        self.tick(0)
        self.scanner.result = 2
        self.scanner.presses = [False, True]
        self.tick(100)
        self.tick(130)
        self.assertEqual(self.scanner.calls, [True, True])

    def test_all_pages_blank(self):
        self.station.process = lambda paths: ([], [])
        self.scanner.presses = [True]
        self.tick()
        self.assertEqual(self.sent, [("hold_scan", "every page came out blank, nothing was sent to Paperless")])
        self.assertEqual(self.station.spool.pending(), [])

    def test_processing_crash_keeps_raw_scan(self):
        def boom(paths):
            raise ValueError("bad image")
        self.station.process = boom
        self.scanner.presses = [True]
        self.tick()
        self.assertEqual(self.sent[0][0], "hold_scan")
        kept = list((self.dir / "work" / "failed").glob("*/p*.tif"))
        self.assertEqual(len(kept), 2)

    def test_review_press_during_slow_worker_is_seen(self):
        # Finding 1: the button is polled between worker steps, not once per tick.
        for _ in range(3):
            self.station.spool.new(b"%PDF", pages=1, now=T0)
        self.station.step = lambda doc, now: self.scanner.presses.extend([True]) or doc
        self.scanner.presses = [False]
        self.tick(0)
        self.assertEqual(len(self.scanner.calls), 1)

    def test_review_tick_survives_scan_exception(self):
        # Finding 9 (minor, but the same guard): nothing in a press may kill the loop.
        self.station.notify = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        self.scanner.result = ScanError("jam")
        self.scanner.presses = [True]
        self.tick(0)  # must not raise


class StartupTest(unittest.TestCase):
    def test_review_bad_token_at_startup_does_not_crash(self):
        # Finding 7: a 401 at startup must not crash-loop the service and stop all scanning.
        from scanstation.paperless import PaperlessDown, PaperlessRejected
        from scanstation.service import lookup_gpt_tag

        class P:
            def __init__(self, exc):
                self.exc = exc

            def tag_id(self, name):
                raise self.exc
        self.assertEqual(lookup_gpt_tag(P(PaperlessRejected("HTTP 401"))), -1)
        self.assertEqual(lookup_gpt_tag(P(PaperlessDown("down"))), -1)


class ScannerWrapperTest(unittest.TestCase):
    def test_scanner_closed_at_startup_and_reopened(self):
        # The S1500 is off the USB bus while its chute is closed.
        from scanstation import service
        from scanstation.scanner import ScanError
        present = {"on": False}

        def lookup():
            if not present["on"]:
                raise ScanError("other", "scanner not found")
            return "fujitsu:ScanSnap S1500:42"
        ready = []
        s = service._Scanner(lookup=lookup, poll=lambda dev: True, retry_every=0, on_ready=lambda: ready.append(1),
                             reset=lambda: False)
        self.assertIsNone(s.device)
        self.assertEqual(ready, [])
        self.assertFalse(s.button())
        present["on"] = True
        self.assertTrue(s.button())
        self.assertEqual(s.device, "fujitsu:ScanSnap S1500:42")
        s.button()
        self.assertEqual(ready, [1])  # once per appearance, not per poll

    def test_scanner_looked_for_every_second(self):
        from scanstation import service
        self.assertLessEqual(service._Scanner.__init__.__defaults__[2], 1.0)

    def test_review_device_looked_up_again_after_failures(self):
        # Finding 6: the S1500 re-enumerates when the chute closes, so its name can change.
        from scanstation import service
        names = iter(["fujitsu:old", "fujitsu:new"])
        calls = []
        service.scan_mod_device = None
        s = service._Scanner(retry_every=0, lookup=lambda: next(names), reset=lambda: False,
                             poll=lambda dev: calls.append(dev) or (_ for _ in ()).throw(OSError("gone")) if dev == "fujitsu:old" else True)
        for _ in range(service.DEVICE_RETRY + 1):
            s.button()
        self.assertEqual(s.device, "fujitsu:new")
        self.assertTrue(s.button())

    def test_asleep_scanner_is_reset_not_dropped(self):
        # The S1500 stops answering on USB about 15 minutes after it last woke, while still on the
        # bus. A USB reset brings it back, the same as closing and opening the chute.
        from scanstation import service
        state = {"awake": True}
        resets, ready = [], []

        def lookup():
            if not state["awake"]:
                raise OSError("no scanners")
            return "fujitsu:ScanSnap S1500:1"

        def poll(dev):
            if not state["awake"]:
                raise OSError("I/O error")
            return False

        def reset():
            resets.append(1)
            state["awake"] = True
            return True

        s = service._Scanner(lookup=lookup, poll=poll, retry_every=0, on_ready=lambda: ready.append(1), reset=reset,
                             sleep=lambda s: None)
        state["awake"] = False
        for _ in range(service.DEVICE_RETRY):
            s.button()
        self.assertEqual(resets, [1])
        self.assertEqual(s.device, "fujitsu:ScanSnap S1500:1")
        self.assertEqual(ready, [1])  # still the one from startup: a reset isn't a new appearance

    def test_no_reset_storm_when_reset_does_not_help(self):
        from scanstation import service
        t = {"now": 0.0}
        resets = []

        def lookup():
            raise OSError("no scanners")

        s = service._Scanner(lookup=lookup, poll=lambda dev: False, retry_every=0, clock=lambda: t["now"],
                             reset=lambda: resets.append(1) or True, sleep=lambda s: None)
        for _ in range(20):
            t["now"] += 1
            s.button()
        self.assertEqual(len(resets), 1)
        t["now"] += service.RESET_EVERY
        s.button()
        self.assertEqual(len(resets), 2)


if __name__ == "__main__":
    unittest.main()
