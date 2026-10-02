"""
Where paper-trading data is persisted.

  LocalStorage — a directory. In GitHub Actions this is a checkout of the
                 `paper-data` branch which the workflow commits back.
  DriveStorage — plain CSV files in one Google Drive folder. Authenticates
                 with an OAuth refresh token created by the setup page
                 (scope: drive.file — the bot can only see files it created).

Both expose read_text / write_text plus CSV helpers.
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

import requests


class Storage:
    name = "base"

    def read_text(self, filename: str) -> str | None:  # pragma: no cover - interface
        raise NotImplementedError

    def write_text(self, filename: str, text: str, mime: str = "text/csv") -> None:  # pragma: no cover
        raise NotImplementedError

    # ── helpers ──────────────────────────────────────────────────────────────

    def read_json(self, filename: str, default=None):
        t = self.read_text(filename)
        return json.loads(t) if t else default

    def write_json(self, filename: str, data) -> None:
        self.write_text(filename, json.dumps(data, indent=2, default=str), mime="application/json")

    def read_csv(self, filename: str) -> list[dict]:
        t = self.read_text(filename)
        return list(csv.DictReader(io.StringIO(t))) if t else []

    def write_csv(self, filename: str, rows: list[dict], cols: list[str]) -> None:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        self.write_text(filename, buf.getvalue())

    def append_csv(self, filename: str, rows: list[dict], cols: list[str]) -> None:
        if not rows and self.read_text(filename) is not None:
            return
        self.write_csv(filename, self.read_csv(filename) + rows, cols)


class LocalStorage(Storage):
    name = "local"

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def read_text(self, filename):
        p = self.root / filename
        return p.read_text() if p.exists() else None

    def write_text(self, filename, text, mime="text/csv"):
        (self.root / filename).write_text(text)


class DriveStorage(Storage):
    name = "drive"
    API = "https://www.googleapis.com/drive/v3/files"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"

    def __init__(self, folder_id: str, client_id: str, client_secret: str, refresh_token: str):
        if not all([folder_id, client_id, client_secret, refresh_token]):
            raise ValueError("Google Drive storage needs folder id, client id/secret and refresh token")
        self.folder_id = folder_id
        self._creds = (client_id, client_secret, refresh_token)
        self._token = None
        self._ids: dict[str, str] = {}
        self._cache: dict[str, str | None] = {}

    def _auth(self) -> dict:
        if not self._token:
            cid, secret, refresh = self._creds
            r = requests.post("https://oauth2.googleapis.com/token", data={
                "client_id": cid, "client_secret": secret, "refresh_token": refresh,
                "grant_type": "refresh_token"}, timeout=20)
            if r.status_code != 200:
                raise PermissionError(f"Google token refresh failed ({r.status_code}): {r.text[:200]}. "
                                      "Reconnect Google Drive on the setup page.")
            self._token = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._token}"}

    def _find(self, filename: str) -> str | None:
        if filename in self._ids:
            return self._ids[filename]
        q = f"name = '{filename}' and '{self.folder_id}' in parents and trashed = false"
        r = requests.get(self.API, params={"q": q, "fields": "files(id,name)", "spaces": "drive"},
                         headers=self._auth(), timeout=20)
        r.raise_for_status()
        files = r.json().get("files", [])
        if files:
            self._ids[filename] = files[0]["id"]
        return self._ids.get(filename)

    def read_text(self, filename):
        if filename in self._cache:
            return self._cache[filename]
        fid = self._find(filename)
        text = None
        if fid:
            r = requests.get(f"{self.API}/{fid}", params={"alt": "media"}, headers=self._auth(), timeout=60)
            r.raise_for_status()
            text = r.content.decode("utf-8")
        self._cache[filename] = text
        return text

    def write_text(self, filename, text, mime="text/csv"):
        fid = self._find(filename)
        body = text.encode("utf-8")
        if fid:
            r = requests.patch(f"{self.UPLOAD}/{fid}", params={"uploadType": "media"},
                               headers={**self._auth(), "Content-Type": mime}, data=body, timeout=120)
        else:
            boundary = "tradebot-paper-boundary"
            meta = json.dumps({"name": filename, "parents": [self.folder_id], "mimeType": mime})
            payload = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                       f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + body + f"\r\n--{boundary}--".encode()
            r = requests.post(self.UPLOAD, params={"uploadType": "multipart", "fields": "id"},
                              headers={**self._auth(), "Content-Type": f"multipart/related; boundary={boundary}"},
                              data=payload, timeout=120)
        r.raise_for_status()
        if not fid:
            self._ids[filename] = r.json()["id"]
        self._cache[filename] = text


class ReadOnlyOverlay(Storage):
    """Reads from `base`, keeps writes in memory. Used for test runs off the main branch."""
    name = "read-only"

    def __init__(self, base: Storage):
        self.base, self.written = base, {}

    def read_text(self, filename):
        return self.written[filename] if filename in self.written else self.base.read_text(filename)

    def write_text(self, filename, text, mime="text/csv"):
        self.written[filename] = text


def from_config(cfg: dict) -> Storage:
    store = _from_config(cfg)
    ref = os.environ.get("GITHUB_REF", "")
    if os.environ.get("GITHUB_ACTIONS") == "true" and ref and ref != "refs/heads/main":
        print(f"Test run from {ref}: results are not saved", flush=True)
        return ReadOnlyOverlay(store)
    return store


def _from_config(cfg: dict) -> Storage:
    st = cfg["storage"]
    if st.get("backend") == "drive":
        return DriveStorage(
            folder_id=st.get("drive_folder_id") or os.environ.get("GOOGLE_DRIVE_FOLDER_ID", ""),
            client_id=os.environ.get("GOOGLE_CLIENT_ID") or st.get("google_client_id", ""),
            client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            refresh_token=os.environ.get("GOOGLE_REFRESH_TOKEN", ""),
        )
    return LocalStorage(os.environ.get("PAPER_DATA_DIR", ".paper-data"))
