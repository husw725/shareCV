# ShareCV: Cross-Platform Clipboard Sharing

ShareCV is a lightweight tool that synchronizes your clipboard (both text and files) across multiple computers on the same network. It supports seamless copying on one machine and pasting on another, whether you are using Windows or macOS.

## Features

-   **Automatic Discovery:** No need to type IP addresses. Clients automatically find the server on your local network.
-   **Instant Push Sync:** Changes are delivered over a WebSocket the moment they happen — low latency, and zero network traffic while idle.
-   **Multi-Machine:** One machine acts as the hub; any number of others connect to it and stay in sync.
-   **Text Sharing:** Copy text on one computer, paste it on another.
-   **File & Image Sharing:** Copy one or **multiple** files in Finder (macOS) or File Explorer (Windows), or copy a screenshot, and paste them on another machine. Files are streamed and content-addressed (de-duplicated by hash).
-   **Token Auth:** An optional shared secret keeps other people on the network out of your clipboard.
-   **Cross-Platform:** Works bi-directionally between macOS and Windows.
-   **Local Server:** Runs entirely on your local network for privacy and speed.

## Prerequisites

-   **Python 3.7+** installed on both machines.
-   **Network:** Both computers must be on the same local network (e.g., connected to the same Wi-Fi).

## Installation

### 1. Clone the Repository

On both machines:
```bash
git clone https://github.com/husw725/shareCV.git
cd shareCV
```

### 2. Install Dependencies

**On both machines:**
```bash
pip install -r requirements.txt
```

---

## Usage

### Step 1: Start on the First Machine (becomes the Server)

On your primary machine, run:
```bash
python sharecv.py --token <your-secret>
```
*   Since no other instance is running, it will automatically start in **Server mode** (listening on port `6097`).
*   `--token` (or the `SHARECV_TOKEN` env var) is strongly recommended: without it, **anyone on the same network can read and write your clipboard**. Use the same token on every machine.
*   It acts as the central hub while also monitoring this machine's local clipboard.

### Step 2: Start on the Second Machine (becomes the Client)

On your other machine, run:
```bash
python sharecv.py --token <your-secret>
```
*   It will automatically discover the server running on the first machine and start in **Client mode**.
*   **Manual Fallback:** If your computers are on different subnets (e.g., VMs, VPNs) and auto-discovery fails, you can connect directly by providing the server's IP address:
    ```bash
    python sharecv.py 10.0.6.136
    ```
*   **Forcing a role:** By default (`--mode auto`) each instance discovers an existing server, and becomes the server itself if none is found — so if discovery is blocked (firewall / Wi-Fi AP isolation) or both machines start at the same moment, both can end up as servers. To pin the roles, run `--mode server` on one machine and `--mode client` on the other (`client` never falls back to becoming a server; it exits if no server is found).

### Step 3: Share!

*   **Copy Text:** Copy any text on one computer. Within seconds, you can paste it on the other.
*   **Copy Files:**
    *   **macOS:** Select one or more files in Finder and press `Cmd+C`.
    *   **Windows:** Select one or more files in Explorer and press `Ctrl+C`.
    *   **Paste:** Press `Cmd+V` (macOS) or `Ctrl+V` (Windows) on the destination computer to paste the file(s). Folders are not synced (only files).

---

## Technical Details

-   **Auto-Discovery:** Two-way UDP on port `6098`: the server announces itself via broadcast + multicast every second, and clients actively probe (5 retries) with the server answering each probe by unicast. A last-known-good server is cached as a fallback.
-   **Server Mode (hub):** A `FastAPI` app on port `6097` holds the authoritative clipboard state (versioned, content-addressed by SHA-256) and pushes updates to all connected clients over a WebSocket (`/ws`). Files are stored in a content-addressed store under the downloads directory and served streamed via `/file/{hash}`. The hub also participates in sync like any client.
-   **Client Mode:** Connects to the hub's WebSocket, applies incoming changes, and pushes local clipboard changes instantly. Reconnects automatically if the connection drops.
-   **Echo prevention:** Each node tracks the signature of what it last saw/applied, so synced content is never bounced back around the network.
-   **Security:** With a token set, the WebSocket handshake and all file transfers are authenticated (`X-ShareCV-Token`). Uploads are verified against their declared SHA-256 (no store poisoning) and capped at 4 GiB. Store/download files older than 7 days are purged at startup.
-   **Clipboard Handling (`ClipboardBackend`):**
    -   **macOS:** Pure AppKit (`NSPasteboard`) — reads/writes file URLs and images directly, with `changeCount()` used to skip redundant work.
    -   **Windows:** PowerShell (`Get-Clipboard` / `Set-Clipboard`, with file paths passed via environment variables — no string interpolation).
    -   **Other:** Text-only fallback via `pyperclip`.

> **macOS dependency:** file/image clipboard support requires `pyobjc-framework-Cocoa` (installed automatically by `requirements.txt` on macOS). Without it, ShareCV degrades gracefully to text-only.
