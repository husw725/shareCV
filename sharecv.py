"""ShareCV - Cross-platform clipboard synchronization across multiple computers.

Architecture (full rewrite):
  - One node is the SERVER (hub); all others are CLIENTS. Roles are symmetric for
    clipboard handling -- the server participates in sync just like a client.
  - Control plane is a WebSocket: changes are PUSHED instantly (no fixed-interval
    polling of the network), so latency is ~ms and idle traffic is zero.
  - The hub holds a single authoritative state with a monotonically increasing
    `version`; every clipboard item is content-addressed by a SHA-256 `hash`, which
    is used for change detection, de-duplication and skip-if-already-have downloads.
  - Files travel over HTTP, streamed in both directions (never loaded fully in RAM).
  - All blocking clipboard / file work happens off the asyncio event loop, so the
    server never stalls regardless of how many clients are connected.
"""

import argparse
import asyncio
import hashlib
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx
import pyperclip
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

# ================= CONFIG =================
DISCOVERY_PORT = 6098
SERVER_PORT = 6097
DISCOVERY_MESSAGE = b"ShareCV-Server:6097"
MULTICAST_GROUP = "239.255.255.250"
POLL_INTERVAL = 1.0          # local clipboard poll cadence (seconds)
RECONNECT_DELAY = 2.0        # client reconnect backoff (seconds)
CHUNK_SIZE = 1 << 20         # 1 MiB streaming chunks
IMAGE_EXTS = {"png", "jpg", "jpeg", "tiff", "gif", "bmp"}
CACHE_FILE = ".sharecv_cache"

if sys.platform == "darwin":
    # /tmp keeps files reachable for sandboxed apps (DingTalk, WeChat) that read lazily.
    DOWNLOAD_DIR = "/tmp/ShareCV"
else:
    DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sharecv_downloads")

STORE_DIR = os.path.join(DOWNLOAD_DIR, ".store")  # content-addressed file store (by hash)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(STORE_DIR, exist_ok=True)

NODE_ID = uuid.uuid4().hex[:12]


