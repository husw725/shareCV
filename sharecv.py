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
import hmac
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
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

try:                      # optional: gives per-interface addresses + netmasks
    import psutil
except ImportError:       # pragma: no cover - degraded interface enumeration
    psutil = None

# ================= CONFIG =================
DISCOVERY_PORT = 6098
SERVER_PORT = 6097
DISCOVERY_MESSAGE = b"ShareCV-Server:6097"
PROBE_MESSAGE = b"ShareCV-Probe"
MULTICAST_GROUP = "239.255.255.250"
POLL_INTERVAL = 1.0          # local clipboard poll cadence (seconds)
RECONNECT_DELAY = 2.0        # client reconnect backoff (seconds)
CHUNK_SIZE = 1 << 20         # 1 MiB streaming chunks
MAX_UPLOAD = 4 << 30         # reject uploads beyond 4 GiB
CLEANUP_DAYS = 7             # purge store/download files older than this at startup
IMAGE_EXTS = {"png", "jpg", "jpeg", "tiff", "gif", "bmp"}
CACHE_FILE = ".sharecv_cache"

# Shared secret for auth. Set via --token / SHARECV_TOKEN; empty = open (warned).
TOKEN = os.environ.get("SHARECV_TOKEN", "")

# Pin the advertised LAN address. Set via --ip / SHARECV_IP; empty = auto-detect.
LAN_IP_PIN = os.environ.get("SHARECV_IP", "")


def token_ok(supplied: str) -> bool:
    return not TOKEN or hmac.compare_digest(supplied or "", TOKEN)


def auth_headers() -> dict:
    return {"X-ShareCV-Token": TOKEN} if TOKEN else {}

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
    """A unit of clipboard content. Text is identified by `hash`; a file item
    carries one or more entries, each a dict {"path", "name", "hash"} where
    `path` is the local filesystem location (empty until materialized)."""
    type: str               # "text" | "file"
    text: str = ""
    hash: str = ""          # text content hash (unused for files)
    files: list = field(default_factory=list)

    def signature(self) -> tuple:
        """Identity used for change detection (independent of local paths)."""
        if self.type == "file":
            return ("file", tuple(f["hash"] for f in self.files))
        return ("text", self.hash)

    def label(self) -> str:
        if self.type == "file":
            return ", ".join(f["name"] for f in self.files)
        return repr(self.text[:50])

    def to_wire(self) -> dict:
        if self.type == "text":
            return {"type": "text", "text": self.text, "hash": self.hash}
        return {"type": "file",
                "files": [{"name": f["name"], "hash": f["hash"]} for f in self.files]}

    @staticmethod
    def from_wire(d: dict) -> "ClipItem":
        if d.get("type") == "file":
            files = [{"path": "",
                      "name": os.path.basename(f.get("name") or "") or "file",
                      "hash": f.get("hash", "")}
                     for f in d.get("files", [])]
            return ClipItem(type="file", files=files)
        text = d.get("text", "") or ""
        return ClipItem(type="text", text=text, hash=d.get("hash") or hash_text(text))


def file_entry(path: str) -> dict:
    return {"path": path, "name": os.path.basename(path), "hash": hash_file(path)}


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
        "pb.writeObjects_([NSURL.fileURLWithPath_(p) for p in sys.argv[1:]])\n"
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
        files = []
        for url in urls or []:
            path = url.path()
            if path and os.path.isfile(path):
                files.append(file_entry(path))
        if files:
            return ClipItem(type="file", files=files)
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
        return ClipItem(type="file",
                        files=[{"path": final, "name": os.path.basename(final), "hash": h}])

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

        paths = []
        for f in item.files:
            path = os.path.abspath(f["path"])
            if os.path.exists(path):
                paths.append(path)
            else:
                print(f"[!] File to copy not found: {path}")
        if not paths:
            return

        if len(paths) == 1:
            path = paths[0]
            ext = path.rsplit(".", 1)[-1].lower() if "." in os.path.basename(path) else ""
            if ext in IMAGE_EXTS:
                img = NSImage.alloc().initByReferencingFile_(path)
                if img is not None and img.isValid():
                    pb.clearContents()
                    pb.writeObjects_([img])
                    self._cc = pb.changeCount()
                    self._cached = item
                    return
        # Non-image / multiple files: hand off to a short-lived process (see _URL_WRITER).
        subprocess.run([sys.executable, "-c", self._URL_WRITER, *paths], check=False)
        self._cc = pb.changeCount()
        self._cached = item


