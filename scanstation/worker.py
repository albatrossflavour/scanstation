"""Move one spooled document towards Paperless, one state per call.

The rule that matters: nobody is told "safe to shred" until Paperless has the document (stored)
or the failsafe share holds a copy (failsafe). Everything else is a hold, or silence while retrying.
"""
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

from .paperless import PaperlessDown, PaperlessRejected

FAILSAFE_AFTER = timedelta(minutes=5)
GIVE_UP_AFTER = timedelta(hours=1)  # uploaded but never consumed
GPT_WAIT = timedelta(minutes=3)
MIN_TEXT_PER_PAGE = 15
BACKOFF_FIRST, BACKOFF_MAX = 15, 120  # seconds


def _ts(value):
    return datetime.fromisoformat(value) if value else None


def step(doc, *, paperless, spool, notify, failsafe, now, gpt_tag):
    def tell(kind, detail=""):
        """Send a message once per doc. Returns True if it's been delivered, now or before."""
        if kind in doc.notified:
            return True
        if notify(kind, doc, detail):
            nonlocal_update(notified=[*doc.notified, kind])
            return True
        return False

    def nonlocal_update(**changes):
        nonlocal doc
        doc = spool.update(doc, **changes)

    def keep_on_failsafe():
        """Make sure a copy is on the failsafe share. True if one is there."""
        if doc.failsafe_path:
            return True
        path = failsafe.copy(spool.pdf(doc), doc.filename)
        if path:
            nonlocal_update(failsafe_path=str(path))
            return True
        return False

    def reject(reason):
        keep_on_failsafe()
        if tell("hold_rejected", reason):
            nonlocal_update(state="rejected", last_error=reason, finished_at=now.isoformat())
        else:
            nonlocal_update(last_error=reason)

    age = now - _ts(doc.created_at)

    # A failsafe copy means the stack can be shredded; keep trying to say so until Pushover takes it.
    if doc.failsafe_path and doc.state in ("new", "failsafe", "uploaded"):
        tell("stored_on_failsafe")

    if doc.state in ("new", "failsafe"):
        # Not in Paperless yet. After five minutes, get it onto the failsafe share so the stack can be shredded.
        if age >= FAILSAFE_AFTER and doc.state == "new":
            if keep_on_failsafe():
                nonlocal_update(state="failsafe")
                tell("stored_on_failsafe")
            else:
                tell("hold_pi_only")
        due = _ts(doc.next_attempt_at)
        if due and now < due:
            return doc
        try:
            task_id = paperless.upload(spool.pdf(doc), doc.filename)
        except PaperlessRejected as e:
            reject(f"upload refused: {e}")
            return doc
        except PaperlessDown as e:
            wait = min(BACKOFF_FIRST * 2 ** doc.attempts, BACKOFF_MAX)
            nonlocal_update(attempts=doc.attempts + 1, last_error=str(e),
                            next_attempt_at=(now + timedelta(seconds=wait)).isoformat())
            return doc
        nonlocal_update(state="uploaded", task_id=task_id, attempts=doc.attempts + 1, last_error=None,
                        next_attempt_at=None)
        return doc

    if doc.state == "uploaded":
        if age >= FAILSAFE_AFTER and not doc.failsafe_path:
            # Uploaded but not consumed after five minutes: the failsafe share holds the copy meanwhile.
            if keep_on_failsafe():
                tell("stored_on_failsafe")
            else:
                tell("hold_pi_only")
        try:
            task = paperless.task(doc.task_id)
        except PaperlessDown:
            return doc
        result = task.get("result") or ""
        if task["status"] not in ("SUCCESS", "FAILURE") and age >= GIVE_UP_AFTER:
            reject("Paperless never finished consuming it")
            return doc
        if task["status"] == "SUCCESS" and "duplicate" in result.lower():
            # After a crash or timeout the same PDF can be uploaded twice; the first copy is ours.
            try:
                mine = paperless.find_by_checksum(hashlib.sha256(spool.pdf(doc).read_bytes()).hexdigest())
            except PaperlessDown:
                return doc
            if mine:
                task = {**task, "related_document": str(mine)}
                result = ""
        if task["status"] == "FAILURE":
            reject(f"Paperless couldn't consume it: {result[:200]}")
        elif task["status"] == "SUCCESS" and "duplicate" in result.lower():
            reject(f"Paperless already has it: {result[:200]}")
        elif task["status"] == "SUCCESS" and task.get("related_document"):
            doc_id = int(task["related_document"])
            try:
                d = paperless.document(doc_id)
            except PaperlessDown:
                return doc
            flags = list(doc.flags)
            if len(d["content"].strip()) < MIN_TEXT_PER_PAGE * doc.pages and "unreadable" not in flags:
                flags.append("unreadable")
            nonlocal_update(state="stored", doc_id=doc_id, title=d["title"], flags=flags, stored_at=now.isoformat())
        return doc

    if doc.state == "stored":
        if doc.failsafe_path:
            failsafe.remove(Path(doc.failsafe_path))
            nonlocal_update(failsafe_path=None)
        try:
            d = paperless.document(doc.doc_id)
        except PaperlessDown:
            d = None
        classified = d is not None and gpt_tag not in d["tags"]
        if not classified and now - _ts(doc.stored_at) < GPT_WAIT:
            return doc
        if d:
            nonlocal_update(title=d["title"])
        kind = "hold_check" if doc.flags else "safe"
        detail = "" if classified else "not classified yet"
        if tell(kind, detail):
            nonlocal_update(state="done", finished_at=now.isoformat())
        return doc

    return doc
