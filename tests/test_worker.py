import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scanstation.paperless import PaperlessDown, PaperlessRejected
from scanstation.spool import Spool
from scanstation.worker import step

T0 = datetime(2026, 10, 28, 21, 0, tzinfo=timezone.utc)
GPT_TAG = 534


class FakePaperless:
    def __init__(self):
        self.up = "ok"            # "ok", "down" or "rejected"
        self.status = "SUCCESS"
        self.result = "Success. New document id 77 created"
        self.content = "x" * 400
        self.tags = []
        self.uploads = 0
        self.existing = {}

    def upload(self, pdf, filename):
        if self.up == "down":
            raise PaperlessDown("down")
        if self.up == "rejected":
            raise PaperlessRejected("HTTP 400")
        self.uploads += 1
        return f"task-{self.uploads}"

    def task(self, task_id):
        if self.up == "down":
            raise PaperlessDown("down")
        return {"status": self.status, "result": self.result, "related_document": "77" if self.status == "SUCCESS" else None}

    def find_by_checksum(self, checksum):
        return self.existing.get(checksum)

    def document(self, doc_id):
        return {"title": "Council Rates Notice", "content": self.content, "tags": self.tags, "page_count": 2}


class FakeFailsafe:
    def __init__(self, works=True):
        self.works, self.copies, self.removed = works, [], []

    def copy(self, pdf, name):
        if not self.works:
            return None
        self.copies.append(name)
        return Path(f"/mnt/failsafe/{name}")

    def remove(self, path):
        self.removed.append(path)


