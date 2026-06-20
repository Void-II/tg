"""
process_queue.py — privacy-safe filenames, encrypted everything, sync-after-action,
join/start chat, cleanup workflow support.
"""

import os
import json
import asyncio
import base64
import hashlib
import mimetypes
from pathlib import Path
from datetime import datetime, timezone

from pyrogram import Client
from pyrogram.errors import FloodWait, PeerIdInvalid, ChatIdInvalid
from cryptography.fernet import Fernet

API_ID         = int(os.environ["TG_API_ID"])
API_HASH       = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ["TG_SESSION_STRING"].strip()
RAW_KEY        = os.environ["DATA_KEY"]
QUEUE_B64      = os.environ["QUEUE_B64"]

DATA_DIR     = Path("data")
FILES_DIR    = DATA_DIR / "files"
RESULTS_FILE = DATA_DIR / "queue_results.json"
FILES_INDEX  = DATA_DIR / "files_index.json"

DATA_DIR.mkdir(exist_ok=True)
FILES_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE = 20 * 1024 * 1024

def load_files_index() -> dict:
    if FILES_INDEX.exists():
        try:
            return json.loads(FILES_INDEX.read_text())
        except Exception:
            return {}
    return {}

def save_files_index(index: dict):
    FILES_INDEX.write_text(json.dumps(index, indent=2, default=str))

def make_filename(chat_id, msg_id, ext: str = "") -> str:
    """Normal, predictable filename so files are easy to identify."""
    return f"{chat_id}_{msg_id}{ext}"

async def resolve_peer(app: Client, chat_id):
    try:
        return await app.get_chat(chat_id)
    except (PeerIdInvalid, ChatIdInvalid):
        async for dialog in app.get_dialogs():
            if dialog.chat.id == chat_id:
                return dialog.chat
        raise PeerIdInvalid(f"Could not resolve peer: {chat_id}")

# ── file download helper (privacy-safe names) ────────────────────────────────
async def download_and_store(app: Client, msg, label: str) -> dict:
    import tempfile
    if msg.media is None:
        return {"ok": False, "error": "No media in message"}

    media_obj = (
        msg.document or msg.photo or msg.video or msg.audio
        or msg.voice or msg.sticker or msg.video_note or msg.animation
    )
    if media_obj and hasattr(media_obj, "file_size"):
        size = media_obj.file_size or 0
        if size > MAX_FILE_SIZE:
            return {"ok": False, "error": f"File too large ({size/1048576:.1f} MB > {MAX_FILE_SIZE//1048576} MB limit)"}

    orig_filename = getattr(media_obj, "file_name", None) or f"file_{msg.id}"
    ext = Path(orig_filename).suffix or ""
    if not ext and hasattr(media_obj, "mime_type") and media_obj.mime_type:
        ext = mimetypes.guess_extension(media_obj.mime_type) or ""
    if not ext and msg.photo:
        ext = ".jpg"

    safe_name = make_filename(msg.chat.id, msg.id, ext)
    file_path = FILES_DIR / safe_name

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = os.path.join(tmp, safe_name)
        downloaded = await app.download_media(msg, file_name=tmp_path)
        if not downloaded:
            return {"ok": False, "error": "download_media returned None"}
        raw = Path(downloaded).read_bytes()

    file_path.write_bytes(raw)

    index = load_files_index()
    index[safe_name] = {
        "chat_id":   msg.chat.id,
        "msg_id":    msg.id,
        "filename":  orig_filename,
        "safe_name": safe_name,
        "path":      str(file_path),
        "size":      len(raw),
        "mime_type": getattr(media_obj, "mime_type", None) or ("image/jpeg" if msg.photo else None),
        "label":     label,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    }
    save_files_index(index)

    return {"ok": True, "safe_name": safe_name, "filename": orig_filename, "size": len(raw), "path": str(file_path)}

# ── action handlers ───────────────────────────────────────────────────────────

