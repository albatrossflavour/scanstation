"""Minimal Paperless-ngx API client: upload, consume task status, the resulting document."""
import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

TIMEOUT = 10


class PaperlessDown(Exception):
    """Paperless couldn't be reached or answered 5xx. Retry later."""


class PaperlessRejected(Exception):
    """Paperless answered 4xx. Retrying the same request won't help."""


class Paperless:
    def __init__(self, base_url, token, opener=urllib.request.urlopen):
        self.base = base_url.rstrip("/")
        self.token = token
        self.opener = opener

    def _call(self, path, data=None, content_type=None):
        req = urllib.request.Request(f"{self.base}/api{path}", data=data, method="POST" if data else "GET")
        req.add_header("Authorization", f"Token {self.token}")
        req.add_header("Accept", "application/json; version=9")
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with self.opener(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                raise PaperlessDown(f"HTTP {e.code}") from e
            raise PaperlessRejected(f"HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise PaperlessDown(str(e)) from e
        return json.loads(raw) if raw else None

    def upload(self, pdf, filename):
        """Post the PDF for consumption. Returns the consume task id. No created date is sent."""
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{filename}\"\r\n"
                f"Content-Type: application/pdf\r\n\r\n").encode() + Path(pdf).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        return self._call("/documents/post_document/", body, f"multipart/form-data; boundary={boundary}")

    def task(self, task_id):
        """The consume task: status (PENDING, STARTED, SUCCESS, FAILURE), result text, document id."""
        rows = self._call(f"/tasks/?task_id={task_id}")
        if not rows:
            return {"status": "PENDING", "result": None, "related_document": None}
        t = rows[0]
        return {"status": t.get("status"), "result": t.get("result"), "related_document": t.get("related_document")}

    def find_by_checksum(self, checksum):
        """Id of a document this user can see with that exact file, or None."""
        rows = self._call(f"/documents/?checksum__iexact={checksum}&fields=id")
        results = rows.get("results", []) if isinstance(rows, dict) else []
        return results[0]["id"] if results else None

    def document(self, doc_id):
        d = self._call(f"/documents/{doc_id}/")
        return {"title": d.get("title"), "content": d.get("content") or "", "tags": d.get("tags") or [],
                "page_count": d.get("page_count")}

    def tag_id(self, name):
        rows = self._call(f"/tags/?name__iexact={urllib.request.quote(name)}")
        results = rows.get("results", []) if isinstance(rows, dict) else []
        return results[0]["id"] if results else None