# ================= HASHING =================
def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def is_hex_hash(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


# ================= CLIPBOARD ITEM =================
@dataclass
class ClipItem:
    """A unit of clipboard content. `hash` identifies the content; for files,
    `path` is the local filesystem location and `name` the original basename."""
    type: str               # "text" | "file"
    text: str = ""
    path: str = ""
    name: str = ""
    hash: str = ""

    def signature(self) -> tuple:
        """Identity used for change detection (independent of local path)."""
        return (self.type, self.hash)

    def to_wire(self) -> dict:
        if self.type == "text":
            return {"type": "text", "text": self.text, "hash": self.hash}
        return {"type": "file", "name": self.name, "hash": self.hash}

    @staticmethod
    def from_wire(d: dict) -> "ClipItem":
        if d.get("type") == "file":
            return ClipItem(type="file", name=os.path.basename(d.get("name", "") or "file"),
                            hash=d.get("hash", ""))
        text = d.get("text", "") or ""
        return ClipItem(type="text", text=text, hash=d.get("hash") or hash_text(text))


# ================= CLIPBOARD BACKENDS =================
class ClipboardBackend:
    """Platform-specific clipboard read/write. All methods are BLOCKING and must
    be called off the event loop (via asyncio.to_thread or a worker thread)."""

    def read(self) -> Optional[ClipItem]:
        raise NotImplementedError

    def write(self, item: ClipItem) -> None:
        raise NotImplementedError


class FallbackClipboard(ClipboardBackend):
    """Text-only backend (Linux / unsupported platforms / missing pyobjc)."""

    def read(self) -> Optional[ClipItem]:
        text = pyperclip.paste() or ""
        return ClipItem(type="text", text=text, hash=hash_text(text))

    def write(self, item: ClipItem) -> None:
        if item.type == "text":
            pyperclip.copy(item.text)


class MacClipboard(ClipboardBackend):
    """macOS backend using pure AppKit (no osascript/shell pipelines).

    Uses NSPasteboard.changeCount() as a cheap gate so we only do real work when
    the clipboard actually changed."""

    # Path written by a short-lived subprocess so file-URL data is owned by the
    # pasteboard server (copied eagerly) -- this avoids Finder/WeChat paste hangs
    # that occur when the writing process holds the data lazily. Path is passed as
    # argv (never string-interpolated), so filenames with quotes are safe.
    _URL_WRITER = (
        "import sys\n"
        "from AppKit import NSPasteboard, NSURL\n"
        "pb = NSPasteboard.generalPasteboard()\n"
        "pb.clearContents()\n"
        "pb.writeObjects_([NSURL.fileURLWithPath_(sys.argv[1])])\n"
    )

    def __init__(self):
        from AppKit import NSPasteboard  # noqa: F401 -- fail fast if pyobjc missing
        self._cc = -1
        self._cached: Optional[ClipItem] = None

    def read(self) -> Optional[ClipItem]:
        from AppKit import NSPasteboard
        pb = NSPasteboard.generalPasteboard()
        cc = pb.changeCount()
        if cc == self._cc and self._cached is not None:
            return self._cached
        self._cc = cc

        item = self._read_file_url(pb) or self._read_image(pb) or self._read_text()
        self._cached = item
        return item

    def _read_file_url(self, pb) -> Optional[ClipItem]:
        from AppKit import NSURL, NSPasteboardURLReadingFileURLsOnlyKey
        try:
            urls = pb.readObjectsForClasses_options_(
                [NSURL], {NSPasteboardURLReadingFileURLsOnlyKey: True})
        except Exception:
            urls = None
        if urls:
            path = urls[0].path()
            if path and os.path.exists(path) and os.path.isfile(path):
                return ClipItem(type="file", path=path, name=os.path.basename(path),
                                hash=hash_file(path))
        return None

    def _read_image(self, pb) -> Optional[ClipItem]:
        from AppKit import (NSPasteboardTypePNG, NSPasteboardTypeTIFF,
                            NSBitmapImageRep, NSBitmapImageFileTypePNG)
        kind = pb.availableTypeFromArray_([NSPasteboardTypePNG, NSPasteboardTypeTIFF])
        if kind is None:
            return None
        data = pb.dataForType_(NSPasteboardTypePNG)
        if data is None:
            tiff = pb.dataForType_(NSPasteboardTypeTIFF)
            if tiff is None:
                return None
            rep = NSBitmapImageRep.imageRepWithData_(tiff)
            if rep is None:
                return None
            data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
        if data is None:
            return None
        tmp = os.path.join("/tmp", f"sharecv_clip_{os.getpid()}.png")
        if not data.writeToFile_atomically_(tmp, True):
            return None
        h = hash_file(tmp)
        final = os.path.join("/tmp", f"sharecv_img_{h}.png")
        if os.path.exists(final):
            os.remove(tmp)
        else:
            os.replace(tmp, final)
        return ClipItem(type="file", path=final, name=os.path.basename(final), hash=h)

    def _read_text(self) -> ClipItem:
        text = pyperclip.paste() or ""
        return ClipItem(type="text", text=text, hash=hash_text(text))

    def write(self, item: ClipItem) -> None:
        from AppKit import NSPasteboard, NSImage
        pb = NSPasteboard.generalPasteboard()
        if item.type == "text":
            pyperclip.copy(item.text)
            self._cc = pb.changeCount()
            self._cached = item
            return

        path = os.path.abspath(item.path)
        if not os.path.exists(path):
            print(f"[!] File to copy not found: {path}")
            return

        ext = path.rsplit(".", 1)[-1].lower() if "." in os.path.basename(path) else ""
        if ext in IMAGE_EXTS:
            img = NSImage.alloc().initByReferencingFile_(path)
            if img is not None and img.isValid():
                pb.clearContents()
                pb.writeObjects_([img])
                self._cc = pb.changeCount()
                self._cached = item
                return
        # Non-image files: hand off to a short-lived process (see _URL_WRITER).
        subprocess.run([sys.executable, "-c", self._URL_WRITER, path], check=False)
        self._cc = pb.changeCount()
        self._cached = item


class WindowsClipboard(ClipboardBackend):
    """Windows backend via PowerShell. File paths are passed through the
    SHARECV_PATH environment variable, never interpolated into the script."""

    _READ = (
        "Get-Clipboard -Format FileDropList | ForEach-Object { $_.FullName }"
    )
    _WRITE_FILE = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$p = $env:SHARECV_PATH;"
        "$data = New-Object System.Windows.Forms.DataObject;"
        "$files = New-Object System.Collections.Specialized.StringCollection;"
        "$files.Add($p) | Out-Null;"
        "$data.SetFileDropList($files);"
        "$eff = New-Object byte[] 4; $eff[0] = 5;"
        "$data.SetData('Preferred DropEffect', [System.IO.MemoryStream]::new($eff));"
        "if (@('.png','.jpg','.jpeg','.bmp','.gif','.tiff') -contains [IO.Path]::GetExtension($p).ToLower()) {"
        "  try { $bmp = New-Object System.Drawing.Bitmap($p); $data.SetImage($bmp);"
        "        [System.Windows.Forms.Clipboard]::SetDataObject($data, $true); $bmp.Dispose() }"
        "  catch { [System.Windows.Forms.Clipboard]::SetDataObject($data, $true) }"
        "} else { [System.Windows.Forms.Clipboard]::SetDataObject($data, $true) }"
    )

    def read(self) -> Optional[ClipItem]:
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", self._READ],
                capture_output=True, text=True, check=False)
            if result.returncode == 0 and result.stdout.strip():
                path = result.stdout.strip().splitlines()[0].strip()
                if path and os.path.exists(path) and os.path.isfile(path):
                    return ClipItem(type="file", path=path, name=os.path.basename(path),
                                    hash=hash_file(path))
        except Exception:
            pass
        text = pyperclip.paste() or ""
        return ClipItem(type="text", text=text, hash=hash_text(text))

    def write(self, item: ClipItem) -> None:
        if item.type == "text":
            pyperclip.copy(item.text)
            return
        path = os.path.abspath(item.path)
        if not os.path.exists(path):
            print(f"[!] File to copy not found: {path}")
            return
        env = dict(os.environ, SHARECV_PATH=path)
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", self._WRITE_FILE],
                       env=env, check=False)


