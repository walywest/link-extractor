"""aria2c daemon wrapper using JSON-RPC.

Speed-tuned defaults (see design doc for reasoning):
  - ONE connection per file — see below; the CDN can't do multi-connection
  - up to N files downloading at once (parallelism comes from concurrent files)
  - falloc for instant NTFS pre-allocation, prevents fragmentation
  - 64 MB disk cache to batch writes

Why one connection per file
---------------------------
fuckingfast's signed download host (Cloudflare-fronted) mishandles bounded
HTTP Range requests: for a request like ``Range: bytes=450887680-451936255``
it returns the correct 1 MB *body* but a ``Content-Range`` whose end is always
the file's EOF (``450887680-524287999``), not the requested end. aria2's
multi-connection mode splits a file into bounded segments and strictly
validates each ``Content-Range`` end, so it rejects every such response with
"Invalid range header" and the download errors — which is exactly what wedged
resume for partially-downloaded parts. An *open-ended* request
(``Range: bytes=450887680-``), which is what a single connection issues, is
answered correctly, so a single connection downloads and resumes reliably.
Throughput is recovered by downloading several files concurrently instead.
"""
from __future__ import annotations
import io
import os
import shutil
import socket
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path

import requests

if __package__ in (None, ""):
    from paths import VENDOR_DIR
else:
    from .paths import VENDOR_DIR

# Windows ships a vendored aria2c.exe; every other platform uses the system
# aria2c on PATH (installed via nix/apt/brew). The name has no .exe off Windows
# so the vendored Windows binary is never picked up on Linux/macOS.
_ARIA2_EXE = "aria2c.exe" if os.name == "nt" else "aria2c"
ARIA2_PATH = VENDOR_DIR / _ARIA2_EXE

ARIA2_RELEASE_URL = (
    "https://github.com/aria2/aria2/releases/download/"
    "release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"
)
ARIA2_SHA256 = "67d015301eef0b612191212d564c5bb0a14b5b9c4796b76454276a4d28d9b288"

# Fake a real Firefox UA so origin servers don't fingerprint aria2c
DOWNLOAD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) "
    "Gecko/20100101 Firefox/135.0"
)


class Aria2NotFound(Exception):
    pass


class Aria2RpcError(Exception):
    pass


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ensure_aria2c() -> Path:
    """Return a runnable aria2c: vendored binary, then PATH, then (Windows) download."""
    # 1. Vendored binary (the Windows release ships one next to the app).
    if ARIA2_PATH.exists():
        return ARIA2_PATH
    # 2. System aria2c on PATH — the normal case on Linux/macOS/Nix.
    found = shutil.which("aria2c") or shutil.which("aria2c.exe")
    if found:
        return Path(found)
    # 3. Only Windows can bootstrap itself by downloading the official build.
    if os.name != "nt":
        raise Aria2NotFound(
            "aria2c not found. Install it with your package manager "
            "(e.g. `nix profile install nixpkgs#aria2`, `apt install aria2`, "
            "`brew install aria2`) so it's on your PATH."
        )
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    import hashlib

    with urllib.request.urlopen(ARIA2_RELEASE_URL, timeout=120) as r:
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != ARIA2_SHA256:
        raise Aria2NotFound(
            f"aria2 zip hash mismatch (expected {ARIA2_SHA256}, got {got})"
        )
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.endswith("aria2c.exe"):
                with z.open(name) as src, open(ARIA2_PATH, "wb") as dst:
                    dst.write(src.read())
                break
    if not ARIA2_PATH.exists():
        raise Aria2NotFound("aria2c.exe not found in downloaded zip")
    return ARIA2_PATH