class WindowsClipboard(ClipboardBackend):
    """Windows backend via PowerShell. File paths are passed through the
    SHARECV_PATH environment variable (newline-separated), never interpolated
    into the script.

    Uses GetClipboardSequenceNumber() (the Windows analogue of NSPasteboard's
    changeCount) as a cheap gate, so the PowerShell subprocess and file hashing
    only run when the clipboard actually changed."""

    _READ = (
        "Get-Clipboard -Format FileDropList | ForEach-Object { $_.FullName }"
    )
    _WRITE_FILE = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$paths = $env:SHARECV_PATH -split \"`n\";"
        "$data = New-Object System.Windows.Forms.DataObject;"
        "$files = New-Object System.Collections.Specialized.StringCollection;"
        "foreach ($p in $paths) { $files.Add($p) | Out-Null };"
        "$data.SetFileDropList($files);"
        "$eff = New-Object byte[] 4; $eff[0] = 5;"
        "$data.SetData('Preferred DropEffect', [System.IO.MemoryStream]::new($eff));"
        "if ($paths.Count -eq 1 -and @('.png','.jpg','.jpeg','.bmp','.gif','.tiff') -contains [IO.Path]::GetExtension($paths[0]).ToLower()) {"
        "  try { $bmp = New-Object System.Drawing.Bitmap($paths[0]); $data.SetImage($bmp);"
        "        [System.Windows.Forms.Clipboard]::SetDataObject($data, $true); $bmp.Dispose() }"
        "  catch { [System.Windows.Forms.Clipboard]::SetDataObject($data, $true) }"
        "} else { [System.Windows.Forms.Clipboard]::SetDataObject($data, $true) }"
    )

    def __init__(self):
        import ctypes
        self._user32 = ctypes.windll.user32
        self._seq = -1
        self._cached: Optional[ClipItem] = None

    def read(self) -> Optional[ClipItem]:
        seq = self._user32.GetClipboardSequenceNumber()
        if seq == self._seq and self._cached is not None:
            return self._cached
        self._seq = seq
        item = self._read_files() or self._read_text()
        self._cached = item
        return item

    def _read_files(self) -> Optional[ClipItem]:
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", self._READ],
                capture_output=True, text=True, check=False)
            if result.returncode == 0 and result.stdout.strip():
                files = []
                for line in result.stdout.strip().splitlines():
                    path = line.strip()
                    if path and os.path.isfile(path):
                        files.append(file_entry(path))
                if files:
                    return ClipItem(type="file", files=files)
        except Exception:
            pass
        return None

    def _read_text(self) -> ClipItem:
        text = pyperclip.paste() or ""
        return ClipItem(type="text", text=text, hash=hash_text(text))

    def write(self, item: ClipItem) -> None:
        if item.type == "text":
            pyperclip.copy(item.text)
        else:
            paths = []
            for f in item.files:
                path = os.path.abspath(f["path"])
                if os.path.exists(path):
                    paths.append(path)
                else:
                    print(f"[!] File to copy not found: {path}")
            if not paths:
                return
            env = dict(os.environ, SHARECV_PATH="\n".join(paths))
            subprocess.run(["powershell.exe", "-NoProfile", "-Command", self._WRITE_FILE],
                           env=env, check=False)
        # Baseline the sequence number so our own write isn't re-read as a change.
        self._seq = self._user32.GetClipboardSequenceNumber()
        self._cached = item


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
# Why we don't use the classic "connect a UDP socket to 8.8.8.8 and read back the
# source address" trick: proxy clients running in TUN mode (XiGua / Clash / Mihomo /
# Surge...) install a default route through a virtual adapter, so that trick reports
# the tunnel address -- typically in 198.18.0.0/15, the RFC 2544 benchmarking range
# Clash-family cores use for their TUN gateway and fake-ip pool. That happens in rule
# mode just as much as in global mode: the routing table always points at the TUN and
# only the rule engine downstream decides proxy-vs-direct. So we enumerate every
# interface and score them instead, which keeps ShareCV on the real LAN address.

