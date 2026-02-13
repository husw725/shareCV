import asyncio
import httpx
import pyperclip
import sys
import subprocess
import os
import shutil

SERVER = "http://10.0.6.136:6097"  # change to your server IP
POLL_INTERVAL = 2.0  # seconds between checks
DOWNLOAD_DIR = "client_downloads"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def get_local_clipboard():
    """Get current clipboard content (text or file path)"""
    # Check for file on macOS
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
    
    # Check for file on Windows
    elif sys.platform == "win32":
        try:
            # PowerShell command to get file paths from clipboard
            cmd = ["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard -Format FileDropList | ForEach-Object { $_.FullName }"]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                # Take the first file if multiple are copied
                path = result.stdout.strip().splitlines()[0]
                if os.path.exists(path):
                    return {"type": "file", "content": path}
        except Exception:
            pass

    # Fallback to text
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
                script = f'set the clipboard to POSIX file "{abs_path}"'
                subprocess.run(["osascript", "-e", script])
            except Exception as e:
                print(f"[⚠️] Failed to set macOS file clipboard: {e}")
        
        elif sys.platform == "win32":
            try:
                # PowerShell command to set file to clipboard
                cmd = ["powershell.exe", "-NoProfile", "-Command", f'Set-Clipboard -Path "{abs_path}"']
                subprocess.run(cmd)
            except Exception as e:
                print(f"[⚠️] Failed to set Windows file clipboard: {e}")

async def sync_clipboard():
    last_local = {"type": "text", "content": ""}
    last_remote = {"type": "text", "content": ""}
    last_action = None  # "sent" or "received"

    async with httpx.AsyncClient(timeout=30.0) as client:
        print(f"🔗 Connected to {SERVER} — syncing every {POLL_INTERVAL}s (Ctrl+C to stop)\n")

        while True:
            try:
                # 1️⃣ Get current local clipboard
                current_local = get_local_clipboard()

                # 2️⃣ Get remote clipboard state
                try:
                    resp = await client.get(f"{SERVER}/get")
                    remote_state = resp.json()
                except Exception as e:
                    print(f"[⚠️] Server error: {e}")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                # Normalizing remote state (server might return just content for text if old version, but we updated server)
                # But just in case, handle dict
                if not isinstance(remote_state, dict):
                    remote_state = {"type": "text", "content": str(remote_state)}

                # 3️⃣ Remote → Local update
                # Check if remote is different from what we last saw AND different from what we have locally
                # (Simple equality check works for dicts)
                if remote_state != last_remote:
                    # If remote changed, we should probably update local, UNLESS we just sent it.
                    # But if we just sent it, remote_state should match last_local (mostly).
                    
                    # Logic: If remote is new, pull it.
                    is_new_remote = (remote_state["content"] != last_local["content"]) or (remote_state["type"] != last_local["type"])
                    
                    if is_new_remote:
                        print(f"[⬇️] Remote changed to {remote_state['type']}: {remote_state['content'][:30]}...")
                        
                        if remote_state["type"] == "file":
                            # Download file
                            filename = remote_state["content"]
                            local_path = os.path.join(DOWNLOAD_DIR, filename)
                            
                            # Check if we already have it to avoid re-downloading? 
                            # Maybe not, just overwrite to be safe or skip if exists?
                            # Let's download.
                            print(f"[⬇️] Downloading file: {filename}...")
                            async with client.stream("GET", f"{SERVER}/download/{filename}") as resp:
                                if resp.status_code == 200:
                                    with open(local_path, "wb") as f:
                                        async for chunk in resp.aiter_bytes():
                                            f.write(chunk)
                                    set_local_clipboard({"type": "file", "content": local_path})
                                    print(f"[✅] File downloaded and copied: {local_path}")
                                else:
                                    print(f"[❌] Failed to download: {resp.status_code}")
                        else:
                            # Text
                            set_local_clipboard(remote_state)
                            print(f"[⬇️] Updated local text.")

                        last_local = get_local_clipboard() # Update our view of local
                        last_remote = remote_state
                        last_action = "received"
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                # 4️⃣ Local → Remote update
                # Check if local changed since last time we checked
                # And ensure we didn't just receive this change
                if current_local != last_local and last_action != "received":
                    print(f"[⬆️] Local changed to {current_local['type']}: {current_local['content'][:30]}...")
                    
                    if current_local["type"] == "file":
                        # Upload file
                        file_path = current_local["content"]
                        filename = os.path.basename(file_path)
                        
                        if os.path.exists(file_path):
                            print(f"[⬆️] Uploading file: {filename}...")
                            with open(file_path, "rb") as f:
                                files = {"file": (filename, f)}
                                await client.post(f"{SERVER}/upload", files=files)
                            # Update server state is handled by upload? 
                            # Our server code updates state on upload.
                            # So we don't need to call /set for files if /upload does it.
                            # Let's check server.py: Yes, /upload updates state.
                        else:
                            print(f"[⚠️] File not found: {file_path}")
                    else:
                        # Send text
                        await client.post(f"{SERVER}/set", json=current_local)
                    
                    last_local = current_local
                    last_remote = current_local # Assume server matches now
                    last_action = "sent"

                # Reset last_action
                last_action = None

            except Exception as e:
                print(f"[⚠️] Error in loop: {type(e).__name__}: {e}")

            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    try:
        asyncio.run(sync_clipboard())
    except KeyboardInterrupt:
        print("\n🛑 Stopped clipboard sync.")