def get_backend() -> ClipboardBackend:
    if sys.platform == "darwin":
        try:
            return MacClipboard()
        except Exception as e:
            print(f"[!] pyobjc/AppKit unavailable ({e}); falling back to TEXT-ONLY mode.")
            print("    Install it with:  pip install pyobjc-framework-Cocoa")
            return FallbackClipboard()
    if sys.platform == "win32":
        return WindowsClipboard()
    return FallbackClipboard()


# ================= LOCAL CLIPBOARD (with echo suppression) =================
class LocalClipboard:
    """Wraps a backend. `poll()` reports a change only when content actually
    differs from what we last saw or last applied -- this is what stops the
    sync loop from echoing content back to the network."""

    def __init__(self, backend: ClipboardBackend):
        self.backend = backend
        self._last_sig: Optional[tuple] = None

    def poll(self) -> Optional[ClipItem]:
        item = self.backend.read()
        if item is None:
            return None
        sig = item.signature()
        if sig == self._last_sig:
            return None
        self._last_sig = sig
        return item

    def apply(self, item: ClipItem) -> None:
        self.backend.write(item)
        # Baseline the signature to what we just wrote so the monitor doesn't
        # treat our own write as a fresh local change and rebroadcast it.
        self._last_sig = item.signature()

    def prime(self) -> None:
        """Capture the current signature without emitting a change (used at
        startup so we don't immediately broadcast whatever was already copied)."""
        item = self.backend.read()
        self._last_sig = item.signature() if item else None


class ClipboardMonitor(threading.Thread):
    """Polls the local clipboard on a worker thread and fires `on_change`."""

    def __init__(self, local: LocalClipboard, on_change: Callable[[ClipItem], None],
                 interval: float = POLL_INTERVAL):
        super().__init__(daemon=True)
        self.local = local
        self.on_change = on_change
        self.interval = interval
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                item = self.local.poll()
                if item is not None:
                    self.on_change(item)
            except Exception as e:
                print(f"[!] Clipboard monitor error: {type(e).__name__}: {e}")
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