# Ranges that are never a usable LAN address for us.
_BAD_NETS = (ipaddress.ip_network("198.18.0.0/15"),)   # RFC 2544 / Clash-family TUN
# Ranges that work but are almost certainly not the LAN we want.
_MEH_NETS = (ipaddress.ip_network("100.64.0.0/10"),)   # CGNAT / Tailscale

_VIRTUAL_IF_HINTS = ("tun", "tap", "utun", "wg", "tailscale", "zerotier", "docker",
                     "veth", "wsl", "hyper-v", "vmware", "virtualbox", "vmnet",
                     "loopback", "pseudo", "xigua", "clash", "warp", "ppp", "bridge")
_PHYSICAL_IF_PREFIXES = ("en", "eth", "wlan", "wl", "wi-fi", "wifi",
                         "以太网", "无线", "本地连接")

_if_cache: tuple = (0.0, [])   # (monotonic_stamp, [(name, ip, netmask)])


def _raw_interface_addrs() -> list:
    """[(ifname, ip, netmask)] for every up IPv4 interface. Best effort per platform."""
    out = []
    if psutil is not None:
        try:
            stats = psutil.net_if_stats()
            for name, addrs in psutil.net_if_addrs().items():
                st = stats.get(name)
                if st is not None and not st.isup:
                    continue
                for a in addrs:
                    if a.family == socket.AF_INET and a.address:
                        out.append((name, a.address, a.netmask or "255.255.255.0"))
            if out:
                return out
        except Exception:
            pass
    try:                                    # works on Windows, spotty elsewhere
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            out.append(("", ip, "255.255.255.0"))
    except Exception:
        pass
    if not out and sys.platform == "darwin":
        for dev in ("en0", "en1", "en2"):
            try:
                ip = subprocess.run(["ipconfig", "getifaddr", dev], capture_output=True,
                                    text=True, timeout=2).stdout.strip()
            except Exception:
                continue
            if ip:
                out.append((dev, ip, "255.255.255.0"))
    if not out:                             # last resort: may well be the TUN address
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            out.append(("", s.getsockname()[0], "255.255.255.0"))
        except Exception:
            pass
        finally:
            s.close()
    return out


def _score_addr(name: str, ip: str, netmask: str) -> int:
    """Rank a local address as "the LAN address a peer should dial". <0 = unusable."""
    try:
        addr = ipaddress.ip_address(ip)
        prefixlen = ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
    except ValueError:
        return -1
    if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        return -1
    if any(addr in net for net in _BAD_NETS):
        return -1
    low = name.lower()
    score = 100
    if any(h in low for h in _VIRTUAL_IF_HINTS):
        score -= 60
    if low.startswith(_PHYSICAL_IF_PREFIXES):
        score += 20
    if prefixlen >= 30:            # /30 or /31 is a point-to-point tunnel, not a LAN
        score -= 50
    if any(addr in net for net in _MEH_NETS):
        score -= 50
    elif addr in ipaddress.ip_network("192.168.0.0/16"):
        score += 10
    elif addr in ipaddress.ip_network("10.0.0.0/8"):
        score += 8
    elif addr in ipaddress.ip_network("172.16.0.0/12"):
        score += 4                 # frequently Docker/WSL, so a weaker bonus
    elif not addr.is_private:
        score -= 30                # a public address on the box isn't the LAN path
    return score


