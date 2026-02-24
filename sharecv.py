import asyncio
import httpx
import pyperclip
import sys
import argparse
import subprocess
import os
import shutil
import socket
import threading
import time
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse
import uvicorn

# Discovery settings
DISCOVERY_PORT = 6098
DISCOVERY_MESSAGE = b"ShareCV-Server:6097"
SERVER_PORT = 6097
POLL_INTERVAL = 2.0
DOWNLOAD_DIR = "sharecv_downloads"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ================= SERVER STATE =================
app = FastAPI()
clipboard_state = {"type": "text", "content": ""}

# ================= HELPER FUNCTIONS =================

def discover_server(timeout=2.0):
    """Listen for UDP broadcast from the server"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if sys.platform != "win32":
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
    
    # Multicast setup
    MULTICAST_GROUP = '239.255.255.250'

    try:
        sock.bind(('', DISCOVERY_PORT))

        # Join multicast group
        try:
            group = socket.inet_aton(MULTICAST_GROUP)
            mreq = group + socket.inet_aton('0.0.0.0')
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except Exception:
            pass

        sock.settimeout(timeout)
        data, addr = sock.recvfrom(1024)
        if data == DISCOVERY_MESSAGE:
            server_ip = addr[0]
            server_port = data.decode().split(":")[1]
            server_url = f"http://{server_ip}:{server_port}"
            print(f"✅ Found server at {server_url}")
            return server_url
    except Exception:
        pass
    finally:
        sock.close()
    return None

def udp_broadcaster():
    """Broadcast server presence for auto-discovery across all interfaces"""
    broadcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    broadcast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # Multicast setup
    MULTICAST_GROUP = '239.255.255.250'
    broadcast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    
    print(f"📡 Discovery broadcaster started on port {DISCOVERY_PORT}...")
    while True:
        try:
            # 1. Standard Global Broadcast
            broadcast_sock.sendto(DISCOVERY_MESSAGE, ('<broadcast>', DISCOVERY_PORT))
            
            # 2. SSDP/Multicast (often passes through VPNs better than broadcast)
            broadcast_sock.sendto(DISCOVERY_MESSAGE, (MULTICAST_GROUP, DISCOVERY_PORT))
            
            # 3. Subnet-specific broadcasts for all local IPs
            try:
                hostname = socket.gethostname()
                for ip in socket.gethostbyname_ex(hostname)[2]:
                    if ip.startswith("127."): continue
                    # Rough guess for subnet broadcast (e.g., 10.0.6.255)
                    subnet = ".".join(ip.split(".")[:-1]) + ".255"
                    broadcast_sock.sendto(DISCOVERY_MESSAGE, (subnet, DISCOVERY_PORT))
            except Exception:
                pass

        except Exception as e:
            pass
        time.sleep(1)

def get_local_clipboard():
    """Get current clipboard content (text or file path)"""
    if sys.platform == "darwin":
        try:
            script = 'tell application "System Events" to return POSIX path of (the clipboard as alias)'
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
            if result.returncode == 0:
                path = result.stdout.strip()
                if os.path.exists(path):
                    return {"type": "file", "content": path}
        except Exception:
            pass
            
        try:
            script_check = 'if ((clipboard info) as string) contains "PNGf" then \n return "PNGf" \n else if ((clipboard info) as string) contains "TIFF picture" then \n return "TIFF" \n end if \n return "no"'
            res_check = subprocess.run(["osascript", "-e", script_check], capture_output=True, text=True)
            img_type = res_check.stdout.strip()
            if img_type in ["PNGf", "TIFF"]:
                img_path = "/tmp/sharecv_mac_temp.png"
                if img_type == "PNGf":
                    extract_cmd = f"osascript -e 'the clipboard as \"PNGf\"' | sed -e 's/«data PNGf//' -e 's/»//' | xxd -r -p > {img_path}"
                    subprocess.run(extract_cmd, shell=True)
                else:
                    tiff_path = "/tmp/sharecv_mac_temp.tiff"
                    extract_cmd = f"osascript -e 'the clipboard as \"TIFF\"' | sed -e 's/«data TIFF//' -e 's/»//' | xxd -r -p > {tiff_path} && sips -s format png {tiff_path} --out {img_path}"
                    subprocess.run(extract_cmd, shell=True)

                if os.path.exists(img_path) and os.path.getsize(img_path) > 0:
                    import hashlib
                    with open(img_path, "rb") as f:
                        file_hash = hashlib.md5(f.read()).hexdigest()
                    final_path = f"/tmp/sharecv_mac_screenshot_{file_hash}.png"
                    if not os.path.exists(final_path):
                        shutil.move(img_path, final_path)
                    else:
                        os.remove(img_path)
                    return {"type": "file", "content": final_path}
        except Exception:
            pass
            
    elif sys.platform == "win32":
        try:
            cmd = ["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard -Format FileDropList | ForEach-Object { $_.FullName }"]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                path = result.stdout.strip().splitlines()[0]
                if os.path.exists(path):
                    return {"type": "file", "content": path}
        except Exception:
            pass

    text = pyperclip.paste() or ""
    return {"type": "text", "content": text}

def set_local_clipboard(data):
    """Set clipboard content"""
    if data["type"] == "text":
        pyperclip.copy(data["content"])
    elif data["type"] == "file":
        abs_path = os.path.abspath(data["content"])
        if not os.path.exists(abs_path):
            print(f"[⚠️] File to copy not found: {abs_path}")
            return

        if sys.platform == "darwin":
            try:
                script = f'''use framework "AppKit"
use scripting additions
set theURL to current application's NSURL's fileURLWithPath:"{abs_path}"
set pb to current application's NSPasteboard's generalPasteboard()
pb's clearContents()
set theExt to theURL's pathExtension()'s lowercaseString() as string
if theExt is in {{"png", "jpg", "jpeg", "tiff", "gif", "bmp"}} then
    set theImage to current application's NSImage's alloc()'s initWithContentsOfURL:theURL
    if theImage is not missing value then
        pb's writeObjects:{{theImage, theURL}}
        return
    end if
end if
pb's writeObjects:{{theURL}}'''
                subprocess.run(["osascript", "-e", script])
            except Exception as e:
                print(f"[⚠️] Failed to set macOS file clipboard: {e}")
        
        elif sys.platform == "win32":
            try:
                ext = abs_path.lower().split('.')[-1]
                if ext in ['png', 'jpg', 'jpeg', 'bmp', 'gif', 'tiff']:
                    safe_path = abs_path.replace("'", "''")
                    ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$data = New-Object System.Windows.Forms.DataObject
$files = New-Object System.Collections.Specialized.StringCollection
$files.Add('{safe_path}')
$data.SetFileDropList($files)
$dropEffect = New-Object byte[] 4
$dropEffect[0] = 5
$data.SetData("Preferred DropEffect", [System.IO.MemoryStream]::new($dropEffect))
try {{
    $bmp = New-Object System.Drawing.Bitmap('{safe_path}')
    $data.SetImage($bmp)
    [System.Windows.Forms.Clipboard]::SetDataObject($data, $true)
    $bmp.Dispose()
}} catch {{
    [System.Windows.Forms.Clipboard]::SetDataObject($data, $true)
}}
"""
                    import base64
                    encoded = base64.b64encode(ps_script.encode('utf-16le')).decode('utf-8')
                    cmd = ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded]
                    subprocess.run(cmd)
                else:
                    cmd = ["powershell.exe", "-NoProfile", "-Command", f'Set-Clipboard -Path "{abs_path}"']
                    subprocess.run(cmd)
            except Exception as e:
                print(f"[⚠️] Failed to set Windows file clipboard: {e}")

