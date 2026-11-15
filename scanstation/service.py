"""The scan station service: watch the button, scan, process, spool, and work the spool.

One process owns the scanner. It polls the button between scans only, never during one, and
works the spool between polls, so an upload never holds up the next stack.
"""
import io
import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

from PIL import Image

from . import scanner as scan_mod
from .failsafe import Archive, Failsafe
from .image import correct, is_blank, page_flags, straighten_sheet, upright
from .notify import Notifier
from .paperless import Paperless, PaperlessDown, PaperlessRejected
from .pdf import build_pdf
from .spool import Spool
from .worker import step as worker_step

CARD_RETRY = timedelta(seconds=120)
DEVICE_RETRY = 10  # failed button polls before looking the scanner up again
RESET_EVERY = 60  # seconds between USB resets of a scanner that is on the bus but not answering
RESET_SETTLE = 2  # seconds for the scanner to come back after a reset
STEPS_PER_TICK = 5
log = logging.getLogger("scanstation")


def process(paths):
    """Raw duplex TIFFs to finished pages. Returns (pages, flags) with blank pages dropped."""
    pages, flags = [], []
    for front_path, back_path in zip(paths[0::2], paths[1::2]):
        front, back, _ = straighten_sheet(Image.open(front_path), Image.open(back_path))
        for img, side in ((front, "front"), (back, "back")):
            page = correct(img, side)
            if is_blank(page):
                continue
            page, _, _ = upright(page)
            flags.extend(f for f in page_flags(page) if f not in flags)
            pages.append(page)
    return pages, flags


class Station:
    def __init__(self, *, scanner, process, spool, notify, step, work_dir):
        self.scanner, self.process, self.spool = scanner, process, spool
        self.notify, self.step = notify, step
        self.work_dir = Path(work_dir)
        self.was_pressed = False
        self.card_retry_until = None
        # With ARCHIVE=yes every scan is also kept on the failsafe share, so a fault in the page
        # processing can be recovered from. None turns it off.
        self.archive = None

    def tick(self, now):
        # The button is polled before every worker step, so a slow Paperless or failsafe call can
        # delay a press by at most one step, never swallow it.
        self.poll_button(now)
        for doc in self.spool.pending()[:STEPS_PER_TICK]:
            try:
                self.step(doc, now)
            except Exception:  # one bad doc mustn't stop the rest, or the next scan
                log.exception("worker step failed for %s", doc.id)
            self.poll_button(now)

    def poll_button(self, now):
        pressed = self.scanner.button()
        if pressed and not self.was_pressed:
            try:
                self.handle_press(now)
            except Exception:  # the loop must survive anything a press throws
                log.exception("handling a press failed")
            # A button still held after the scan mustn't start another; wait for it to be released.
            pressed = True
        self.was_pressed = pressed

    @property
    def _scanning_mark(self):
        return self.work_dir / "scanning"

    def recover(self):
        """At startup: if the last scan never finished (restart, power cut), say so."""
        if self._scanning_mark.exists():
            self._scanning_mark.unlink()
            self.notify("hold_scan", None, "the station restarted in the middle of a scan, rescan the last document")

    def handle_press(self, now):
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._scanning_mark.write_text(now.isoformat())
        try:
            self._handle_press(now)
        except Exception:
            self._scanning_mark.unlink(missing_ok=True)
            raise
        # Being killed (SIGTERM, SystemExit, a power cut) leaves the mark for recover() to find.
        self._scanning_mark.unlink(missing_ok=True)

    def _handle_press(self, now):
        card_retry = self.card_retry_until is not None and now <= self.card_retry_until
        self.card_retry_until = None
        raw = self.work_dir / "current"
        raw.mkdir(parents=True, exist_ok=True)
        try:
            paths = self.scanner.scan(raw, thickness_check=not card_retry)
        except scan_mod.ScanError as e:
            if e.kind == "jam":
                self.card_retry_until = now + CARD_RETRY
                self.notify("hold_scan", None, "jam or double feed, rescan the whole document. A card goes through on the next press")
            elif e.kind == "no_paper":
                self.notify("hold_scan", None, "the feeder was empty, nothing scanned")
            else:
                self.notify("hold_scan", None, f"the scanner reported a problem ({e.detail[:120]}), rescan the whole document")
            return
        try:
            pages, flags = self.process(paths)
            if not pages:
                self.notify("hold_scan", None, "every page came out blank, nothing was sent to Paperless")
                return
            buf = io.BytesIO()
            build_pdf(pages, buf)
            doc = self.spool.new(buf.getvalue(), pages=len(pages), now=now, flags=flags)
            if self.archive:
                try:
                    if not self.archive(self.spool.pdf(doc), doc.filename):
                        log.warning("archive copy of %s failed", doc.filename)
                except Exception:
                    log.exception("archive copy of %s failed", doc.filename)
        except Exception:
            log.exception("processing failed")
            keep = self.work_dir / "failed" / now.strftime("%Y%m%d-%H%M%S")
            keep.mkdir(parents=True, exist_ok=True)
            for p in paths:
                shutil.move(str(p), keep / Path(p).name)
            self.notify("hold_scan", None, f"the scan couldn't be processed, raw pages kept on the Pi in {keep.name}")


