"""State persistence: local JSON plus a GitHub branch that survives
redeploys.

The container's disk is replaced on every deploy, so state that matters
(the earned-today integral, the terms history, the switch, the audit
log) is saved two ways:

* locally, atomic-write JSON, every sample — free, survives process
  restarts;
* remotely, gzipped JSON force-pushed as a PARENTLESS commit to a state
  branch every couple of minutes — 1.0's trick, kept: no history
  accrues however often it saves, the gzip dodges the API's ~1 MB
  request cap, and a redeploy costs at most the save interval.

3.0 uses its own branch (`v3-state`) so it can never collide with 1.0's
`live-state` or 2.0's `v2-state`. Boot takes whichever copy — local or remote
— was saved last.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import threading
import time

import requests

API = "https://api.github.com"


class StateStore:
    def __init__(self, local_path: str, repo: str | None = None,
                 token: str | None = None, branch: str = "v3-state",
                 session=None, clock=None, save_interval: float = 60.0):
        self.local_path = local_path
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY", "wfco223/Liquidity-rewards")
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.branch = branch
        self.session = session or requests.Session()
        self._clock = clock or time.time
        self.save_interval = save_interval
        self._last_remote_save = 0.0
        self.last_error = ""
        # the background save (2026-09-07: a tap's save of a 12 MB state
        # — gzip, then four GitHub calls — ran inside the web request,
        # long enough for the phone to give up and read "unreachable"
        # while the sale had gone through). One uploader at a time; a
        # tap hands it the newest snapshot and returns at once.
        self._up_lock = threading.Lock()
        self._pend_lock = threading.Lock()
        self._pending: bytes | None = None
        self._pending_flags = (False, False)  # (write the disk, upload) for the pending snapshot
        self._worker: threading.Thread | None = None
        self.behind = 0                      # snapshots handed over, not yet uploaded
        self.dropped = 0                     # older snapshots a newer one replaced
        self.last_save_s = 0.0               # how long the last background save took

    # -- local ---------------------------------------------------------------

    def save_local(self, state: dict) -> bool:
        """Atomic local write; a read-only disk is reported, never fatal."""
        try:
            tmp = self.local_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, separators=(",", ":"))
            os.replace(tmp, self.local_path)
            return True
        except OSError as e:
            self.last_error = f"local save: {e}"
            return False

    def load_local(self) -> dict | None:
        try:
            with open(self.local_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    # -- remote ----------------------------------------------------------------

    def _gh(self, method: str, path: str, body: dict | None = None,
            raw: bool = False):
        headers = {"Authorization": f"Bearer {self.token}",
                   "Accept": ("application/vnd.github.raw+json" if raw
                              else "application/vnd.github+json")}
        r = self.session.request(method, API + path, json=body,
                                 headers=headers, timeout=30)
        return r

    def save_remote(self, state: dict) -> bool:
        """Gzip + parentless commit + force ref update. Any failure is
        recorded and swallowed — persistence must never take the loop down."""
        if not self.token:
            self.last_error = "no GITHUB_TOKEN — saves are local only"
            return False
        return self._upload(json.dumps(state, separators=(",", ":")).encode())

    def save_remote_soon(self, state: dict) -> bool:
        """A tap's remote save, behind the request (see save_soon)."""
        if not self.token:
            self.last_error = "no GITHUB_TOKEN — saves are local only"
            return False
        return self.save_soon(state, local=False, force_remote=True)

    def save_soon(self, state: dict, local: bool = True, force_remote: bool = False) -> bool:
        """THE save (2026-09-07: "there is like a 5 minute delay when I
        do anything" — a 12 MB state written to disk and pushed to
        GitHub inside every tap and every cycle). The snapshot is taken
        now, so what lands is what he saw; the disk write and the
        GitHub upload run on one background thread. The upload goes
        when it is due (save_interval) or forced (a tap). Two snapshots
        before the worker gets to them: the newest wins, the flags add
        up. Returns whether anything was handed over."""
        now = self._clock()
        remote = bool(self.token) and (force_remote
                                       or now - self._last_remote_save >= self.save_interval)
        if remote:
            self._last_remote_save = now
        if not local and not remote:
            return False
        raw = json.dumps(state, separators=(",", ":")).encode()
        if not self.token:
            # nowhere to upload: the disk write is the whole save, and a
            # reader right after (a restart, a test) must find it there
            return self._write_local(raw)
        with self._pend_lock:
            prev = self._pending_flags
            self._pending = raw
            self._pending_flags = (local or prev[0], remote or prev[1])
            self.behind += 1
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._drain, daemon=True,
                                                name="state-save")
                self._worker.start()
        return True

    def wait_remote(self, timeout: float = 10.0) -> bool:
        """Block until the background uploads are done (tests, shutdown)."""
        w = self._worker
        if w is not None and w.is_alive():
            w.join(timeout)
        return not (self._worker is not None and self._worker.is_alive())

    def _drain(self) -> None:
        while True:
            with self._pend_lock:
                raw = self._pending
                local, remote = self._pending_flags
                self._pending = None
                self._pending_flags = (False, False)
                n = self.behind
                self.behind = 0
                if raw is None:
                    self._worker = None
                    return
            t0 = time.time()
            if local:
                self._write_local(raw)
            if remote:
                self._upload(raw)
            self.last_save_s = round(time.time() - t0, 1)
            if n > 1:
                self.dropped += n - 1

    def _write_local(self, raw: bytes) -> bool:
        try:
            tmp = self.local_path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, self.local_path)
            return True
        except OSError as e:
            self.last_error = f"local save: {e}"
            return False

    def _upload(self, raw: bytes) -> bool:
        """The four GitHub calls, one uploader at a time."""
        with self._up_lock:
            try:
                payload = gzip.compress(raw)
                r = self._gh("POST", f"/repos/{self.repo}/git/blobs",
                             {"content": base64.b64encode(payload).decode(),
                              "encoding": "base64"})
                if r.status_code >= 400:
                    raise RuntimeError(f"blob: HTTP {r.status_code}")
                blob = r.json()["sha"]
                r = self._gh("POST", f"/repos/{self.repo}/git/trees",
                             {"tree": [{"path": "state.json", "mode": "100644",
                                        "type": "blob", "sha": blob}]})
                if r.status_code >= 400:
                    raise RuntimeError(f"tree: HTTP {r.status_code}")
                tree = r.json()["sha"]
                r = self._gh("POST", f"/repos/{self.repo}/git/commits",
                             {"message": "v3 state save", "tree": tree})
                if r.status_code >= 400:
                    raise RuntimeError(f"commit: HTTP {r.status_code}")
                sha = r.json()["sha"]
                r = self._gh("PATCH", f"/repos/{self.repo}/git/refs/heads/{self.branch}",
                             {"sha": sha, "force": True})
                if r.status_code == 404 or (r.status_code == 422 and "does not exist"
                                            in (r.text or "").lower()):
                    r = self._gh("POST", f"/repos/{self.repo}/git/refs",
                                 {"ref": f"refs/heads/{self.branch}", "sha": sha})
                if r.status_code >= 400:
                    raise RuntimeError(f"ref: HTTP {r.status_code}")
                self.last_error = ""
                return True
            except Exception as e:  # noqa: BLE001 — never fatal
                self.last_error = f"remote save: {e}"
                return False

    def maybe_save_remote(self, state: dict) -> bool:
        """Throttled remote save — at most one per save_interval."""
        now = self._clock()
        if now - self._last_remote_save < self.save_interval:
            return False
        ok = self.save_remote(state)
        if ok:
            self._last_remote_save = now
        return ok

    def load_remote(self) -> dict | None:
        if not self.token:
            return None
        try:
            r = self._gh("GET", f"/repos/{self.repo}/contents/state.json"
                                f"?ref={self.branch}", raw=True)
            if r.status_code >= 400:
                return None
            data = r.content
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return json.loads(data)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"remote load: {e}"
            return None

    # -- boot ----------------------------------------------------------------------

    def load_best(self) -> dict | None:
        """Whichever copy was saved last — a redeploy has a stale disk and a
        fresh branch; an ordinary restart usually the reverse."""
        candidates = [s for s in (self.load_local(), self.load_remote()) if s]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.get("saved_at") or 0)
