from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import pyperclip
import uvicorn

app = FastAPI()

@app.get("/get")
async def get_clipboard():
    """Return current clipboard text"""
    return JSONResponse({"text": pyperclip.paste()})

@app.post("/set")
async def set_clipboard(request: Request):
    """Set clipboard text"""
    data = await request.json()
    text = data.get("text", "")
    pyperclip.copy(text)
    return JSONResponse({"status": "ok", "set": text})

if __name__ == "__main__":
    # Run Uvicorn with a single worker, no reload, minimal threads
    uvicorn.run(
        "clipboard_server_fastapi:app",
        host="0.0.0.0",
        port=6097,
        workers=1,
        reload=False,
        log_level="warning"
    )