import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

from scanstation.failsafe import Failsafe
from scanstation.notify import Notifier
from scanstation.spool import Doc


def doc(**kw):
    base = dict(id="d1", filename="scanstation-20261028-213012.pdf", pages=2, created_at="2026-10-28T21:30:12+00:00",
                title="Council Rates Notice", doc_id=77)
    return Doc(**{**base, **kw})


class NotifierTest(unittest.TestCase):
    def setUp(self):
        self.status = Path(tempfile.mkdtemp()) / "status.json"
        self.sent = []

        def opener(req, timeout=None):
            self.sent.append(dict(urllib.parse.parse_qsl(req.data.decode())))
            return io.BytesIO(b'{"status":1}')
        self.n = Notifier("apptoken", "userkey", "https://paperless.example", self.status, opener=opener)

    def test_safe_message(self):
        self.assertTrue(self.n("safe", doc()))
        m = self.sent[0]
        self.assertEqual((m["token"], m["user"], m["priority"]), ("apptoken", "userkey", "0"))
        self.assertTrue(m["message"].startswith("Safe to shred:"))
        self.assertIn("Council Rates Notice", m["message"])
        self.assertIn("2 pages", m["message"])
        self.assertEqual(m["url"], "https://paperless.example/documents/77/details")

    def test_hold_messages_are_high_priority_and_say_hold(self):
        for kind in ("hold_check", "hold_pi_only", "hold_rejected", "hold_scan"):
            self.n(kind, doc(flags=["edge"]), "jam or double feed, rescan the whole document")
        for m in self.sent:
            self.assertEqual(m["priority"], "1")
            self.assertTrue(m["message"].startswith("HOLD this stack:"), m["message"])

    def test_filename_title_reads_as_untitled(self):
        self.n("safe", doc(title="scanstation-20261028-111017"))
        self.assertTrue(self.sent[0]["message"].startswith("Safe to shred: Untitled scan (2 pages)"))

    def test_ready_message_is_quiet(self):
        self.assertTrue(self.n("ready", None))
        self.assertEqual(self.sent[0]["priority"], "-1")
        self.assertEqual(self.sent[0]["message"], "Scanner ready")

    def test_failsafe_message_says_safe(self):
        n = Notifier("t", "u", "https://p", self.status, opener=self.n.opener, failsafe_name="the NAS")
        n("stored_on_failsafe", doc(doc_id=None))
        self.assertTrue(self.sent[0]["message"].startswith("Safe to shred:"))
        self.assertIn("is on the NAS", self.sent[0]["message"])

    def test_default_failsafe_name(self):
        self.n("hold_pi_only", doc())
        self.assertIn("Paperless and the failsafe share are both unreachable", self.sent[0]["message"])

    def test_no_failsafe_hold_says_so(self):
        n = Notifier("t", "u", "https://p", self.status, opener=self.n.opener, failsafe_name=None)
        n("hold_pi_only", doc())
        self.assertIn("Paperless is unreachable and there's no failsafe share", self.sent[0]["message"])
        self.assertIn("only on the Pi", self.sent[0]["message"])

    def test_no_pushover_logs_only(self):
        def never(req, timeout=None):
            raise AssertionError("Pushover called with no keys")
        n = Notifier("", "", "https://p", self.status, opener=never)
        self.assertTrue(n("safe", doc()))
        self.assertEqual(json.loads(self.status.read_text())["kind"], "safe")

    def test_status_file_written_even_when_pushover_fails(self):
        def down(req, timeout=None):
            raise urllib.error.URLError("no route")
        n = Notifier("t", "u", "https://p", self.status, opener=down)
        self.assertFalse(n("hold_pi_only", doc()))
        s = json.loads(self.status.read_text())
        self.assertEqual(s["kind"], "hold_pi_only")
        self.assertFalse(s["safe"])


class FailsafeTest(unittest.TestCase):
    def setUp(self):
        self.pdf = Path(tempfile.mkdtemp()) / "document.pdf"
        self.pdf.write_bytes(b"%PDF")

    def test_copy_and_remove(self):
        share = Path(tempfile.mkdtemp())
        fs = Failsafe(share)
        path = fs.copy(self.pdf, "scanstation-x.pdf")
        self.assertEqual(path.read_bytes(), b"%PDF")
        fs.remove(path)
        self.assertFalse(path.exists())

    def test_archive_files_by_year(self):
        from scanstation.failsafe import Archive
        share = Path(tempfile.mkdtemp())
        path = Archive(share).copy(self.pdf, "scanstation-20261028-213012.pdf")
        self.assertEqual(path, share / "2026" / "scanstation-20261028-213012.pdf")
        self.assertEqual(path.read_bytes(), b"%PDF")

    def test_unconfigured_or_missing_share(self):
        self.assertIsNone(Failsafe(None).copy(self.pdf, "x.pdf"))
        self.assertIsNone(Failsafe(Path("/nonexistent/failsafe")).copy(self.pdf, "x.pdf"))


if __name__ == "__main__":
    unittest.main()