class Downloader:
    def __init__(self, connections_per_file: int = 16, concurrent_files: int = 3) -> None:
        self.connections_per_file = max(1, min(32, connections_per_file))
        self.concurrent_files = max(1, min(6, concurrent_files))
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.secret = "fgdl-" + os.urandom(8).hex()

    def start(self) -> None:
        if self.proc is not None:
            return
        exe = ensure_aria2c()
        self.port = _find_free_port()
        args = [
            str(exe),
            "--enable-rpc=true",
            f"--rpc-listen-port={self.port}",
            f"--rpc-secret={self.secret}",
            "--rpc-listen-all=false",
            "--rpc-allow-origin-all=false",
            # One connection per file: this CDN returns a to-EOF Content-Range
            # for bounded segment requests, which aria2 rejects as an invalid
            # range. Open-ended single-connection requests are honored. See the
            # module docstring. connections_per_file is intentionally ignored.
            "--max-connection-per-server=1",
            "--split=1",
            "--min-split-size=1M",
            f"--max-concurrent-downloads={self.concurrent_files}",
            # No preallocation. falloc was only worthwhile for multi-connection
            # random writes; single-connection downloads write straight through.
            # It also can't run on every filesystem — fallocate() fails with
            # "Operation not supported" on a WSL drvfs mount (/mnt/c), which
            # killed every part when the TUI ran under WSL. "none" works
            # everywhere, and without preallocation a partial's on-disk size is
            # its real progress, so the full-size "ghost" hazard disappears too.
            "--file-allocation=none",
            "--disk-cache=64M",
            "--continue=true",
            "--auto-file-renaming=false",
            "--allow-overwrite=false",
            "--check-certificate=true",
            "--console-log-level=warn",
            "--summary-interval=0",
            "--no-conf=true",
            f"--user-agent={DOWNLOAD_UA}",
            "--referer=https://fuckingfast.co/",
            "--max-tries=5",
            "--retry-wait=3",
            "--connect-timeout=15",
            "--timeout=60",
        ]
        creation = 0
        if os.name == "nt":
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation,
        )
        deadline = time.time() + 10
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                self._rpc("aria2.getVersion")
                return
            except Exception as e:
                last_err = e
                time.sleep(0.2)
        self.stop()
        raise Aria2RpcError(f"aria2c RPC did not start in time: {last_err}")

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self._rpc("aria2.shutdown")
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def _rpc(self, method: str, *params):
        assert self.port is not None
        payload = {
            "jsonrpc": "2.0",
            "id": "fgdl",
            "method": method,
            "params": [f"token:{self.secret}"] + list(params),
        }
        r = requests.post(
            f"http://127.0.0.1:{self.port}/jsonrpc", json=payload, timeout=15
        )
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise Aria2RpcError(data["error"])
        return data["result"]

    def add(self, url: str, out_dir: str, filename: str) -> str:
        opts = {
            "dir": str(out_dir),
            "out": filename,
        }
        return self._rpc("aria2.addUri", [url], opts)

    def change_url(self, gid: str, old_url: str, new_url: str) -> None:
        # aria2.changeUri: gid, fileIndex(1-based), delUris, addUris
        self._rpc("aria2.changeUri", gid, 1, [old_url], [new_url])

    def pause(self, gid: str):
        return self._rpc("aria2.pause", gid)

    def unpause(self, gid: str):
        return self._rpc("aria2.unpause", gid)

    def remove(self, gid: str) -> None:
        for method in ("aria2.remove", "aria2.forceRemove"):
            try:
                self._rpc(method, gid)
                break
            except Exception:
                continue
        try:
            self._rpc("aria2.removeDownloadResult", gid)
        except Exception:
            pass

    def status(self, gid: str) -> dict:
        return self._rpc(
            "aria2.tellStatus",
            gid,
            [
                "gid",
                "status",
                "totalLength",
                "completedLength",
                "downloadSpeed",
                "errorMessage",
                "errorCode",
                "connections",
                "numPieces",
                "pieceLength",
                "files",
            ],
        )

    def pause_all(self):
        return self._rpc("aria2.pauseAll")

    def unpause_all(self):
        return self._rpc("aria2.unpauseAll")

    def global_stat(self) -> dict:
        return self._rpc("aria2.getGlobalStat")