# ================= SERVER ENDPOINTS =================

@app.get("/get")
async def get_clipboard():
    global clipboard_state
    current_local = get_local_clipboard()
    local_changed = False
    
    if current_local["type"] == "text":
        if clipboard_state["type"] != "text" or current_local["content"] != clipboard_state["content"]:
            clipboard_state = current_local
            local_changed = True
    elif current_local["type"] == "file":
        local_filename = os.path.basename(current_local["content"])
        if clipboard_state["type"] != "file" or clipboard_state["content"] != local_filename:
            src_path = current_local["content"]
            dest_path = os.path.join(DOWNLOAD_DIR, local_filename)
            if os.path.abspath(src_path) != os.path.abspath(dest_path):
                try:
                    shutil.copy2(src_path, dest_path)
                except Exception as e:
                    print(f"Error staging file: {e}")
            clipboard_state = {"type": "file", "content": local_filename}
            local_changed = True

    if local_changed:
        print(f"[Local Update] State changed to {clipboard_state['type']}: {clipboard_state['content']}")

    return JSONResponse(clipboard_state)

@app.post("/set")
async def set_clipboard(request: Request):
    global clipboard_state
    data = await request.json()
    new_type = data.get("type", "text")
    content = data.get("content", "")
    
    clipboard_state["type"] = new_type
    clipboard_state["content"] = content
    
    if new_type == "text":
        set_local_clipboard({"type": "text", "content": content})
        
    return JSONResponse({"status": "ok", "state": clipboard_state})

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    global clipboard_state
    file_path = os.path.join(DOWNLOAD_DIR, file.filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    clipboard_state["type"] = "file"
    clipboard_state["content"] = file.filename
    set_local_clipboard({"type": "file", "content": file_path})
    return JSONResponse({"status": "ok", "filename": file.filename})

@app.get("/download/{filename}")
async def download_file(filename: str):
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=filename)
    return JSONResponse({"error": "File not found"}, status_code=404)

