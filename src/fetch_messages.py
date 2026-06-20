"""
fetch_messages.py — with archived/pinned/saved chats, ghost mode, configurable
per-chat fetch counts, system message handling, privacy-safe filenames.
"""

import os
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone

from pyrogram import Client
from pyrogram.enums import ChatType, MessageMediaType
from pyrogram.errors import FloodWait

API_ID         = int(os.environ["TG_API_ID"])
API_HASH       = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ["TG_SESSION_STRING"].strip()
FORCE          = os.environ.get("FORCE_FULL", "false").lower() == "true"
INIT_N         = int(os.environ.get("DEFAULT_FETCH_COUNT",  "20"))
UPD_N          = int(os.environ.get("DEFAULT_UPDATE_COUNT", "50"))
GHOST_MODE     = os.environ.get("GHOST_MODE", "false").lower() == "true"


DATA_DIR    = Path("data")
DATA_DIR.mkdir(exist_ok=True)
META_FILE   = DATA_DIR / "meta.json"
CHATS_FILE  = DATA_DIR / "chats.json"
SETTINGS_FILE = DATA_DIR / "chat_settings.json"  # per-chat fetch_n overrides

def load_chat_settings() -> dict:
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text())
    return {}

def _chat_kind(chat_type: ChatType) -> str:
    return {
        ChatType.PRIVATE:    "user",
        ChatType.BOT:        "bot",
        ChatType.GROUP:      "group",
        ChatType.SUPERGROUP: "group",
        ChatType.CHANNEL:    "channel",
    }.get(chat_type, "unknown")

def _media_info(msg) -> dict | None:
    if msg.media is None:
        return None
    mt = msg.media
    if mt == MessageMediaType.PHOTO:
        return {"type": "photo", "id": msg.id}
    if mt == MessageMediaType.DOCUMENT and msg.document:
        doc = msg.document
        return {"type": "document", "id": doc.file_id,
                "filename": doc.file_name, "mime_type": doc.mime_type, "size": doc.file_size}
    if mt == MessageMediaType.VIDEO and msg.video:
        v = msg.video
        return {"type": "document", "id": v.file_id,
                "filename": v.file_name or "video.mp4", "mime_type": v.mime_type, "size": v.file_size}
    if mt == MessageMediaType.AUDIO and msg.audio:
        a = msg.audio
        return {"type": "document", "id": a.file_id,
                "filename": a.file_name or "audio", "mime_type": a.mime_type, "size": a.file_size}
    if mt == MessageMediaType.VOICE and msg.voice:
        return {"type": "document", "id": msg.voice.file_id,
                "filename": "voice.ogg", "mime_type": "audio/ogg", "size": msg.voice.file_size}
    if mt == MessageMediaType.STICKER and msg.sticker:
        return {"type": "sticker", "emoji": msg.sticker.emoji,
                "id": msg.sticker.file_id, "is_animated": msg.sticker.is_animated,
                "mime_type": "image/webp", "size": msg.sticker.file_size}
    if mt == MessageMediaType.ANIMATION and msg.animation:
        return {"type": "document", "id": msg.animation.file_id,
                "filename": msg.animation.file_name or "animation.gif",
                "mime_type": msg.animation.mime_type, "size": msg.animation.file_size}
    if mt == MessageMediaType.VIDEO_NOTE and msg.video_note:
        return {"type": "document", "id": msg.video_note.file_id,
                "filename": "video_note.mp4", "mime_type": "video/mp4",
                "size": msg.video_note.file_size}
    if mt == MessageMediaType.WEB_PAGE and msg.web_page:
        wp = msg.web_page
        return {"type": "webpage", "url": wp.url, "title": wp.title,
                "description": wp.description}
    if mt == MessageMediaType.POLL and msg.poll:
        return {"type": "poll", "question": msg.poll.question,
                "options": [o.text for o in msg.poll.options]}
    if mt == MessageMediaType.CONTACT and msg.contact:
        return {"type": "contact", "name": f"{msg.contact.first_name or ''} {msg.contact.last_name or ''}".strip(),
                "phone": msg.contact.phone_number}
    if mt == MessageMediaType.LOCATION and msg.location:
        return {"type": "location", "lat": msg.location.latitude, "lng": msg.location.longitude}
    return {"type": mt.name.lower() if mt else "unknown"}

def _reactions(msg) -> list:
    if not msg.reactions:
        return []
    return [{"emoji": r.emoji, "count": r.count} for r in msg.reactions.reactions]

def _inline_keyboard(msg) -> list | None:
    if not msg.reply_markup:
        return None
    try:
        rows = []
        for row in msg.reply_markup.inline_keyboard:
            buttons = []
            for btn in row:
                buttons.append({
                    "text":          btn.text,
                    "callback_data": getattr(btn, "callback_data", None),
                    "url":           getattr(btn, "url", None),
                })
            rows.append(buttons)
        return rows
    except Exception:
        return None