# ================= NETWORK DISCOVERY =================
def discover_server(timeout=2.0) -> Optional[str]:
    """Listen for the server's UDP/multicast announcement."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if sys.platform != "win32":
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
    try:
        sock.bind(("", DISCOVERY_PORT))
        try:
            mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass
        sock.settimeout(timeout)
        data, addr = sock.recvfrom(1024)
        if data == DISCOVERY_MESSAGE:
            port = data.decode().split(":")[1]
            url = f"http://{addr[0]}:{port}"
            print(f"[+] Found server at {url}")
            return url
    except Exception:
        pass
    finally:
        sock.close()
    return None


def udp_broadcaster(stop: threading.Event):
    """Announce server presence for client auto-discovery."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    print(f"[*] Discovery broadcaster started on port {DISCOVERY_PORT}")
    while not stop.is_set():
        try:
            sock.sendto(DISCOVERY_MESSAGE, ("<broadcast>", DISCOVERY_PORT))
            sock.sendto(DISCOVERY_MESSAGE, (MULTICAST_GROUP, DISCOVERY_PORT))
            try:
                for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                    if ip.startswith("127."):
                        continue
                    subnet = ".".join(ip.split(".")[:-1]) + ".255"
                    sock.sendto(DISCOVERY_MESSAGE, (subnet, DISCOVERY_PORT))
            except Exception:
                pass
        except Exception:
            pass
        stop.wait(1.0)
    sock.close()


def save_cache(url: str):
    try:
        with open(CACHE_FILE, "w") as f:
            f.write(url)
    except Exception:
        pass


def load_cache() -> Optional[str]:
    try:
        with open(CACHE_FILE) as f:
            return f.read().strip() or None
    except Exception:
        return None


# ================= FILE STORE =================
def store_path(file_hash: str) -> str:
    return os.path.join(STORE_DIR, file_hash)


def ingest_local_file(item: ClipItem) -> None:
    """Copy a locally-originated file into the content-addressed store."""
    dest = store_path(item.hash)
    if not os.path.exists(dest) and os.path.exists(item.path):
        tmp = dest + ".part"
        shutil.copy2(item.path, tmp)
        os.replace(tmp, dest)


def materialize(item: ClipItem) -> Optional[str]:
    """Produce a real, well-named file path from a stored item so it can be
    placed on the clipboard. Returns None if the content isn't in the store."""
    src = store_path(item.hash)
    if not os.path.exists(src):
        return None
    dest = os.path.join(DOWNLOAD_DIR, os.path.basename(item.name) or item.hash)
    if not (os.path.exists(dest) and hash_file(dest) == item.hash):
        shutil.copy2(src, dest)
    return dest


# ================= SERVER (HUB) =================
class Hub:
    """Authoritative clipboard state + connected client registry."""

    def __init__(self):
        self.version = 0
        self.item = ClipItem(type="text", text="", hash=hash_text(""))
        self.conns: dict[WebSocket, str] = {}
        self.lock = asyncio.Lock()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.local: Optional[LocalClipboard] = None

    async def register(self, ws: WebSocket, node_id: str):
        self.conns[ws] = node_id
        await ws.send_text(json.dumps({
            "kind": "state", "version": self.version, "origin": NODE_ID,
            "item": self.item.to_wire(),
        }))

    def unregister(self, ws: WebSocket):
        self.conns.pop(ws, None)

    async def _broadcast(self, payload: dict, exclude: Optional[WebSocket] = None):
        msg = json.dumps(payload)
        dead = []
        for ws in list(self.conns):
            if ws is exclude:
                continue
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.unregister(ws)

    async def publish(self, item: ClipItem, origin: str, exclude: Optional[WebSocket] = None,
                      apply_local: bool = True):
        """Set new state, bump version, push to everyone (except the origin conn),
        and reflect it onto the hub machine's own clipboard."""
        async with self.lock:
            if item.signature() == self.item.signature():
                return
            self.version += 1
            self.item = item
            version = self.version
        print(f"[hub] v{version} <- {origin}: {item.type} {item.name or item.text[:40]!r}")
        await self._broadcast({
            "kind": "update", "version": version, "origin": origin,
            "item": item.to_wire(),
        }, exclude=exclude)
        if apply_local and self.local is not None:
            await self._apply_local(item)

    async def _apply_local(self, item: ClipItem):
        if item.type == "file":
            path = await asyncio.to_thread(materialize, item)
            if path is None:
                return
            item = ClipItem(type="file", path=path, name=item.name, hash=item.hash)
        await asyncio.to_thread(self.local.apply, item)