def lan_interfaces(max_age: float = 5.0) -> list:
    """Usable local IPv4 interfaces as [(score, name, ip, netmask)], best first."""
    global _if_cache
    now = time.monotonic()
    if now - _if_cache[0] > max_age:
        scored = [(s, n, ip, m) for n, ip, m in _raw_interface_addrs()
                  if (s := _score_addr(n, ip, m)) >= 0]
        scored.sort(key=lambda t: -t[0])
        _if_cache = (now, scored)
    return _if_cache[1]


def lan_ip() -> str:
    """LAN address of this machine, ignoring proxy/VPN tunnel adapters."""
    if LAN_IP_PIN:
        return LAN_IP_PIN
    ifaces = lan_interfaces()
    return ifaces[0][2] if ifaces else "127.0.0.1"


def describe_interfaces() -> str:
    ifaces = lan_interfaces()
    if not ifaces:
        return "    (none detected)"
    return "\n".join(f"    {ip}/{ipaddress.IPv4Network(f'0.0.0.0/{m}').prefixlen}"
                     f"  {name or '?'}  (score {s})" for s, name, ip, m in ifaces)


def _broadcast_targets() -> list:
    """Subnet-directed broadcast addresses, one per usable interface.

    Subnet-directed (10.20.1.255) rather than 255.255.255.255 on purpose: the former
    matches the on-link route of the real NIC, so it leaves via that NIC even when a
    TUN adapter owns the default route -- which is exactly where 255.255.255.255 goes
    and gets swallowed by the proxy.
    """
    targets = []
    for _s, _n, ip, mask in lan_interfaces():
        try:
            bcast = str(ipaddress.IPv4Interface(f"{ip}/{mask}").network.broadcast_address)
        except ValueError:
            continue
        if bcast not in targets:
            targets.append(bcast)
    return targets