# system / service message labels (pin, title change, member join, etc.)
def _service_text(msg) -> str | None:
    """Return a human-readable label for service messages, or None if not a service msg."""
    if not msg.service:
        return None
    svc = msg.service
    svc_name = svc.name if hasattr(svc, "name") else str(svc)
    try:
        if msg.pinned_message:
            preview = (msg.pinned_message.text or "media")[:40]
            return f"📌 Pinned: \"{preview}\""
        if msg.new_chat_title:
            return f"✏️ Changed group name to \"{msg.new_chat_title}\""
        if msg.new_chat_photo:
            return "🖼️ Changed group photo"
        if msg.delete_chat_photo:
            return "🗑️ Removed group photo"
        if msg.new_chat_members:
            names = ", ".join(f"{u.first_name or u.id}" for u in msg.new_chat_members)
            return f"➕ {names} joined the group"
        if msg.left_chat_member:
            u = msg.left_chat_member
            return f"➖ {u.first_name or u.id} left the group"
        if msg.group_chat_created:
            return "👥 Group created"
        if msg.channel_chat_created:
            return "📢 Channel created"
        if msg.migrate_to_chat_id:
            return "🔁 Group upgraded to supergroup"
    except Exception:
        pass
    return f"⚙️ {svc_name}"

def _serialize_message(msg, chat_id: int) -> dict:
    from_id = None
    if msg.from_user:
        from_id = msg.from_user.id
    elif msg.sender_chat:
        from_id = msg.sender_chat.id

    service_text = _service_text(msg)

    reply_preview = None
    if getattr(msg, "reply_to_message", None):
        rm = msg.reply_to_message
        reply_preview = {
            "id":      rm.id,
            "text":    rm.text or rm.caption or _service_text(rm) or "",
            "from_id": rm.from_user.id if rm.from_user else None,
            "media":   _media_info(rm),
        }

    # Telegram "quote" feature (select text + reply with that exact excerpt)
    quote_text = None
    if getattr(msg, "quote", None):
        quote_text = getattr(msg.quote, "text", None)
    elif hasattr(msg, "reply_to_message") and getattr(msg, "quote_text", None):
        quote_text = msg.quote_text

    return {
        "id":              msg.id,
        "chat_id":         chat_id,
        "date":            msg.date.isoformat() if msg.date else None,
        "from_id":         from_id,
        "text":            msg.text or msg.caption or service_text or "",
        "is_service":      bool(service_text),
        "media":           _media_info(msg),
        "reactions":       _reactions(msg),
        "reply_to":        msg.reply_to_message_id,
        "reply_preview":   reply_preview,
        "quote_text":      quote_text,
        "pinned":          getattr(msg, "pinned", False),
        "out":             getattr(msg, "outgoing", False),
        "views":           getattr(msg, "views", None),
        "forwards":        getattr(msg, "forwards", None),
        "inline_keyboard": _inline_keyboard(msg),
        "via_bot":         msg.via_bot.id if msg.via_bot else None,
    }

async def _fetch_chat_info(app: Client, chat_id: int) -> dict:
    try:
        full = await app.get_chat(chat_id)
        return {
            "bio":           getattr(full, "bio", None) or getattr(full, "description", None),
            "members_count": getattr(full, "members_count", None),
            "is_verified":   getattr(full, "is_verified", False),
            "is_scam":       getattr(full, "is_scam", False),
            "is_fake":       getattr(full, "is_fake", False),
            "dc_id":         getattr(full, "dc_id", None),
            "has_photo":     bool(getattr(full, "photo", None)),
        }
    except Exception:
        return {}

