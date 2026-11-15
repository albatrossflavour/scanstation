"""On-disk queue: one directory per document, holding the PDF and its state.

Every state change is an atomic file replace, so a power cut or restart never loses a scan or
leaves half-written state behind.
"""
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path

KEEP_DONE = timedelta(days=30)
STATES = ("new", "uploaded", "stored", "failsafe", "rejected", "done")


@dataclass
class Doc:
    id: str
    filename: str
    pages: int
    created_at: str
    state: str = "new"
    task_id: str | None = None
    doc_id: int | None = None
    title: str | None = None
    attempts: int = 0
    last_error: str | None = None
    failsafe_path: str | None = None
    stored_at: str | None = None
    finished_at: str | None = None
    next_attempt_at: str | None = None
    flags: list = field(default_factory=list)
    notified: list = field(default_factory=list)


class Spool:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def pdf(self, doc):
        return self.root / doc.id / "document.pdf"

    def new(self, pdf_bytes, pages, now, flags=()):
        stamp = now.strftime("%Y%m%d-%H%M%S")
        doc = Doc(id=f"{stamp}-{uuid.uuid4().hex[:6]}", filename=f"scanstation-{stamp}.pdf",
                  pages=pages, created_at=now.isoformat(), flags=list(flags))
        d = self.root / doc.id
        d.mkdir()
        self._atomic(d / "document.pdf", pdf_bytes)
        self._write(doc)
        return doc

    def update(self, doc, **changes):
        known = {f.name for f in fields(Doc)}
        unknown = set(changes) - known
        if unknown:
            raise TypeError(f"unknown Doc fields: {sorted(unknown)}")
        if "state" in changes and changes["state"] not in STATES:
            raise ValueError(changes["state"])
        doc = Doc(**{**asdict(doc), **changes})
        self._write(doc)
        return doc

    def pending(self):
        """Docs still being worked, oldest first."""
        docs = [self._read(p) for p in sorted(self.root.glob("*/state.json"))]
        return [d for d in docs if d and d.state not in ("done", "rejected")]

    def prune(self, now):
        """Remove finished docs older than 30 days. Failsafe and rejected docs are never pruned."""
        removed = 0
        for p in self.root.glob("*/state.json"):
            d = self._read(p)
            if d and d.state == "done" and d.finished_at and now - datetime.fromisoformat(d.finished_at) > KEEP_DONE:
                for f in p.parent.iterdir():
                    f.unlink()
                p.parent.rmdir()
                removed += 1
        return removed

    def _write(self, doc):
        self._atomic(self.root / doc.id / "state.json", json.dumps(asdict(doc), indent=1).encode())

    @staticmethod
    def _read(path):
        try:
            return Doc(**json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _atomic(path, data):
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
