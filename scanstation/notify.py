"""Pushover messages for the rack, plus a status file a future screen can read.

A message starting "Safe to shred:" means Paperless or the failsafe share holds the scan. Anything
else starts "HOLD this stack:" and says why. With no Pushover keys, messages only go to the log.
"""
import json
import logging
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PUSHOVER = "https://api.pushover.net/1/messages.json"
FLAG_WORDS = {"edge": "content touches an edge, check nothing's cut off",
              "skewed": "a page is still crooked",
              "unreadable": "Paperless found little or no text"}
log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token, user, paperless_url, status_path, opener=urllib.request.urlopen,
                 failsafe_name="the failsafe share"):
        self.token, self.user = token, user
        self.failsafe_name = failsafe_name
        self.paperless_url = paperless_url.rstrip("/")
        self.status_path = Path(status_path)
        self.opener = opener

    def message(self, kind, doc, detail=""):
        if kind == "ready":
            return "Scanner ready", True
        pages = f"{doc.pages} page{'s' if doc.pages != 1 else ''}" if doc else ""
        title = (doc.title or doc.filename) if doc else ""
        if title.startswith("scanstation-"):
            title = "Untitled scan"  # paperless-gpt hasn't titled it; the checker will later
        if kind == "safe":
            text = f"Safe to shred: {title} ({pages})"
            if detail:
                text += f", {detail}"
            return text, True
        if kind == "stored_on_failsafe":
            return f"Safe to shred: Paperless is unreachable, so {doc.filename} ({pages}) is on {self.failsafe_name}. It'll go into Paperless when it's back", True
        if kind == "hold_check":
            reasons = "; ".join(FLAG_WORDS.get(f, f) for f in doc.flags)
            return f"HOLD this stack: {title} ({pages}) is in Paperless but needs a look: {reasons}", False
        if kind == "hold_pi_only":
            if not self.failsafe_name:
                return f"HOLD this stack: Paperless is unreachable and there's no failsafe share, so {doc.filename} is only on the Pi", False
            return f"HOLD this stack: Paperless and {self.failsafe_name} are both unreachable, so {doc.filename} is only on the Pi", False
        if kind == "hold_rejected":
            return f"HOLD this stack: {detail or 'Paperless rejected the scan'}. A copy is kept for you", False
        return f"HOLD this stack: {detail or kind}", False

    def __call__(self, kind, doc, detail=""):
        text, safe = self.message(kind, doc, detail)
        self._status(kind, text, safe, doc)
        if not (self.token and self.user):
            log.info("message: %s", text)
            return True
        fields = {"token": self.token, "user": self.user, "message": text, "title": "Scan station",
                  "priority": "-1" if kind == "ready" else "0" if safe else "1"}
        if doc and doc.doc_id:
            fields["url"] = f"{self.paperless_url}/documents/{doc.doc_id}/details"
        try:
            with self.opener(urllib.request.Request(PUSHOVER, data=urllib.parse.urlencode(fields).encode()), timeout=15):
                return True
        except (urllib.error.URLError, OSError) as e:
            log.warning("pushover failed: %s", e)
            return False

    def _status(self, kind, text, safe, doc):
        data = {"time": datetime.now(timezone.utc).isoformat(), "kind": kind, "safe": safe, "message": text,
                "doc": doc.id if doc else None}
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.status_path.parent)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.status_path)