def _discovery_socket() -> socket.socket:
    """UDP socket bound to the discovery port, broadcast+multicast enabled."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if sys.platform != "win32":
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.bind(("", DISCOVERY_PORT))
    # Join on every real interface, not just the default-route one (the TUN).
    joined = False
    for _s, _n, ip, _m in lan_interfaces():
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton(ip))
            joined = True
        except OSError:
            pass
    if not joined:
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton("0.0.0.0"))
        except OSError:
            pass
    return sock


def _send_everywhere(sock: socket.socket, msg: bytes):
    """Fire msg at every reachable path: per-interface broadcast + multicast."""
    for bcast in _broadcast_targets():
        try:
            sock.sendto(msg, (bcast, DISCOVERY_PORT))
        except Exception:
            pass
    for _s, _n, ip, _m in lan_interfaces():
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                            socket.inet_aton(ip))
            sock.sendto(msg, (MULTICAST_GROUP, DISCOVERY_PORT))
        except Exception:
            pass
    try:                                    # also try the default route, harmless
        sock.sendto(msg, ("<broadcast>", DISCOVERY_PORT))
    except Exception:
        pass


def discover_server(attempts=5, timeout=1.0) -> Optional[str]:
    """Actively probe for a server and listen for its reply/announcement."""
    try:
        sock = _discovery_socket()
    except Exception:
        return None
    try:
        for _ in range(attempts):
            _send_everywhere(sock, PROBE_MESSAGE)
            deadline = time.monotonic() + timeout
            while (remaining := deadline - time.monotonic()) > 0:
                sock.settimeout(remaining)
                try:
                    data, addr = sock.recvfrom(1024)
                except socket.timeout:
                    break
                if data == DISCOVERY_MESSAGE:
                    port = data.decode().split(":")[1]
                    url = f"http://{addr[0]}:{port}"
                    print(f"[+] Found server at {url}")
                    return url
                # ignore our own probe echoing back off the broadcast
    except Exception:
        pass
    finally:
        sock.close()
    return None


def udp_broadcaster(stop: threading.Event):
    """Announce server presence and answer client probes with a unicast reply."""
    try:
        sock = _discovery_socket()
    except Exception as e:
        print(f"[!] Discovery responder failed to start: {e}")
        return
    print(f"[*] Discovery broadcaster started on port {DISCOVERY_PORT}")
    while not stop.is_set():
        _send_everywhere(sock, DISCOVERY_MESSAGE)
        deadline = time.monotonic() + 1.0
        while not stop.is_set() and (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                break
            except Exception:
                break
            if data == PROBE_MESSAGE:
                try:
                    sock.sendto(DISCOVERY_MESSAGE, addr)
                except Exception:
                    pass
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
    """Copy locally-originated files into the content-addressed store."""
    for f in item.files:
        dest = store_path(f["hash"])
        if not os.path.exists(dest) and os.path.exists(f["path"]):
            tmp = dest + ".part"
            shutil.copy2(f["path"], tmp)
            os.replace(tmp, dest)


def materialize(item: ClipItem) -> Optional[ClipItem]:
    """Produce real, well-named file paths from stored content so the item can
    be placed on the clipboard. Returns None if any file isn't in the store."""
    files = []
    for f in item.files:
        src = store_path(f["hash"])
        if not os.path.exists(src):
            return None
        dest = os.path.join(DOWNLOAD_DIR, os.path.basename(f["name"]) or f["hash"])
        if os.path.exists(dest) and hash_file(dest) != f["hash"]:
            # Same basename, different content (e.g. two files named a.txt in
            # one batch) -- de-conflict with a hash prefix.
            dest = os.path.join(DOWNLOAD_DIR, f"{f['hash'][:8]}_{os.path.basename(f['name'])}")
        if not (os.path.exists(dest) and hash_file(dest) == f["hash"]):
            shutil.copy2(src, dest)
        files.append({"path": dest, "name": f["name"], "hash": f["hash"]})
    return ClipItem(type="file", files=files)