async def handle_send_message(app: Client, action: dict) -> dict:
    chat_id  = action["chat_id"]
    text     = action["text"]
    reply_to = action.get("reply_to")
    await resolve_peer(app, chat_id)
    msg = await app.send_message(chat_id, text, reply_to_message_id=reply_to)
    return {"ok": True, "msg_id": msg.id}

async def handle_react(app: Client, action: dict) -> dict:
    chat_id = action["chat_id"]; msg_id = action["msg_id"]; emoji = action["emoji"]
    await resolve_peer(app, chat_id)
    await app.send_reaction(chat_id, msg_id, emoji)
    return {"ok": True}

async def handle_mark_read(app: Client, action: dict) -> dict:
    chat_id = action["chat_id"]
    await resolve_peer(app, chat_id)
    await app.read_chat_history(chat_id)
    return {"ok": True}

async def handle_download_request(app: Client, action: dict) -> dict:
    import tempfile
    chat_id = action["chat_id"]; msg_id = action["msg_id"]; kind = action.get("kind", "file")
    await resolve_peer(app, chat_id)

    if kind == "profile_pic":
        chat = await app.get_chat(chat_id)
        if not chat.photo:
            return {"ok": False, "error": "No profile photo"}
        safe_name = f"profile_{chat_id}.jpg"
        file_path = FILES_DIR / safe_name
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = os.path.join(tmp, safe_name)
            downloaded = await app.download_media(chat.photo.big_file_id, file_name=tmp_path)
            raw = Path(downloaded).read_bytes() if downloaded else b""
        file_path.write_bytes(raw)
        index = load_files_index()
        index[safe_name] = {
            "chat_id": chat_id, "msg_id": None, "filename": "profile.jpg",
            "safe_name": safe_name, "path": str(file_path),
            "size": len(raw), "mime_type": "image/jpeg", "label": "profile_pic",
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        }
        save_files_index(index)
        return {"ok": True, "safe_name": safe_name, "kind": "profile_pic"}

    if kind == "all_profile_media":
        results = []
        i = 0
        async for photo in app.get_chat_photos(chat_id):
            i += 1
            safe_name = f"profile_{chat_id}_{i}.jpg"
            file_path = FILES_DIR / safe_name
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = os.path.join(tmp, safe_name)
                downloaded = await app.download_media(photo.file_id, file_name=tmp_path)
                raw = Path(downloaded).read_bytes() if downloaded else b""
            file_path.write_bytes(raw)
            index = load_files_index()
            index[safe_name] = {
                "chat_id": chat_id, "msg_id": None, "filename": f"profile_{i}.jpg",
                "safe_name": safe_name, "path": str(file_path),
                "size": len(raw), "mime_type": "image/jpeg", "label": "profile_media",
                "downloaded_at": datetime.now(timezone.utc).isoformat(),
            }
            save_files_index(index)
            results.append(safe_name)
            await asyncio.sleep(0.3)
        return {"ok": True, "downloaded": len(results), "files": results}

    msg = await app.get_messages(chat_id, msg_id)
    if not msg:
        return {"ok": False, "error": f"Message {msg_id} not found"}
    return await download_and_store(app, msg, kind)

async def handle_pin_message(app: Client, action: dict) -> dict:
    chat_id = action["chat_id"]; msg_id = action["msg_id"]
    await resolve_peer(app, chat_id)
    await app.pin_chat_message(chat_id, msg_id)
    return {"ok": True}

async def handle_unpin_message(app: Client, action: dict) -> dict:
    chat_id = action["chat_id"]; msg_id = action.get("msg_id")
    await resolve_peer(app, chat_id)
    if msg_id:
        await app.unpin_chat_message(chat_id, msg_id)
    else:
        await app.unpin_all_chat_messages(chat_id)
    return {"ok": True}

async def handle_forward(app: Client, action: dict) -> dict:
    from_chat = action["from_chat_id"]; msg_id = action["msg_id"]; to_chat = action["to_chat_id"]
    await resolve_peer(app, from_chat); await resolve_peer(app, to_chat)
    await app.forward_messages(to_chat, from_chat, msg_id)
    return {"ok": True}