# ================= CLIENT LOGIC =================

async def sync_clipboard(server_url):
    last_local = {"type": "text", "content": ""}
    last_remote = {"type": "text", "content": ""}
    last_action = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        print(f"🔗 Connected to {server_url} — syncing every {POLL_INTERVAL}s (Ctrl+C to stop)\\n")

        while True:
            try:
                current_local = get_local_clipboard()

                try:
                    resp = await client.get(f"{server_url}/get")
                    remote_state = resp.json()
                except Exception as e:
                    print(f"[⚠️] Server connection lost: {e}")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                if not isinstance(remote_state, dict):
                    remote_state = {"type": "text", "content": str(remote_state)}

                if remote_state != last_remote:
                    is_new_remote = (remote_state["content"] != last_local["content"]) or (remote_state["type"] != last_local["type"])
                    
                    if is_new_remote:
                        print(f"[⬇️] Remote changed to {remote_state['type']}: {remote_state['content'][:30]}...")
                        
                        if remote_state["type"] == "file":
                            filename = remote_state["content"]
                            local_path = os.path.join(DOWNLOAD_DIR, filename)
                            
                            print(f"[⬇️] Downloading file: {filename}...")
                            async with client.stream("GET", f"{server_url}/download/{filename}") as resp:
                                if resp.status_code == 200:
                                    with open(local_path, "wb") as f:
                                        async for chunk in resp.aiter_bytes():
                                            f.write(chunk)
                                    set_local_clipboard({"type": "file", "content": local_path})
                                    print(f"[✅] File downloaded and copied: {local_path}")
                                else:
                                    print(f"[❌] Failed to download: {resp.status_code}")
                        else:
                            set_local_clipboard(remote_state)
                            print(f"[⬇️] Updated local text.")

                        last_local = get_local_clipboard()
                        last_remote = remote_state
                        last_action = "received"
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                if current_local != last_local and last_action != "received":
                    print(f"[⬆️] Local changed to {current_local['type']}: {current_local['content'][:30]}...")
                    
                    if current_local["type"] == "file":
                        file_path = current_local["content"]
                        filename = os.path.basename(file_path)
                        
                        if os.path.exists(file_path):
                            print(f"[⬆️] Uploading file: {filename}...")
                            with open(file_path, "rb") as f:
                                files = {"file": (filename, f)}
                                await client.post(f"{server_url}/upload", files=files)
                        else:
                            print(f"[⚠️] File not found: {file_path}")
                    else:
                        await client.post(f"{server_url}/set", json=current_local)
                    
                    last_local = current_local
                    last_remote = current_local
                    last_action = "sent"

                last_action = None

            except Exception as e:
                print(f"[⚠️] Error in loop: {type(e).__name__}: {e}")

            await asyncio.sleep(POLL_INTERVAL)

CACHE_FILE = ".sharecv_cache"

def save_cache(url):
    try:
        with open(CACHE_FILE, "w") as f:
            f.write(url)
    except Exception:
        pass

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return f.read().strip()
        except Exception:
            pass
    return None

async def check_server(url):
    """Quick check if a server is alive"""
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            resp = await client.get(f"{url}/get")
            return resp.status_code == 200
    except Exception:
        return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ShareCV - Cross-Platform Clipboard Synchronization")
    parser.add_argument("server", nargs="?", default=None, help="Optional IP address or URL of the server to connect to directly (bypasses auto-discovery)")
    args = parser.parse_args()

    found_server_url = None

    if args.server:
        if not args.server.startswith("http://"):
            found_server_url = f"http://{args.server}:{SERVER_PORT}"
        else:
            found_server_url = args.server
        print(f"✅ Manual server URL provided. Connecting directly...")
    else:
        print("🔍 Looking for existing ShareCV server on local network...")
        found_server_url = discover_server(timeout=2.0)
        
        # Smart Memory Fallback
        if not found_server_url:
            cached_url = load_cache()
            if cached_url:
                print(f"⌛ Discovery failed, trying cached server: {cached_url}...")
                if asyncio.run(check_server(cached_url)):
                    print(f"✨ Cached server is alive!")
                    found_server_url = cached_url
    
    if found_server_url:
        print(f"✅ Running in CLIENT mode connected to {found_server_url}")
        save_cache(found_server_url)
        try:
            asyncio.run(sync_clipboard(found_server_url))
        except KeyboardInterrupt:
            print("\\n🛑 Stopped clipboard sync.")
    else:
        print("❌ No existing server found. Running in SERVER mode (Main Hub).")
        threading.Thread(target=udp_broadcaster, daemon=True).start()

        uvicorn.run(
            "sharecv:app",
            host="0.0.0.0",
            port=SERVER_PORT,
            workers=1,
            reload=False,
            log_level="warning"
        )