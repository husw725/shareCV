import asyncio
import httpx
import pyperclip

SERVER = "http://10.0.6.136:6097"  # change to your server IP
POLL_INTERVAL = 2.0  # seconds between checks (adjust to balance speed vs. CPU use)

async def sync_clipboard():
    last_local = ""
    last_remote = ""
    async with httpx.AsyncClient(timeout=3.0) as client:
        print(f"🔗 Connected to {SERVER} — syncing every {POLL_INTERVAL}s (Ctrl+C to stop)\n")
        while True:
            try:
                # Fetch remote clipboard
                resp = await client.get(f"{SERVER}/get")
                remote_text = resp.json()["text"]

                # Local clipboard
                local_text = pyperclip.paste()

                # Remote → Local update
                if remote_text != last_remote and remote_text != local_text:
                    pyperclip.copy(remote_text)
                    print(f"[⬇️] Updated local clipboard: {remote_text[:60]}...")
                    last_remote = remote_text

                # Local → Remote update
                if local_text != last_local:
                    await client.post(f"{SERVER}/set", json={"text": local_text})
                    print(f"[⬆️] Sent to remote: {local_text[:60]}...")
                    last_local = local_text

            except Exception as e:
                print(f"[⚠️] {type(e).__name__}: {e}")

            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    try:
        asyncio.run(sync_clipboard())
    except KeyboardInterrupt:
        print("\n🛑 Stopped clipboard sync.")