import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from scanstation.paperless import Paperless, PaperlessDown, PaperlessRejected


class FakeOpener:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return io.BytesIO(json.dumps(r).encode())


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b"{}"))


class PaperlessTest(unittest.TestCase):
    def setUp(self):
        self.pdf = Path(tempfile.mkdtemp()) / "a.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 test")

    def test_upload_sends_multipart_without_created(self):
        op = FakeOpener("4f2a-task")
        task = Paperless("https://p", "tok", opener=op).upload(self.pdf, "scanstation-20261028-213012.pdf")
        self.assertEqual(task, "4f2a-task")
        req = op.requests[0]
        self.assertTrue(req.full_url.endswith("/api/documents/post_document/"))
        self.assertEqual(req.get_header("Accept"), "application/json; version=9")
        self.assertEqual(req.get_header("Authorization"), "Token tok")
        body = req.data.decode("latin-1")
        self.assertIn('name="document"; filename="scanstation-20261028-213012.pdf"', body)
        self.assertIn("%PDF-1.4 test", body)
        self.assertNotIn('name="created"', body)

    def test_consume_failure_is_terminal(self):
        op = FakeOpener([{"status": "FAILURE", "result": "boom", "related_document": None}],
                        [{"status": "SUCCESS", "result": "Not consuming x: It is a duplicate of y (#12)", "related_document": "12"}])
        p = Paperless("https://p", "tok", opener=op)
        self.assertEqual(p.task("t")["status"], "FAILURE")
        t = p.task("t")
        self.assertEqual(t["status"], "SUCCESS")
        self.assertIn("duplicate", t["result"])

    def test_task_not_listed_yet(self):
        p = Paperless("https://p", "tok", opener=FakeOpener([]))
        self.assertEqual(p.task("t")["status"], "PENDING")

    def test_http_errors_raise(self):
        p = Paperless("https://p", "tok", opener=FakeOpener(http_error(502), urllib.error.URLError("down"), http_error(400)))
        with self.assertRaises(PaperlessDown):
            p.task("t")
        with self.assertRaises(PaperlessDown):
            p.task("t")
        with self.assertRaises(PaperlessRejected):
            p.upload(self.pdf, "scanstation-x.pdf")


if __name__ == "__main__":
    unittest.main()
