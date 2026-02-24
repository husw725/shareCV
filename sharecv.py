import asyncio
import httpx
import pyperclip
import sys
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

def discover_server(timeout=3.0):
    """Listen for UDP broadcast from the server"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if sys.platform != "win32":
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
    
    try:
        sock.bind(('', DISCOVERY_PORT))
        sock.settimeout(timeout)
        data, addr = sock.recvfrom(1024)
        if data == DISCOVERY_MESSAGE:
            server_ip = addr[0]
            server_port = data.decode().split(":")[1]
            server_url = f"http://{server_ip}:{server_port}"
            return server_url
    except Exception:
        pass
    finally:
        sock.close()
    return None

def udp_broadcaster():
    """Broadcast server presence for auto-discovery"""
    broadcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    broadcast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    
    print(f"📡 Discovery broadcaster started on port {DISCOVERY_PORT}...")
    while True:
        try:
            broadcast_sock.sendto(DISCOVERY_MESSAGE, ('<broadcast>', DISCOVERY_PORT))
        except Exception as e:
            pass
        time.sleep(5)

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
            script_check = 'if ((clipboard info) as string) contains "PNGf" then return "yes"'
            res_check = subprocess.run(["osascript", "-e", script_check], capture_output=True, text=True)
            if "yes" in res_check.stdout:
                img_path = "/tmp/sharecv_mac_temp.png"
                extract_cmd = f"osascript -e 'the clipboard as \\\"PNGf\\\"' | sed -e 's/«data PNGf//' -e 's/»//' | xxd -r -p > {img_path}"
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

if __name__ == "__main__":
    print("🔍 Looking for existing ShareCV server on local network...")
    found_server_url = discover_server(timeout=3.0)
    
    if found_server_url:
        print(f"✅ Found server! Running in CLIENT mode connected to {found_server_url}")
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