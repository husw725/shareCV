from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse
import pyperclip
import uvicorn
import shutil
import os
import sys
import subprocess

app = FastAPI()

# Global state to track clipboard content
# type: "text" | "file"
# content: text content or filename
clipboard_state = {"type": "text", "content": ""}

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def get_local_clipboard():
    """Get current clipboard content (text or file path)"""
    # Check for file on macOS
    if sys.platform == "darwin":
        try:
            # simpler osascript to get file alias
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

@app.get("/get")
async def get_clipboard():
    """Return current clipboard state, updating from local if needed"""
    global clipboard_state
    
    # Check if local clipboard has changed (Server acting as a user)
    current_local = get_local_clipboard()
    
    # We need to decide if local is 'newer'. 
    # Logic: If local content is different from what we think the global state is.
    # Caveat: If we just set local from a client upload, we don't want to think it's a new user action.
    # But since we update clipboard_state *before* setting local in /upload and /set, 
    # strict equality check might work if we are careful about filenames vs paths.
    
    local_changed = False
    
    if current_local["type"] == "text":
        if clipboard_state["type"] != "text" or current_local["content"] != clipboard_state["content"]:
            clipboard_state = current_local
            local_changed = True
            
    elif current_local["type"] == "file":
        # Local content is a full path (e.g., /Users/me/Desktop/a.png)
        # Global content is just a filename (e.g., a.png)
        # We need to see if the local file matches the global file concept
        
        local_filename = os.path.basename(current_local["content"])
        
        # If global isn't a file, OR filenames don't match
        # (Weak check: different files named 'a.png' will be treated as same, but acceptable for MVP)
        if clipboard_state["type"] != "file" or clipboard_state["content"] != local_filename:
            # Server User copied a new file.
            # We must STAGE it for clients to download.
            src_path = current_local["content"]
            dest_path = os.path.join(DOWNLOAD_DIR, local_filename)
            
            # Copy file to downloads folder if it's not already there
            # (If user copied a file *inside* downloads folder, skip copy)
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
    """Set clipboard state (text or file info)"""
    global clipboard_state
    data = await request.json()
    new_type = data.get("type", "text")
    content = data.get("content", "")
    
    clipboard_state["type"] = new_type
    clipboard_state["content"] = content
    
    # Sync to local clipboard (Server acting as client)
    if new_type == "text":
        set_local_clipboard({"type": "text", "content": content})
        
    return JSONResponse({"status": "ok", "state": clipboard_state})

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Receive a file and update state"""
    global clipboard_state
    file_path = os.path.join(DOWNLOAD_DIR, file.filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    clipboard_state["type"] = "file"
    clipboard_state["content"] = file.filename
    
    # Sync to local clipboard
    set_local_clipboard({"type": "file", "content": file_path})
    
    return JSONResponse({"status": "ok", "filename": file.filename})

@app.get("/download/{filename}")
async def download_file(filename: str):
    """Serve a file"""
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=filename)
    return JSONResponse({"error": "File not found"}, status_code=404)

if __name__ == "__main__":
    # Run Uvicorn with a single worker, no reload, minimal threads
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=6097,
        workers=1,
        reload=False,
        log_level="warning"
    )