hub = Hub()
app = FastAPI()


@app.get("/")
async def health():
    return {"status": "ok", "version": hub.version, "node": NODE_ID}


@app.post("/upload/{file_hash}")
async def upload(file_hash: str, request: Request):
    if not is_hex_hash(file_hash):
        return JSONResponse({"error": "bad hash"}, status_code=400)
    dest = store_path(file_hash)
    if not os.path.exists(dest):
        tmp = dest + f".{uuid.uuid4().hex}.part"
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        os.replace(tmp, dest)
    return {"status": "ok", "hash": file_hash}


@app.get("/file/{file_hash}")
async def download(file_hash: str, name: str = "file"):
    if not is_hex_hash(file_hash):
        return JSONResponse({"error": "bad hash"}, status_code=400)
    path = store_path(file_hash)
    if not os.path.exists(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, filename=os.path.basename(name))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    node_id = "unknown"
    try:
        hello = json.loads(await ws.receive_text())
        node_id = hello.get("node_id", "unknown")
        await hub.register(ws, node_id)
        print(f"[hub] client connected: {node_id} ({len(hub.conns)} total)")
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("kind") == "update":
                item = ClipItem.from_wire(msg.get("item", {}))
                await hub.publish(item, origin=node_id, exclude=ws)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[hub] ws error from {node_id}: {type(e).__name__}: {e}")
    finally:
        hub.unregister(ws)
        print(f"[hub] client disconnected: {node_id} ({len(hub.conns)} total)")


@app.on_event("startup")
async def on_startup():
    hub.loop = asyncio.get_running_loop()
    hub.local = LocalClipboard(get_backend())
    hub.local.prime()

    def on_change(item: ClipItem):
        if item.type == "file":
            ingest_local_file(item)
        # Hand the change to the event loop; don't apply_local (it's already here).
        asyncio.run_coroutine_threadsafe(
            hub.publish(item, origin=NODE_ID, apply_local=False), hub.loop)

    monitor = ClipboardMonitor(hub.local, on_change)
    monitor.start()
    app.state.monitor = monitor


@app.on_event("shutdown")
async def on_shutdown():
    mon = getattr(app.state, "monitor", None)
    if mon:
        mon.stop()