class FakeNotify:
    def __init__(self, works=True):
        self.sent, self.works = [], works

    def __call__(self, kind, doc, detail=""):
        if self.works:
            self.sent.append(kind)
        return self.works


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.spool = Spool(Path(tempfile.mkdtemp()))
        self.p, self.fs, self.n = FakePaperless(), FakeFailsafe(), FakeNotify()
        self.doc = self.spool.new(b"%PDF", pages=2, now=T0)

    def run_for(self, minutes, every=15, start=0):
        """Step the doc every `every` seconds from `start` to `minutes` after it was scanned."""
        t = start
        while t <= minutes * 60:
            self.doc = step(self.doc, paperless=self.p, spool=self.spool, notify=self.n, failsafe=self.fs,
                            now=T0 + timedelta(seconds=t), gpt_tag=GPT_TAG)
            t += every
        return self.doc

    def test_new_uploads(self):
        self.run_for(0)
        self.assertEqual(self.doc.state, "uploaded")
        self.assertEqual(self.doc.task_id, "task-1")

    def test_new_retries_quietly_under_five_minutes(self):
        self.p.up = "down"
        self.run_for(4)
        self.assertEqual(self.doc.state, "new")
        self.assertEqual(self.n.sent, [])
        self.assertGreater(self.doc.attempts, 1)
        self.assertLess(self.doc.attempts, 30)  # backed off, not hammering

    def test_new_goes_to_failsafe_after_five_minutes(self):
        self.p.up = "down"
        self.run_for(6)
        self.assertEqual(self.doc.state, "failsafe")
        self.assertEqual(self.n.sent, ["stored_on_failsafe"])

    def test_failsafe_unavailable_does_not_block(self):
        self.p.up = "down"
        self.fs.works = False
        self.run_for(10)
        self.assertEqual(self.doc.state, "new")
        self.assertEqual(self.n.sent, ["hold_pi_only"])

    def test_stored_when_consumed_with_text(self):
        self.run_for(0)
        self.run_for(0, start=0)
        self.assertEqual(self.doc.state, "stored")
        self.assertEqual(self.doc.doc_id, 77)

    def test_unreadable_text_is_flagged(self):
        self.p.content = "ab"
        self.run_for(4)
        self.assertEqual(self.doc.state, "done")
        self.assertIn("unreadable", self.doc.flags)
        self.assertEqual(self.n.sent, ["hold_check"])

    def test_consume_failure_is_terminal(self):
        self.p.status, self.p.result = "FAILURE", "boom"
        self.run_for(2)
        self.assertEqual(self.doc.state, "rejected")
        self.assertEqual(self.n.sent, ["hold_rejected"])
        self.assertEqual(len(self.fs.copies), 1)
        uploads = self.p.uploads
        self.run_for(10, start=150)
        self.assertEqual(self.p.uploads, uploads)  # no endless retry

    def test_duplicate_is_rejected(self):
        self.p.result = "Not consuming scanstation-x.pdf: It is a duplicate of Council Rates Notice (#12)."
        self.run_for(2)
        self.assertEqual(self.doc.state, "rejected")
        self.assertEqual(self.n.sent, ["hold_rejected"])

    def test_upload_rejected_outright(self):
        self.p.up = "rejected"
        self.run_for(1)
        self.assertEqual(self.doc.state, "rejected")
        self.assertEqual(self.n.sent, ["hold_rejected"])

    def test_safe_after_gpt_finishes(self):
        self.p.tags = [12]  # paperless-gpt-auto already gone
        self.run_for(1)
        self.assertEqual(self.doc.state, "done")
        self.assertEqual(self.n.sent, ["safe"])
        self.assertEqual(self.doc.title, "Council Rates Notice")

    def test_safe_after_three_minutes_even_if_gpt_is_slow(self):
        self.p.tags = [GPT_TAG]
        self.run_for(2)
        self.assertEqual(self.doc.state, "stored")
        self.run_for(4, start=135)
        self.assertEqual(self.doc.state, "done")
        self.assertEqual(self.n.sent, ["safe"])

    def test_page_flags_turn_safe_into_hold(self):
        self.doc = self.spool.update(self.doc, flags=["edge"])
        self.run_for(4)
        self.assertEqual(self.n.sent, ["hold_check"])

    def test_failsafe_copy_removed_once_paperless_has_it(self):
        self.p.up = "down"
        self.run_for(6)
        self.p.up = "ok"
        self.run_for(12, start=375)
        self.assertEqual(self.doc.state, "done")
        self.assertEqual(self.fs.removed, [Path(f"/mnt/failsafe/{self.doc.filename}")])
        self.assertEqual(self.n.sent, ["stored_on_failsafe", "safe"])

    def test_notify_failure_retries_without_skipping(self):
        self.n.works = False
        self.run_for(4)
        self.assertEqual(self.doc.state, "stored")  # safe not delivered, so not done
        self.n.works = True
        self.run_for(5, start=255)
        self.assertEqual(self.n.sent, ["safe"])

    def test_never_safe_without_storage(self):
        scenarios = [("down", False), ("down", True), ("rejected", True), ("ok", True)]
        for up, failsafe in scenarios:
            with self.subTest(up=up, failsafe=failsafe):
                self.setUp()
                self.p.up, self.fs.works = up, failsafe
                self.run_for(10)
                for kind in self.n.sent:
                    if kind == "safe":
                        self.assertIsNotNone(self.doc.doc_id)
                    if kind == "stored_on_failsafe":
                        self.assertTrue(self.fs.copies)

    def test_review_stuck_in_uploaded_gives_up(self):
        # Finding 2: a task Paperless never lists must not poll forever.
        self.p.status = "PENDING"
        self.run_for(70, every=60)
        self.assertEqual(self.doc.state, "rejected")
        self.assertIn("hold_rejected", self.n.sent)

    def test_review_duplicate_of_itself_is_stored(self):
        # Finding 3: a re-upload after a crash finds our own earlier upload, which is a success.
        import hashlib
        checksum = hashlib.sha256(b"%PDF").hexdigest()
        self.p.existing[checksum] = 77
        self.p.result = "Not consuming scanstation-x.pdf: It is a duplicate of Council Rates Notice (#77)."
        self.run_for(4)
        self.assertEqual(self.doc.state, "done")
        self.assertEqual(self.doc.doc_id, 77)
        self.assertEqual(self.n.sent, ["safe"])

    def test_review_failsafe_message_retried_after_pushover_fails(self):
        # Finding 4: a failed stored_on_failsafe message must be resent.
        self.p.up = "down"
        self.n.works = False
        self.run_for(6)
        self.n.works = True
        self.run_for(7, start=375)
        self.assertEqual(self.n.sent, ["stored_on_failsafe"])


if __name__ == "__main__":
    unittest.main()