async def main():
    meta = {}
    if META_FILE.exists():
        meta = json.loads(META_FILE.read_text())

    first_run = FORCE or not meta.get("initialized", False)
    fetch_n   = INIT_N if first_run else UPD_N
    chat_settings = load_chat_settings()  # {chat_id: {"fetch_n": N}}
    print(f"first_run={first_run}, fetch_n={fetch_n}, ghost_mode={GHOST_MODE}")

    app = Client(
        name="relay",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True,
        no_updates=GHOST_MODE,  # in ghost mode, avoid marking things as seen via update stream
    )

    async with app:
        me = await app.get_me()
        my_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        meta["me"] = {"id": me.id, "name": my_name, "username": me.username}
        print(f"✓ Connected as {my_name}")

        chats_data: dict[str, dict] = {}
        if CHATS_FILE.exists() and not first_run:
            chats_data = json.loads(CHATS_FILE.read_text())

        action_log = []

        # ── Saved Messages (chat with yourself) ──
        try:
            saved_id = me.id
            saved_key = str(saved_id)
            n = chat_settings.get(saved_key, {}).get("fetch_n", fetch_n)
            saved_msgs = []
            async for msg in app.get_chat_history(saved_id, limit=n):
                saved_msgs.append(_serialize_message(msg, saved_id))
            existing = {m["id"]: m for m in chats_data.get(saved_key, {}).get("messages", [])}
            for m in saved_msgs:
                existing[m["id"]] = m
            chats_data[saved_key] = {
                "id": saved_id, "name": "Saved Messages", "kind": "saved",
                "is_bot": False, "username": None, "unread_count": 0,
                "last_message_date": saved_msgs[0]["date"] if saved_msgs else None,
                "messages": list(existing.values()),
                "is_pinned": True, "is_archived": False,
                "bio": None, "members_count": None, "is_verified": False, "is_scam": False,
            }
            print(f"  ↳ Saved Messages: {len(saved_msgs)} messages")
        except Exception as e:
            print(f"  Could not fetch Saved Messages: {e}")

        # ── Regular + archived dialogs ──
        # Pyrogram's get_dialogs(folder_id=1) returns archived; folder_id=0/default = main list
        async def process_dialogs(folder_id, is_archived):
            async for dialog in app.get_dialogs(folder_id=folder_id):
                chat    = dialog.chat
                chat_id = chat.id
                key     = str(chat_id)
                unread  = 0 if GHOST_MODE else (dialog.unread_messages_count or 0)
                is_pinned = getattr(dialog, "is_pinned", False)

                if not first_run and not GHOST_MODE and (dialog.unread_messages_count or 0) == 0:
                    # still update pinned/archived flags even with 0 unread, but skip refetch
                    if key in chats_data:
                        chats_data[key]["is_pinned"] = is_pinned
                        chats_data[key]["is_archived"] = is_archived
                    continue

                name = (
                    getattr(chat, "title", None)
                    or f"{getattr(chat,'first_name','') or ''} {getattr(chat,'last_name','') or ''}".strip()
                    or str(chat_id)
                )
                kind = _chat_kind(chat.type)
                is_bot = chat.type == ChatType.BOT

                n = chat_settings.get(key, {}).get("fetch_n", fetch_n)

                messages = []
                try:
                    async for msg in app.get_chat_history(chat_id, limit=n):
                        messages.append(_serialize_message(msg, chat_id))
                except FloodWait as e:
                    print(f"  FloodWait {e.value}s on {name}, skipping")
                    await asyncio.sleep(e.value)
                    continue
                except Exception as e:
                    print(f"  Error fetching {name}: {e}")
                    continue

                extra = {}
                if first_run or key not in chats_data or "bio" not in chats_data.get(key, {}):
                    extra = await _fetch_chat_info(app, chat_id)
                    await asyncio.sleep(0.2)

                existing = {m["id"]: m for m in chats_data.get(key, {}).get("messages", [])}
                for m in messages:
                    existing[m["id"]] = m

                chats_data[key] = {
                    "id":                chat_id,
                    "name":              name,
                    "kind":              kind,
                    "is_bot":            is_bot,
                    "username":          getattr(chat, "username", None),
                    "unread_count":      unread,
                    "last_message_date": messages[0]["date"] if messages else None,
                    "messages":          list(existing.values()),
                    "is_pinned":         is_pinned,
                    "is_archived":       is_archived,
                    "bio":               extra.get("bio") or chats_data.get(key, {}).get("bio"),
                    "members_count":     extra.get("members_count") or chats_data.get(key, {}).get("members_count"),
                    "is_verified":       extra.get("is_verified", False),
                    "is_scam":           extra.get("is_scam", False),
                    "has_photo":         extra.get("has_photo", chats_data.get(key, {}).get("has_photo", False)),
                }
                action_log.append({"chat": name, "fetched": len(messages)})
                print(f"  ↳ {name}{' [archived]' if is_archived else ''}{' [pinned]' if is_pinned else ''}: {len(messages)} messages")

        await process_dialogs(folder_id=0, is_archived=False)
        try:
            await process_dialogs(folder_id=1, is_archived=True)
        except Exception as e:
            print(f"  Could not fetch archived dialogs: {e}")

        CHATS_FILE.write_text(json.dumps(chats_data, ensure_ascii=False, default=str, indent=2))
        print("✓ chats.json written")

        meta["initialized"] = True
        meta["last_sync"]   = datetime.now(timezone.utc).isoformat()
        meta["sync_log"]    = action_log
        meta["total_chats"] = len(chats_data)
        meta["ghost_mode"]  = GHOST_MODE
        META_FILE.write_text(json.dumps(meta, indent=2, default=str))
        print(f"✓ Done — {len(chats_data)} chats written")

asyncio.run(main())