# ================= CLIENT =================
async def fetch_file(client: httpx.AsyncClient, server_url: str, item: ClipItem) -> bool:
    """Ensure item's content is in the local store, downloading if needed."""
    dest = store_path(item.hash)
    if os.path.exists(dest):
        return True
    url = f"{server_url}/file/{item.hash}"
    tmp = dest + f".{uuid.uuid4().hex}.part"
    try:
        async with client.stream("GET", url, params={"name": item.name}) as resp:
            if resp.status_code != 200:
                print(f"[!] Download failed: HTTP {resp.status_code}")
                return False
            with open(tmp, "wb") as f:
                async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                    f.write(chunk)
        os.replace(tmp, dest)
        return True
    except Exception as e:
        print(f"[!] Download error: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)
        return False


async def push_file(client: httpx.AsyncClient, server_url: str, item: ClipItem):
    """Upload a locally-copied file to the hub (streamed)."""
    ingest_local_file(item)
    src = store_path(item.hash)

    async def gen():
        # httpx AsyncClient needs an async body; offload blocking reads to a thread.
        with open(src, "rb") as f:
            while True:
                chunk = await asyncio.to_thread(f.read, CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    await client.post(f"{server_url}/upload/{item.hash}", content=gen(), timeout=None)


async def recv_loop(ws, client, server_url, local: LocalClipboard, seen: dict):
    async for raw in ws:
        msg = json.loads(raw)
        kind = msg.get("kind")
        if kind not in ("update", "state"):
            continue
        version = msg.get("version", 0)
        if version <= seen["version"]:
            continue
        seen["version"] = version
        item = ClipItem.from_wire(msg.get("item", {}))
        if item.type == "file":
            if not item.hash:
                continue
            if not await fetch_file(client, server_url, item):
                continue
            path = await asyncio.to_thread(materialize, item)
            if path is None:
                continue
            item = ClipItem(type="file", path=path, name=item.name, hash=item.hash)
            print(f"[v{version}] received file: {item.name}")
        else:
            print(f"[v{version}] received text: {item.text[:50]!r}")
        await asyncio.to_thread(local.apply, item)


async def send_loop(ws, client, server_url, outq: asyncio.Queue):
    while True:
        item: ClipItem = await outq.get()
        try:
            if item.type == "file":
                print(f"[^] sending file: {item.name}")
                await push_file(client, server_url, item)
            else:
                print(f"[^] sending text: {item.text[:50]!r}")
            await ws.send(json.dumps({
                "kind": "update", "origin": NODE_ID, "item": item.to_wire(),
            }))
        except Exception as e:
            print(f"[!] Send failed: {type(e).__name__}: {e}")


async def run_client(server_url: str):
    backend = get_backend()
    local = LocalClipboard(backend)
    local.prime()
    ws_url = server_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    loop = asyncio.get_running_loop()

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            monitor = None
            try:
                async with ws_connect(ws_url, max_size=None) as ws:
                    await ws.send(json.dumps({"kind": "hello", "node_id": NODE_ID}))
                    print(f"[+] Connected to {server_url} (node {NODE_ID}). Ctrl+C to stop.")
                    save_cache(server_url)
                    seen = {"version": 0}
                    outq: asyncio.Queue = asyncio.Queue()

                    def on_change(item: ClipItem):
                        loop.call_soon_threadsafe(outq.put_nowait, item)

                    monitor = ClipboardMonitor(local, on_change)
                    monitor.start()

                    recv = asyncio.create_task(recv_loop(ws, client, server_url, local, seen))
                    send = asyncio.create_task(send_loop(ws, client, server_url, outq))
                    done, pending = await asyncio.wait(
                        [recv, send], return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
            except (ConnectionClosed, OSError, ConnectionError) as e:
                print(f"[!] Connection lost ({type(e).__name__}); reconnecting in {RECONNECT_DELAY}s...")
            except Exception as e:
                print(f"[!] Client error: {type(e).__name__}: {e}; reconnecting...")
            finally:
                if monitor:
                    monitor.stop()
            await asyncio.sleep(RECONNECT_DELAY)


# ================= ENTRYPOINT =================
def main():
    parser = argparse.ArgumentParser(
        description="ShareCV - Cross-platform clipboard synchronization")
    parser.add_argument("server", nargs="?", default=None,
                        help="Server IP or URL to connect to directly (skips auto-discovery)")
    args = parser.parse_args()

    server_url = None
    if args.server:
        server_url = args.server if args.server.startswith("http") else f"http://{args.server}:{SERVER_PORT}"
        print(f"[+] Manual server URL: {server_url}")
    else:
        print("[*] Looking for an existing ShareCV server on the local network...")
        server_url = discover_server(timeout=2.0)
        if not server_url:
            cached = load_cache()
            if cached:
                print(f"[*] Discovery failed; trying cached server {cached}...")
                try:
                    if httpx.get(f"{cached}/", timeout=1.0).status_code == 200:
                        print("[+] Cached server is alive!")
                        server_url = cached
                except Exception:
                    pass

    if server_url:
        print(f"[+] CLIENT mode -> {server_url}")
        try:
            asyncio.run(run_client(server_url))
        except KeyboardInterrupt:
            print("\n[*] Stopped clipboard sync.")
    else:
        print("[+] No server found. Starting in SERVER mode (hub).")
        stop = threading.Event()
        threading.Thread(target=udp_broadcaster, args=(stop,), daemon=True).start()
        try:
            uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT, workers=1,
                        reload=False, log_level="warning")
        finally:
            stop.set()


if __name__ == "__main__":
    main()
