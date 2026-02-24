# ShareCV: Cross-Platform Clipboard Sharing

ShareCV is a lightweight tool that synchronizes your clipboard (both text and files) across two computers on the same network. It supports seamless copying on one machine and pasting on another, whether you are using Windows or macOS.

## Features

-   **Automatic Discovery:** No need to type IP addresses. The client automatically finds the server on your local network.
-   **Text Sharing:** Copy text on one computer, paste it on another.
-   **File Sharing:** Copy files in Finder (macOS) or File Explorer (Windows), and paste them on the other machine.
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
python sharecv.py
```
*   Since no other instance is running, it will automatically start in **Server mode** (listening on port `6097`).
*   It acts as the central hub while also monitoring this machine's local clipboard.

### Step 2: Start on the Second Machine (becomes the Client)

On your other machine, run:
```bash
python sharecv.py
```
*   It will automatically discover the server running on the first machine and start in **Client mode**.
*   **Manual Fallback:** If your computers are on different subnets (e.g., VMs, VPNs) and auto-discovery fails, you can connect directly by providing the server's IP address:
    ```bash
    python sharecv.py 10.0.6.136
    ```

### Step 3: Share!

*   **Copy Text:** Copy any text on one computer. Within seconds, you can paste it on the other.
*   **Copy Files:**
    *   **macOS:** Select a file in Finder and press `Cmd+C`.
    *   **Windows:** Select a file in Explorer and press `Ctrl+C`.
    *   **Paste:** Press `Cmd+V` (macOS) or `Ctrl+V` (Windows) on the destination computer to paste the file.

---

## Technical Details

-   **Auto-Discovery:** Uses UDP broadcasting on port `6098` to allow the client to find the server's IP automatically.
-   **Server Mode (`sharecv.py`):** Uses `FastAPI` to handle HTTP requests. It manages the central clipboard state and stores transferred files in a `sharecv_downloads/` directory.
-   **Client Mode (`sharecv.py`):** Polls the server for changes and pushes local clipboard updates.
-   **File Handling:**
    -   **macOS:** Uses `osascript` (AppleScript) to read/write file paths to the system clipboard.
    -   **Windows:** Uses PowerShell (`Get-Clipboard`, `Set-Clipboard`) to interact with the file clipboard.