def cleanup_old_files(days: float = CLEANUP_DAYS) -> None:
    """Purge store/download files untouched for `days` (run once at startup)."""
    cutoff = time.time() - days * 86400
    for d in (STORE_DIR, DOWNLOAD_DIR):
        for name in os.listdir(d):
            path = os.path.join(d, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass


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
        conns = [ws for ws in list(self.conns) if ws is not exclude]
        results = await asyncio.gather(*(ws.send_text(msg) for ws in conns),
                                       return_exceptions=True)
        for ws, res in zip(conns, results):
            if isinstance(res, BaseException):
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
        print(f"[hub] v{version} <- {origin}: {item.type} {item.label()}")
        await self._broadcast({
            "kind": "update", "version": version, "origin": origin,
            "item": item.to_wire(),
        }, exclude=exclude)
        if apply_local and self.local is not None:
            await self._apply_local(item)

    async def _apply_local(self, item: ClipItem):
        if item.type == "file":
            item = await asyncio.to_thread(materialize, item)
            if item is None:
                return
        await asyncio.to_thread(self.local.apply, item)


hub = Hub()


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub.loop = asyncio.get_running_loop()
    hub.local = LocalClipboard(get_backend())
    hub.local.prime()
    cleanup_old_files()

    def on_change(item: ClipItem):
        if item.type == "file":
            ingest_local_file(item)
        # Hand the change to the event loop; don't apply_local (it's already here).
        asyncio.run_coroutine_threadsafe(
            hub.publish(item, origin=NODE_ID, apply_local=False), hub.loop)

    monitor = ClipboardMonitor(hub.local, on_change)
    monitor.start()
    print(f"[+] Server ready: http://{lan_ip()}:{SERVER_PORT}")
    yield
    monitor.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return {"status": "ok", "version": hub.version, "node": NODE_ID}


@app.post("/upload/{file_hash}")
async def upload(file_hash: str, request: Request):
    if not token_ok(request.headers.get("x-sharecv-token", "")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not is_hex_hash(file_hash):
        return JSONResponse({"error": "bad hash"}, status_code=400)
    dest = store_path(file_hash)
    if os.path.exists(dest):
        return {"status": "ok", "hash": file_hash}
    tmp = dest + f".{uuid.uuid4().hex}.part"
    hasher = hashlib.sha256()
    size = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    return JSONResponse({"error": "too large"}, status_code=413)
                hasher.update(chunk)
                f.write(chunk)
        # Content must actually match the hash it claims to be -- otherwise anyone
        # could poison the content-addressed store.
        if hasher.hexdigest() != file_hash:
            return JSONResponse({"error": "hash mismatch"}, status_code=400)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"status": "ok", "hash": file_hash}


@app.get("/file/{file_hash}")
async def download(file_hash: str, request: Request, name: str = "file"):
    if not token_ok(request.headers.get("x-sharecv-token", "")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
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
        if not token_ok(hello.get("token", "")):
            print(f"[hub] rejected client {node_id}: bad token")
            await ws.close(code=4401)
            return
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


# ================= CLIENT =================
async def fetch_file(client: httpx.AsyncClient, server_url: str, f: dict) -> bool:
    """Ensure one file entry's content is in the local store, downloading if needed."""
    dest = store_path(f["hash"])
    if os.path.exists(dest):
        return True
    url = f"{server_url}/file/{f['hash']}"
    tmp = dest + f".{uuid.uuid4().hex}.part"
    try:
        async with client.stream("GET", url, params={"name": f["name"]},
                                 headers=auth_headers()) as resp:
            if resp.status_code != 200:
                print(f"[!] Download failed: HTTP {resp.status_code}")
                return False
            with open(tmp, "wb") as fh:
                async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                    fh.write(chunk)
        os.replace(tmp, dest)
        return True
    except Exception as e:
        print(f"[!] Download error: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)
        return False


async def push_files(client: httpx.AsyncClient, server_url: str, item: ClipItem):
    """Upload locally-copied files to the hub (streamed, one request per file)."""
    ingest_local_file(item)
    for f in item.files:
        src = store_path(f["hash"])

        async def gen(path=src):
            # httpx AsyncClient needs an async body; offload blocking reads to a thread.
            with open(path, "rb") as fh:
                while True:
                    chunk = await asyncio.to_thread(fh.read, CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk

        await client.post(f"{server_url}/upload/{f['hash']}", content=gen(),
                          headers=auth_headers(), timeout=None)


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
            if not item.files or not all(f["hash"] for f in item.files):
                continue
            fetched = await asyncio.gather(
                *(fetch_file(client, server_url, f) for f in item.files))
            if not all(fetched):
                continue
            item = await asyncio.to_thread(materialize, item)
            if item is None:
                continue
            print(f"[v{version}] received files: {item.label()}")
        else:
            print(f"[v{version}] received text: {item.label()}")
        await asyncio.to_thread(local.apply, item)


async def send_loop(ws, client, server_url, outq: asyncio.Queue):
    while True:
        item: ClipItem = await outq.get()
        try:
            if item.type == "file":
                print(f"[^] sending files: {item.label()}")
                await push_files(client, server_url, item)
            else:
                print(f"[^] sending text: {item.label()}")
            await ws.send(json.dumps({
                "kind": "update", "origin": NODE_ID, "item": item.to_wire(),
            }))
        except Exception as e:
            print(f"[!] Send failed: {type(e).__name__}: {e}")


async def run_client(server_url: str):
    backend = get_backend()
    local = LocalClipboard(backend)
    local.prime()
    cleanup_old_files()
    ws_url = server_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    loop = asyncio.get_running_loop()

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            monitor = None
            try:
                async with ws_connect(ws_url, max_size=None) as ws:
                    await ws.send(json.dumps(
                        {"kind": "hello", "node_id": NODE_ID, "token": TOKEN}))
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
                    tasks = [recv, send]
                    try:
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        # Surface why a loop ended (e.g. WinError 121 socket timeout) so the
                        # outer handler can reconnect, instead of asyncio dumping a raw traceback.
                        for t in done:
                            if t.exception() is not None:
                                raise t.exception()
                    finally:
                        for t in tasks:
                            t.cancel()
                        # Drain both tasks so no "Task exception was never retrieved" noise.
                        await asyncio.gather(*tasks, return_exceptions=True)
            except (ConnectionClosed, OSError, ConnectionError) as e:
                if isinstance(e, ConnectionClosed) and e.rcvd is not None and e.rcvd.code == 4401:
                    print("[!] Server rejected our token. Set the right one via --token / SHARECV_TOKEN.")
                    return
                print(f"[!] Connection lost ({type(e).__name__}: {e}); reconnecting in {RECONNECT_DELAY}s...")
            except Exception as e:
                print(f"[!] Client error: {type(e).__name__}: {e}; reconnecting in {RECONNECT_DELAY}s...")
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
    parser.add_argument("--token", default=os.environ.get("SHARECV_TOKEN", ""),
                        help="Shared secret; must match on all nodes (env: SHARECV_TOKEN)")
    parser.add_argument("--mode", choices=("auto", "server", "client"), default="auto",
                        help="auto: discover, else become server; server/client: force the role")
    parser.add_argument("--ip", default=os.environ.get("SHARECV_IP", ""),
                        help="Pin the LAN address to advertise, bypassing auto-detection "
                             "(env: SHARECV_IP). Useful if a VPN/proxy TUN adapter confuses it")
    args = parser.parse_args()

    global TOKEN, LAN_IP_PIN
    TOKEN = args.token
    LAN_IP_PIN = args.ip
    if not TOKEN:
        print("[!] WARNING: no --token set -- anyone on this network can read/write "
              "your clipboard. Set the same --token on every node.")

    server_url = None
    if args.server:
        if args.mode == "server":
            parser.error("--mode server cannot be combined with a server address")
        server_url = args.server if args.server.startswith("http") else f"http://{args.server}:{SERVER_PORT}"
        print(f"[+] Manual server URL: {server_url}")
    elif args.mode != "server":
        print("[*] Looking for an existing ShareCV server on the local network...")
        server_url = discover_server()
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
        if not server_url and args.mode == "client":
            print("[!] No server found and --mode client forbids becoming one. "
                  "Start the server first, or pass its IP: python sharecv.py <server-ip>")
            sys.exit(1)

    if server_url:
        print(f"[+] CLIENT mode -> {server_url}")
        try:
            asyncio.run(run_client(server_url))
        except KeyboardInterrupt:
            print("\n[*] Stopped clipboard sync.")
    else:
        print("[+] Starting in SERVER mode (hub)." if args.mode == "server"
              else "[+] No server found. Starting in SERVER mode (hub).")
        if LAN_IP_PIN:
            print(f"[i] LAN address pinned to {LAN_IP_PIN}")
        else:
            print(f"[i] Candidate LAN addresses (tunnel adapters excluded):\n"
                  f"{describe_interfaces()}\n"
                  f"[i] Picked {lan_ip()} -- override with --ip if that's wrong.")
        print(f"[i] If the other machine ALSO ends up in SERVER mode, discovery is being "
              f"blocked (firewall / AP isolation). Connect it manually:\n"
              f"    python sharecv.py {lan_ip()}" + (" --token <your-token>" if TOKEN else ""))
        stop = threading.Event()
        threading.Thread(target=udp_broadcaster, args=(stop,), daemon=True).start()
        try:
            uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT, workers=1,
                        reload=False, log_level="warning")
        finally:
            stop.set()


if __name__ == "__main__":
    main()
