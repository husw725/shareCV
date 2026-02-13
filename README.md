# ShareCV: Cross-Platform Clipboard Sharing

ShareCV is a lightweight tool that synchronizes your clipboard (both text and files) across two computers on the same network. It supports seamless copying on one machine and pasting on another, whether you are using Windows or macOS.

## Features

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

## Configuration

1.  **Find the Server's IP Address:**
    *   **Windows:** Open Command Prompt and run `ipconfig`. Look for "IPv4 Address".
    *   **macOS:** Open Terminal and run `ifconfig | grep "inet " | grep -v 127.0.0.1`.

2.  **Update the Client:**
    *   Open `client.py` on the **Client Machine**.
    *   Change the `SERVER` variable to match your Server's IP address:
        ```python
        SERVER = "http://192.168.1.5:6097"  # Replace with your Server's IP
        ```

---

## Usage

### Step 1: Start the Server

On the **Server Machine**, run:
```bash
python server.py
```
*   The server will start listening on port `6097`.
*   It also acts as a client for this machine, monitoring the local clipboard.

### Step 2: Start the Client

On the **Client Machine**, run:
```bash
python client.py
```
*   The client will connect to the server and start syncing.

### Step 3: Share!

*   **Copy Text:** Copy any text on one computer. Within seconds, you can paste it on the other.
*   **Copy Files:**
    *   **macOS:** Select a file in Finder and press `Cmd+C`.
    *   **Windows:** Select a file in Explorer and press `Ctrl+C`.
    *   Wait a moment for the transfer (large files take longer).
    *   **Paste:** Press `Cmd+V` (macOS) or `Ctrl+V` (Windows) on the destination computer to paste the file.

---

## Technical Details

-   **Server (`server.py`):** Uses `FastAPI` to handle HTTP requests. It manages the central clipboard state and stores transferred files in a `downloads/` directory. It also monitors the server's local clipboard changes.
-   **Client (`client.py`):** Polls the server for changes and pushes local clipboard updates.
-   **File Handling:**
    -   **macOS:** Uses `osascript` (AppleScript) to read/write file paths to the system clipboard.
    -   **Windows:** Uses PowerShell (`Get-Clipboard`, `Set-Clipboard`) to interact with the file clipboard.