class _Scanner:
    """The real scanner. Copes with it vanishing: the S1500 is off the USB bus while its chute is closed."""

    def __init__(self, lookup=scan_mod.device, poll=scan_mod.button_pressed, retry_every=1.0, clock=time.monotonic,
                 on_ready=lambda: None, reset=scan_mod.reset_usb, sleep=time.sleep):
        self.lookup, self.poll, self.retry_every, self.clock = lookup, poll, retry_every, clock
        self.on_ready, self.reset, self.sleep = on_ready, reset, sleep
        self.device, self.failures, self.last_lookup, self.last_reset = None, 0, None, None
        self._find()

    def _lookup(self):
        """The device name. A scanner that is on the bus but asleep gets a USB reset and a second try."""
        try:
            return self.lookup()
        except Exception:
            now = self.clock()
            if self.last_reset is not None and now - self.last_reset < RESET_EVERY:
                raise
            self.last_reset = now
            if not self.reset():
                raise
            log.info("scanner not answering, sent it a USB reset")
            self.sleep(RESET_SETTLE)
            return self.lookup()

    def _find(self):
        self.last_lookup = self.clock()
        try:
            found = self._lookup()
            if found != self.device:
                log.info("scanner ready: %s", found)
                self.device = found
                # No screen at the rack: a quiet message says when a press will be seen.
                self.on_ready()
        except Exception:
            if self.device is not None:
                log.info("scanner gone (chute closed?)")
            self.device = None

    def button(self):
        if self.device is None:
            if self.clock() - self.last_lookup < self.retry_every:
                return False
            self._find()
            if self.device is None:
                return False
        try:
            pressed = self.poll(self.device)
            self.failures = 0
            return pressed
        except Exception:
            self.failures += 1
            # A closed chute or a re-enumeration with a new name: look the scanner up again.
            if self.failures >= DEVICE_RETRY:
                self.failures = 0
                self._find()
            return False

    def scan(self, outdir, thickness_check=True):
        if self.device is None:
            raise scan_mod.ScanError("other", "scanner not connected")
        return scan_mod.scan(outdir, self.device, thickness_check=thickness_check)


def lookup_gpt_tag(paperless, name="paperless-gpt-auto"):
    """The id of the tag paperless-gpt removes when it's done, or -1 for no wait. Never raises: scanning
    must start even if Paperless won't talk."""
    try:
        return paperless.tag_id(name) or -1
    except (PaperlessDown, PaperlessRejected) as e:
        log.warning("couldn't look up the paperless-gpt tag (%s); waiting the full time for classification", e)
        return -1


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    env = os.environ
    state = Path(env.get("STATE_DIR", "/var/lib/scanstation"))
    paperless = Paperless(env["PAPERLESS_URL"], env["PAPERLESS_TOKEN"])
    failsafe_dir = env.get("FAILSAFE_DIR") or None
    notify = Notifier(env.get("PUSHOVER_TOKEN", ""), env.get("PUSHOVER_USER", ""),
                      env.get("PAPERLESS_PUBLIC_URL") or env["PAPERLESS_URL"], env.get("STATUS_PATH", state / "status.json"),
                      failsafe_name=(env.get("FAILSAFE_NAME") or "the failsafe share") if failsafe_dir else None)
    failsafe = Failsafe(failsafe_dir)
    spool = Spool(state / "spool")
    gpt_tag = lookup_gpt_tag(paperless, env.get("GPT_TAG") or "paperless-gpt-auto")
    step = lambda doc, now: worker_step(doc, paperless=paperless, spool=spool, notify=notify,
                                        failsafe=failsafe, now=now, gpt_tag=gpt_tag)
    scanner = _Scanner(on_ready=lambda: notify("ready", None))
    station = Station(scanner=scanner, process=process, spool=spool, notify=notify, step=step,
                      work_dir=state / "work")
    station.recover()
    if failsafe_dir and env.get("ARCHIVE", "no").lower() == "yes":
        station.archive = Archive(Path(failsafe_dir) / "archive").copy
    log.info("scan station ready")
    last_prune = None
    while True:
        now = datetime.now(timezone.utc)
        station.tick(now)
        if last_prune is None or now - last_prune > timedelta(hours=6):
            spool.prune(now)
            last_prune = now
        time.sleep(0.5)


if __name__ == "__main__":
    main()