async def handle_delete_message(app: Client, action: dict) -> dict:
    chat_id = action["chat_id"]; msg_id = action["msg_id"]; revoke = action.get("revoke", False)
    await resolve_peer(app, chat_id)
    await app.delete_messages(chat_id, msg_id, revoke=revoke)
    return {"ok": True}

async def handle_edit_message(app: Client, action: dict) -> dict:
    chat_id = action["chat_id"]; msg_id = action["msg_id"]; text = action["text"]
    await resolve_peer(app, chat_id)
    await app.edit_message_text(chat_id, msg_id, text)
    return {"ok": True}

async def handle_bot_callback(app: Client, action: dict) -> dict:
    chat_id = action["chat_id"]; msg_id = action["msg_id"]; callback_data = action["callback_data"]
    await resolve_peer(app, chat_id)
    await app.request_callback_answer(chat_id, msg_id, callback_data)
    return {"ok": True}

async def handle_join_chat(app: Client, action: dict) -> dict:
    """Join a public/private channel/group, or start a bot, or open a user DM by username/id/link."""
    target = str(action.get("target", "")).strip()
    start_param = action.get("start_param")  # optional /start payload for bots

    if "joinchat" in target or "+t.me" in target or target.startswith("https://t.me/+"):
        result = await app.join_chat(target)
        return {"ok": True, "chat_id": result.id, "title": result.title or result.first_name}

    if target.lstrip("-").isdigit():
        chat = await app.get_chat(int(target))
        return {"ok": True, "chat_id": chat.id, "title": getattr(chat, "title", None) or chat.first_name}

    username = target.replace("https://t.me/", "").replace("@", "").strip()
    chat = await app.get_chat(username)

    if chat.type and chat.type.name == "BOT":
        # starting a bot: send /start (with optional param) instead of join_chat
        if start_param:
            await app.send_message(chat.id, f"/start {start_param}")
        else:
            await app.send_message(chat.id, "/start")
        return {"ok": True, "chat_id": chat.id, "title": chat.first_name, "action": "started_bot"}

    if chat.type and chat.type.name in ("GROUP", "SUPERGROUP", "CHANNEL"):
        result = await app.join_chat(username)
        return {"ok": True, "chat_id": result.id, "title": result.title}

    # private user — nothing to "join", just confirm chat resolved so DM is possible
    return {"ok": True, "chat_id": chat.id, "title": chat.first_name, "action": "resolved_user"}

async def handle_set_chat_fetch_n(app: Client, action: dict) -> dict:
    """Store a per-chat override for how many messages to fetch on next sync."""
    chat_id = action["chat_id"]; n = int(action["fetch_n"])
    settings_file = DATA_DIR / "chat_settings.json"
    settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}
    settings[str(chat_id)] = {"fetch_n": max(1, min(n, 500))}
    settings_file.write_text(json.dumps(settings, indent=2))
    return {"ok": True, "chat_id": chat_id, "fetch_n": n}

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
    "bot_callback":     handle_bot_callback,
    "join_chat":        handle_join_chat,
    "set_chat_fetch_n": handle_set_chat_fetch_n,
}

async def main():
    raw_json = base64.b64decode(QUEUE_B64).decode()
    queue: list[dict] = json.loads(raw_json)
    print(f"Processing {len(queue)} queued action(s) …")

    app = Client(name="relay", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING, in_memory=True)

    async with app:
        me = await app.get_me()
        print(f"✓ Connected as {me.first_name} (@{me.username})")
        print("  Pre-warming peer cache…")
        async for _ in app.get_dialogs():
            pass
        print("  Peer cache ready")

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
                except Exception as e2:
                    results.append({"index": i, "type": atype, "status": "error", "error": str(e2)})
            except Exception as e:
                results.append({"index": i, "type": atype, "status": "error", "error": str(e)})
                print(f"  [{i}] {atype} → error: {e}")
            await asyncio.sleep(0.6)

    RESULTS_FILE.write_text(json.dumps({
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "total": len(queue),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "results": results,
    }, indent=2, default=str))
    print(f"✓ Done — results written to {RESULTS_FILE}")

asyncio.run(main())
