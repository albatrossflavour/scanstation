"""Copies scans onto the failsafe share when Paperless can't take them.

The share is a systemd automount, so a dead NAS can hang a plain file call. Every touch goes
through a subprocess with a timeout instead.
"""
import subprocess
from pathlib import Path

TIMEOUT = 10


class Failsafe:
    def __init__(self, share):
        self.share = Path(share) if share else None

    def copy(self, pdf, name):
        """Copy pdf onto the share as name. Returns the path there, or None if it couldn't."""
        if not self.share:
            return None
        dest = self.share / name
        try:
            r = subprocess.run(["cp", str(pdf), str(dest)], capture_output=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            return None
        return dest if r.returncode == 0 else None

    def remove(self, path):
        try:
            subprocess.run(["rm", "-f", str(path)], capture_output=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            pass


class Archive(Failsafe):
    """Keeps every scan on the share, filed by year (from the scanstation-YYYYMMDD-... filename)."""

    def copy(self, pdf, name):
        if not self.share:
            return None
        year = name.split("-")[1][:4] if name.count("-") >= 2 else "unknown"
        try:
            r = subprocess.run(["mkdir", "-p", str(self.share / year)], capture_output=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            return None
        if r.returncode != 0:
            return None
        return Failsafe(self.share / year).copy(pdf, name)
