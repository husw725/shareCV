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

**On the Server Machine (the main hub):**
```bash
pip install -r requirements-s.txt
```

**On the Client Machine:**
```bash
pip install -r requirements-c.txt
```

---

## Usage

### Step 1: Start the Server

On the **Server Machine**, run:
```bash
python server.py
```
*   The server will start listening on port `6097` and begin broadcasting its presence on the local network.
*   It also acts as a client for this machine, monitoring the local clipboard.

### Step 2: Start the Client

On the **Client Machine**, run:
```bash
python client.py
```
*   The client will automatically search for and connect to the server.
*   **Manual Fallback:** If auto-discovery fails (due to network/firewall settings), it will fall back to the IP address defined in `client.py`.

### Step 3: Share!

*   **Copy Text:** Copy any text on one computer. Within seconds, you can paste it on the other.
*   **Copy Files:**
    *   **macOS:** Select a file in Finder and press `Cmd+C`.
    *   **Windows:** Select a file in Explorer and press `Ctrl+C`.
    *   **Paste:** Press `Cmd+V` (macOS) or `Ctrl+V` (Windows) on the destination computer to paste the file.

---

## Technical Details

-   **Auto-Discovery:** Uses UDP broadcasting on port `6098` to allow the client to find the server's IP automatically.
-   **Server (`server.py`):** Uses `FastAPI` to handle HTTP requests. It manages the central clipboard state and stores transferred files in a `downloads/` directory.
-   **Client (`client.py`):** Polls the server for changes and pushes local clipboard updates.
-   **File Handling:**
    -   **macOS:** Uses `osascript` (AppleScript) to read/write file paths to the system clipboard.
    -   **Windows:** Uses PowerShell (`Get-Clipboard`, `Set-Clipboard`) to interact with the file clipboard.
