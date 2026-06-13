"""
process_queue.py — Pyrogram, fixed peer ID handling, send message, react on any message
"""

import os
import json
import asyncio
import base64
import hashlib
from pathlib import Path
from datetime import datetime, timezone

from pyrogram import Client
from pyrogram.errors import FloodWait, PeerIdInvalid, ChatIdInvalid, UsernameNotOccupied
from cryptography.fernet import Fernet

# ── config ────────────────────────────────────────────────────────────────────
API_ID         = int(os.environ["TG_API_ID"])
API_HASH       = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ["TG_SESSION_STRING"].strip()
RAW_KEY        = os.environ["DATA_KEY"]
QUEUE_B64      = os.environ["QUEUE_B64"]

DATA_DIR     = Path("data")
RESULTS_FILE = DATA_DIR / "queue_results.json"

# ── helpers ───────────────────────────────────────────────────────────────────
def _fernet(passphrase: str) -> Fernet:
    key = base64.urlsafe_b64encode(
        hashlib.sha256(passphrase.encode()).digest()
    )
    return Fernet(key)

F = _fernet(RAW_KEY)

async def resolve_peer(app: Client, chat_id):
    """
    Pyrogram requires peers to be in the session cache before use.
    For supergroups/channels the ID is negative and large (-100xxxxxxxxxx).
    We call get_chat() first which populates the cache, then return the entity.
    """
    try:
        return await app.get_chat(chat_id)
    except (PeerIdInvalid, ChatIdInvalid):
        # Try resolving via dialogs cache — iterate until found
        async for dialog in app.get_dialogs():
            if dialog.chat.id == chat_id:
                return dialog.chat
        raise PeerIdInvalid(f"Could not resolve peer: {chat_id}")

# ── action handlers ───────────────────────────────────────────────────────────

async def handle_send_message(app: Client, action: dict) -> dict:
    chat_id  = action["chat_id"]
    text     = action["text"]
    reply_to = action.get("reply_to")
    await resolve_peer(app, chat_id)
    msg = await app.send_message(
        chat_id,
        text,
        reply_to_message_id=reply_to,
    )
    return {"ok": True, "msg_id": msg.id}

async def handle_react(app: Client, action: dict) -> dict:
    chat_id = action["chat_id"]
    msg_id  = action["msg_id"]
    emoji   = action["emoji"]
    await resolve_peer(app, chat_id)
    await app.send_reaction(chat_id, msg_id, emoji)
    return {"ok": True}

async def handle_mark_read(app: Client, action: dict) -> dict:
    chat_id = action["chat_id"]
    await resolve_peer(app, chat_id)
    await app.read_chat_history(chat_id)
    return {"ok": True}

async def handle_download_request(app: Client, action: dict) -> dict:
    """
    Actual file downloading requires external storage (S3, R2, etc.).
    For now we flag the intent — implement a storage target to enable real downloads.
    """
    return {
        "ok": True,
        "queued": True,
        "note": "Download flagged. Add external storage to enable real downloads.",
    }

async def handle_pin_message(app: Client, action: dict) -> dict:
    chat_id = action["chat_id"]
    msg_id  = action["msg_id"]
    await resolve_peer(app, chat_id)
    await app.pin_chat_message(chat_id, msg_id)
    return {"ok": True}

async def handle_unpin_message(app: Client, action: dict) -> dict:
    chat_id = action["chat_id"]
    msg_id  = action.get("msg_id")
    await resolve_peer(app, chat_id)
    if msg_id:
        await app.unpin_chat_message(chat_id, msg_id)
    else:
        await app.unpin_all_chat_messages(chat_id)
    return {"ok": True}

async def handle_forward(app: Client, action: dict) -> dict:
    from_chat = action["from_chat_id"]
    msg_id    = action["msg_id"]
    to_chat   = action["to_chat_id"]
    await resolve_peer(app, from_chat)
    await resolve_peer(app, to_chat)
    await app.forward_messages(to_chat, from_chat, msg_id)
    return {"ok": True}

async def handle_delete_message(app: Client, action: dict) -> dict:
    chat_id = action["chat_id"]
    msg_id  = action["msg_id"]
    revoke  = action.get("revoke", False)
    await resolve_peer(app, chat_id)
    await app.delete_messages(chat_id, msg_id, revoke=revoke)
    return {"ok": True}

async def handle_edit_message(app: Client, action: dict) -> dict:
    chat_id = action["chat_id"]
    msg_id  = action["msg_id"]
    text    = action["text"]
    await resolve_peer(app, chat_id)
    await app.edit_message_text(chat_id, msg_id, text)
    return {"ok": True}

HANDLERS = {
    "send_message":     handle_send_message,
    "react":            handle_react,
    "mark_read":        handle_mark_read,
    "download_request": handle_download_request,
    "pin_message":      handle_pin_message,
    "unpin_message":    handle_unpin_message,
    "forward":          handle_forward,
    "delete_message":   handle_delete_message,
    "edit_message":     handle_edit_message,
}

# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    raw_json = base64.b64decode(QUEUE_B64).decode()
    queue: list[dict] = json.loads(raw_json)
    print(f"Processing {len(queue)} queued action(s) …")

    app = Client(
        name="relay",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True,
    )

    async with app:
        me = await app.get_me()
        print(f"✓ Connected as {me.first_name} (@{me.username})")

        # pre-warm the peer cache by loading dialogs once
        print("  Pre-warming peer cache…")
        async for _ in app.get_dialogs():
            pass

        results = []
        for i, action in enumerate(queue):
            atype   = action.get("type", "unknown")
            handler = HANDLERS.get(atype)
            try:
                if handler is None:
                    raise ValueError(f"Unknown action type: '{atype}'")
                result = await handler(app, action)
                results.append({"index": i, "type": atype, "status": "ok", "result": result})
                print(f"  [{i}] {atype} → ok")
            except FloodWait as e:
                print(f"  [{i}] {atype} → FloodWait {e.value}s, retrying…")
                await asyncio.sleep(e.value)
                try:
                    result = await handler(app, action)
                    results.append({"index": i, "type": atype, "status": "ok", "result": result})
                    print(f"  [{i}] {atype} → ok (after flood wait)")
                except Exception as e2:
                    results.append({"index": i, "type": atype, "status": "error", "error": str(e2)})
                    print(f"  [{i}] {atype} → error after retry: {e2}")
            except Exception as e:
                results.append({"index": i, "type": atype, "status": "error", "error": str(e)})
                print(f"  [{i}] {atype} → error: {e}")

            await asyncio.sleep(0.6)

    DATA_DIR.mkdir(exist_ok=True)
    RESULTS_FILE.write_text(json.dumps({
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "total":        len(queue),
        "ok":           sum(1 for r in results if r["status"] == "ok"),
        "errors":       sum(1 for r in results if r["status"] == "error"),
        "results":      results,
    }, indent=2))
    print(f"✓ Done — results written to {RESULTS_FILE}")

asyncio.run(main())
