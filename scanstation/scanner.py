"""Drive the S1500 through scanimage: raw duplex scans, double-feed detection, the scan button."""
import fcntl
import os
import re
import subprocess
from pathlib import Path

# Raw scans: the Pi straightens, crops, and drops blanks itself, because the driver's deskew rotated
# a form 37° the wrong way in the bake-off. Only paper length and double-feed detection stay with
# the scanner.
SCAN_TIMEOUT = 600   # a full 50-sheet hopper takes about 3 minutes
POLL_TIMEOUT = 10
OPTIONS = [
    "--source", "ADF Duplex", "--mode", "Color", "--resolution", "300",
    "--page-width", "221.121", "--page-height", "876.695", "--ald=yes",
    "--swdeskew=no", "--swcrop=no", "--swskip", "0",
    "--df-action", "Stop", "--df-length=yes", "--format=tiff",
]


S1500_USB = ("04c5", "11a2")
USBDEVFS_RESET = ord("U") << 8 | 20


class ScanError(Exception):
    """A scan that produced no usable document. kind is "jam", "no_paper" or "other"."""

    def __init__(self, kind, detail=""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


def device(run=subprocess.run):
    """The S1500's SANE device name. Its number changes with the USB port, so look it up."""
    out = run(["scanimage", "-L"], capture_output=True, text=True).stdout
    m = re.search(r"device `(fujitsu:[^']*S1500[^']*)'", out)
    if not m:
        raise ScanError("other", "scanner not found")
    return m.group(1)


def scan(outdir, device, thickness_check=True, run=subprocess.run):
    """Scan everything in the feeder. Returns TIFF pages in feed order: front, back, front, back.

    SANE reports a double feed with the same message as a jam, so both are kind "jam". Any pages
    already written are deleted then, so a fragment of a document can never be uploaded.
    """
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    for old in outdir.glob("p*.tif"):
        old.unlink()
    args = ["scanimage", "-d", device, *OPTIONS,
            f"--df-thickness={'yes' if thickness_check else 'no'}", f"--batch={outdir}/p%03d.tif"]
    try:
        r = run(args, capture_output=True, text=True, timeout=SCAN_TIMEOUT)
    except subprocess.TimeoutExpired:
        for p in outdir.glob("p*.tif"):
            p.unlink(missing_ok=True)
        raise ScanError("other", "the scanner stopped responding")
    pages = sorted(outdir.glob("p*.tif"))

    def fail(kind, detail):
        for p in pages:
            p.unlink(missing_ok=True)
        raise ScanError(kind, detail)

    err = r.stderr or ""
    if "jammed" in err or "double feed" in err.lower():
        fail("jam", err.strip().splitlines()[-1])
    if not pages:
        fail("no_paper" if "out of documents" in err else "other", err.strip()[-200:])
    if "out of documents" not in err and "Batch terminated" not in err and r.returncode != 0:
        fail("other", err.strip()[-200:])
    if len(pages) % 2:
        fail("other", f"duplex scan gave an odd number of pages ({len(pages)})")
    return pages


def button_pressed(device, run=subprocess.run):
    """True while the scanner's scan button is down. Opens the device, so never call mid-scan."""
    r = run(["scanimage", "-d", device, "-A"], capture_output=True, text=True, timeout=POLL_TIMEOUT)
    if r.returncode != 0:
        # Raising lets the caller count failures and look the scanner up again.
        raise ScanError("other", (r.stderr or "").strip()[-200:])
    m =re.search(r"--scan\[=\(yes\|no\)\] \[(yes|no)\]", r.stdout)
    return bool(m and m.group(1) == "yes")


def _usbdevfs_reset(node):
    fd = os.open(node, os.O_WRONLY)
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    finally:
        os.close(fd)


def reset_usb(sysfs="/sys/bus/usb/devices", do_reset=_usbdevfs_reset):
    """USB-reset the S1500 if it is on the bus. True if a reset was sent.

    About 15 minutes after it last woke, the S1500 goes into power save and stops answering USB
    commands while staying on the bus. Closing and opening the chute fixes it by re-enumerating;
    a USB reset does the same from software.
    """
    for d in Path(sysfs).iterdir():
        try:
            ids = ((d / "idVendor").read_text().strip(), (d / "idProduct").read_text().strip())
            if ids != S1500_USB:
                continue
            node = f"/dev/bus/usb/{int((d / 'busnum').read_text()):03d}/{int((d / 'devnum').read_text()):03d}"
        except (OSError, ValueError):
            continue
        try:
            do_reset(node)
            return True
        except OSError:
            return False
    return False
