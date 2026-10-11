"""Telegram family photo bot.

The idea
--------
* A private Telegram group with Topics on. Each family has one topic. The bot is an admin.
* Photos are posted in a family's topic AS FILES (Telegram never compresses files, so
  they keep full quality). The photos stay on Telegram; MongoDB only stores an index
  (file id, family, date, location) plus who is linked to which family.
* Clients open the bot privately and link themselves with:
      first name  ->  last name  ->  the family's 6-digit code
  Then they can browse ONLY their family's photos by date or by location.

Group commands (group admins only, nobody gets "marked" as an admin by the bot):
    /register LastName   inside a topic: makes the family and replies with its 6-digit code
    /code                inside a topic: shows the family's code again
    /members             inside a topic: lists the people linked to that family
    /unlink FirstName    inside a topic: unlinks a person (or /unlink <telegram id>)
    /remove              reply to a photo: asks "are you sure", then deletes that exact photo
    /families            lists every family with its code and photo count

Private chat, for you when you are away from the group:
    /addphotos           asks for the password EVERY time, then for the family (last name +
                         code), then you send files; the bot copies them into the family's
                         topic. Nothing about your account is saved. /done ends it.

Caption for files you post (all optional):
    Central Park
    2026-07-04 | Central Park
    2026-07-04
No date -> the day it was posted. No location -> "No location".
For albums, one caption on any file applies to the whole album.
"""

import asyncio
import functools
import hmac
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pymongo import AsyncMongoClient
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    Update,
)
from telegram.constants import ChatAction, ChatMemberStatus
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGODB_URI = os.environ["MONGODB_URI"]
DB_NAME = os.getenv("MONGODB_DB", "photobot")
GROUP_ID = int(os.getenv("GROUP_ID", "0") or 0)  # your private photo group; use /id to find it
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")  # needed for /addphotos in private chat
TZ = ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))
# Stops clients from forwarding/saving files from the chat (they can still screenshot)
PROTECT_CONTENT = os.getenv("PROTECT_CONTENT", "false").lower() == "true"
MAX_SEND = int(os.getenv("MAX_SEND", "200"))  # max files sent per button press
PAGE_SIZE = 20  # buttons per page in the date/location lists
MAX_FAILS = 5  # wrong guesses allowed before a lock ...
LOCK_SECONDS = 3600  # ... and how long the lock lasts
SESSION_SECONDS = 900  # /addphotos session idle timeout (15 minutes)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("photobot")

db = None  # set in post_init (the Mongo client must be created inside the running loop)

COMPRESSED_MSG = (
    "That came in as a compressed photo, so I did not add it. "
    "Please send it as a File instead (attach → File, or untick “compress” on desktop) "
    "so it keeps its full quality."
)


# ---------- text helpers ----------

def normalize(text: str) -> str:
    """'  García-Lopez ' -> 'garcialopez' (ignores case, accents, spaces, punctuation)."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", "", text.casefold())


CAPTION_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*(?:[|,\-–]\s*)?(.*)$", re.S)


def parse_caption(caption: str | None, default_date: str) -> tuple[str, str | None]:
    """Return (date 'YYYY-MM-DD', location or None) from a caption."""
    caption = (caption or "").strip()
    day, loc = default_date, caption
    m = CAPTION_RE.match(caption)
    if m:
        try:
            day = date.fromisoformat(m.group(1)).isoformat()
            loc = m.group(2)
        except ValueError:
            loc = caption  # looked like a date but wasn't one: treat it all as location
    loc = " ".join(loc.split())[:80]
    return day, (loc or None)


def loc_id(location: str | None) -> str:
    import hashlib

    if not location:
        return "none"
    return hashlib.md5(location.casefold().encode()).hexdigest()[:10]


def pretty_date(day: str) -> str:
    return date.fromisoformat(day).strftime("%b %d, %Y")


FAMILY_RE = re.compile(r"^(.*?)[\s,:;\-]*(\d{6})\s*$", re.S)


def parse_family_and_code(text: str) -> tuple[str, str] | None:
    """'Garcia 123456' -> ('Garcia', '123456'); None if it doesn't look right."""
    m = FAMILY_RE.match(text.strip())
    if not m or not normalize(m.group(1)):
        return None
    return m.group(1).strip(), m.group(2)


def lock_text(seconds: int) -> str:
    mins = max(1, -(-seconds // 60))
    return f"Too many wrong tries. Please try again in about {mins} minute(s)."


# ---------- wrong-guess limits (stored in Mongo so a restart doesn't reset them) ----------

async def locked_for(key: str) -> int:
    doc = await db.attempts.find_one({"_id": key})
    if doc and doc.get("locked_until", 0) > time.time():
        return int(doc["locked_until"] - time.time())
    return 0


async def record_fail(key: str) -> bool:
    """Count a wrong guess. Returns True if this one just triggered the lock."""
    await db.attempts.update_one({"_id": key}, {"$inc": {"fails": 1}}, upsert=True)
    doc = await db.attempts.find_one({"_id": key})
    if doc and doc.get("fails", 0) >= MAX_FAILS:
        await db.attempts.update_one(
            {"_id": key}, {"$set": {"fails": 0, "locked_until": time.time() + LOCK_SECONDS}}
        )
        return True
    return False


async def clear_fails(key: str) -> None:
    await db.attempts.delete_one({"_id": key})


# ---------- shared helpers ----------

async def new_code() -> str:
    while True:
        code = f"{secrets.randbelow(10**6):06d}"
        if not await db.families.find_one({"code": code}):
            return code


async def send_with_retry(coro_factory):
    for _ in range(5):
        try:
            return await coro_factory()
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    raise RuntimeError("Gave up after repeated rate limits")


async def is_group_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def admin_only(fn):
    """Group commands: only people who are admins of the group may use them."""

    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        anonymous_admin = bool(msg.sender_chat and msg.sender_chat.id == msg.chat_id)
        user = update.effective_user
        if not anonymous_admin and not (
            user and await is_group_admin(context.bot, msg.chat_id, user.id)
        ):
            await msg.reply_text("Only group admins can use this command.")
            return
        await fn(update, context)

    return wrapper


async def index_file(
    thread_id: int,
    message_id: int,
    file_id: str,
    caption: str | None,
    media_group_id: str | None,
    sent_at: datetime,
) -> None:
    """Save one file in the index with its date/location (from the caption)."""
    default_day = sent_at.astimezone(TZ).date().isoformat()
    day, loc = parse_caption(caption, default_day)
    has_caption = bool((caption or "").strip())
    lid = loc_id(loc)
    now = datetime.now(timezone.utc)

    set_doc = {
        "thread_id": thread_id,
        "file_id": file_id,
        "media_group_id": media_group_id,
    }
    if has_caption:
        set_doc.update(date=day, location=loc, loc_id=lid, has_caption=True)
        insert_doc = {"added": now}
        if media_group_id:  # apply the caption to the rest of the album
            await db.photos.update_many(
                {
                    "media_group_id": media_group_id,
                    "has_caption": {"$ne": True},
                    "message_id": {"$ne": message_id},
                },
                {"$set": {"date": day, "location": loc, "loc_id": lid}},
            )
    else:
        inherit = (
            await db.photos.find_one({"media_group_id": media_group_id, "has_caption": True})
            if media_group_id
            else None
        )
        insert_doc = {
            "date": inherit["date"] if inherit else day,
            "location": inherit["location"] if inherit else None,
            "loc_id": inherit["loc_id"] if inherit else "none",
            "has_caption": False,
            "added": now,
        }

    await db.photos.update_one(
        {"message_id": message_id},
        {"$set": set_doc, "$setOnInsert": insert_doc},
        upsert=True,
    )


async def warn_compressed(msg, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = msg.media_group_id or f"m{msg.message_id}"
    warned = context.bot_data.setdefault("warned", [])
    if key in warned:
        return
    warned.append(key)
    del warned[:-200]
    await msg.reply_text(COMPRESSED_MSG)


# ---------- group side ----------

@admin_only
async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg.is_topic_message:
        await msg.reply_text("Send this command inside a family's topic.")
        return
    name = " ".join(context.args).strip()
    if not normalize(name):
        await msg.reply_text("Usage: /register LastName  (send it inside the family's topic)")
        return
    thread_id = msg.message_thread_id
    existing = await db.families.find_one({"thread_id": thread_id})
    code = existing["code"] if existing else await new_code()
    await db.families.update_one(
        {"thread_id": thread_id},
        {
            "$set": {"name": name, "key": normalize(name)},
            "$setOnInsert": {"code": code, "created": datetime.now(timezone.utc)},
        },
        upsert=True,
    )
    verb = "Family updated" if existing else "Family made"
    await msg.reply_text(
        f"✅ {verb}: {name}\nCode: {code}\n\n"
        "Give the family their last name and this code. "
        "They link themselves in the bot with /start."
    )
    log.info("%s: %s", verb, name)


async def _family_for_topic(msg):
    """The family for the topic this message was sent in, or None (and tells the admin)."""
    if not msg.is_topic_message:
        await msg.reply_text("Send this command inside a family's topic.")
        return None
    fam = await db.families.find_one({"thread_id": msg.message_thread_id})
    if not fam:
        await msg.reply_text("This topic isn't a family yet. Send /register LastName first.")
    return fam


@admin_only
async def code_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    fam = await _family_for_topic(msg)
    if fam:
        await msg.reply_text(f"{fam['name']} family code: {fam['code']}")


@admin_only
async def members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    fam = await _family_for_topic(msg)
    if not fam:
        return
    people = await db.users.find({"thread_id": fam["thread_id"]}).sort("first_name", 1).to_list()
    if not people:
        await msg.reply_text(f"No one has linked to the {fam['name']} family yet.")
        return
    lines = [
        f"• {p['first_name']} (id {p['_id']}" + (f", @{p['username']}" if p.get("username") else "") + ")"
        for p in people
    ]
    await msg.reply_text(f"{fam['name']} family members:\n" + "\n".join(lines))


@admin_only
async def unlink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    fam = await _family_for_topic(msg)
    if not fam:
        return
    arg = " ".join(context.args).strip()
    if not arg:
        await msg.reply_text("Usage: /unlink FirstName  (or /unlink <telegram id> from /members)")
        return
    people = await db.users.find({"thread_id": fam["thread_id"]}).to_list()
    if arg.isdigit():
        matches = [p for p in people if str(p["_id"]) == arg]
    else:
        matches = [p for p in people if normalize(p["first_name"]) == normalize(arg)]
    if not matches:
        await msg.reply_text(f"No one called “{arg}” is linked to the {fam['name']} family.")
        return
    if len(matches) > 1:
        ids = "\n".join(f"• {p['first_name']}: /unlink {p['_id']}" for p in matches)
        await msg.reply_text("More than one person has that first name. Pick one:\n" + ids)
        return
    person = matches[0]
    await db.users.delete_one({"_id": person["_id"]})
    await msg.reply_text(
        f"Unlinked {person['first_name']} from the {fam['name']} family. "
        "They can link again with the code."
    )


@admin_only
async def families(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    fams = await db.families.find().sort("name", 1).to_list()
    if not fams:
        await msg.reply_text("No families yet. Send /register LastName inside a topic.")
        return
    cursor = await db.photos.aggregate([{"$group": {"_id": "$thread_id", "n": {"$sum": 1}}}])
    counts = {d["_id"]: d["n"] for d in await cursor.to_list()}
    lines = [f"• {f['name']}: code {f['code']}, {counts.get(f['thread_id'], 0)} file(s)" for f in fams]
    await msg.reply_text("Families:\n" + "\n".join(lines))


@admin_only
async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    target = msg.reply_to_message
    if not target or not (target.photo or target.document):
        await msg.reply_text("Reply to the photo you want to remove with /remove.")
        return
    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes, delete it", callback_data=f"rm:y:{target.message_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"rm:n:{target.message_id}"),
            ]
        ]
    )
    await msg.reply_text(
        "Are you sure? That photo will be deleted from the group and from the family's list.",
        reply_markup=buttons,
    )


async def on_remove_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not await is_group_admin(context.bot, q.message.chat_id, q.from_user.id):
        await q.answer("Only group admins can do this.", show_alert=True)
        return
    await q.answer()
    _, action, mid = q.data.split(":")
    message_id = int(mid)
    if action == "n":
        await q.edit_message_text("Cancelled, nothing was deleted.")
        return
    try:
        await context.bot.delete_message(q.message.chat_id, message_id)
    except TelegramError as e:
        await q.edit_message_text(
            f"I couldn't delete it ({e.message}). Make sure I'm an admin with the "
            "“Delete messages” right, and that the photo is less than 48 hours old or I'm an admin."
        )
        return
    await db.photos.delete_one({"message_id": message_id})
    await q.edit_message_text("🗑 That photo was deleted.")


async def on_group_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg.is_topic_message:  # ignore the General topic
        return
    if msg.photo:  # compressed photo: refuse, ask for a file
        await warn_compressed(msg, context)
        return
    doc = msg.document
    if not doc:
        return
    await index_file(
        msg.message_thread_id, msg.message_id, doc.file_id, msg.caption, msg.media_group_id, msg.date
    )
    if not await db.families.find_one({"thread_id": msg.message_thread_id}):
        asked = context.bot_data.setdefault("asked", set())
        if msg.message_thread_id not in asked:
            asked.add(msg.message_thread_id)
            await msg.reply_text(
                "I saved this, but this topic isn't a family yet. "
                "Send /register LastName here so clients can link to it."
            )


# ---------- private chat: linking ----------

def menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📅 By date", callback_data="d:0"),
                InlineKeyboardButton("📍 By location", callback_data="l:0"),
            ],
            [InlineKeyboardButton("🖼 All photos", callback_data="all")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if await db.users.find_one({"_id": user.id}):
        await update.message.reply_text(
            "Welcome back! What would you like to see?", reply_markup=menu_markup()
        )
        return
    lock = await locked_for(f"link:{user.id}")
    if lock:
        await update.message.reply_text(lock_text(lock))
        return
    context.user_data.pop("up", None)
    context.user_data["link"] = {"stage": "first"}
    await update.message.reply_text("Hi! 👋 Let's get you to your photos. What's your first name?")


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await db.users.find_one({"_id": update.effective_user.id}):
        await update.message.reply_text("What would you like to see?", reply_markup=menu_markup())
    else:
        await update.message.reply_text("Send /start to link your account first.")


async def handle_link_step(update: Update, context: ContextTypes.DEFAULT_TYPE, link: dict) -> None:
    msg = update.message
    user = update.effective_user
    text = (msg.text or "").strip()
    stage = link["stage"]

    if stage == "first":
        if not normalize(text) or len(text) > 40:
            await msg.reply_text("Please send just your first name.")
            return
        link.update(stage="last", first=text)
        await msg.reply_text(f"Nice to meet you, {text}! What's your family's last name?")

    elif stage == "last":
        if not normalize(text) or len(text) > 60:
            await msg.reply_text("Please send your family's last name.")
            return
        link.update(stage="code", last=text)
        await msg.reply_text("Thanks! Now the 6-digit code you were given.")

    elif stage == "code":
        key = f"link:{user.id}"
        lock = await locked_for(key)
        if lock:
            context.user_data.pop("link", None)
            await msg.reply_text(lock_text(lock))
            return
        code = re.sub(r"\D", "", text)
        fam = None
        if len(code) == 6:
            fam = await db.families.find_one({"key": normalize(link["last"]), "code": code})
        if not fam:
            locked = await record_fail(key)
            context.user_data.pop("link", None)
            await msg.reply_text(
                "Those details didn't match any family."
                + (" " + lock_text(LOCK_SECONDS) if locked else " Send /start to try again.")
            )
            return
        await clear_fails(key)
        await db.users.update_one(
            {"_id": user.id},
            {
                "$set": {
                    "thread_id": fam["thread_id"],
                    "first_name": link["first"],
                    "username": user.username,
                    "linked_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
        context.user_data.pop("link", None)
        log.info("Linked %s (%s) to family %s", link["first"], user.id, fam["name"])
        await msg.reply_text(
            f"You're in, {link['first']}! 🎉 Here are the {fam['name']} family's photos.",
            reply_markup=menu_markup(),
        )


# ---------- private chat: /addphotos (password every time, nothing saved about you) ----------

async def addphotos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not ADMIN_PASSWORD:
        await update.message.reply_text("Adding photos from here isn't set up (no password configured).")
        return
    lock = await locked_for(f"pw:{user.id}")
    if lock:
        await update.message.reply_text(lock_text(lock))
        return
    context.user_data.pop("link", None)
    context.user_data["up"] = {"stage": "password", "until": time.time() + SESSION_SECONDS}
    await update.message.reply_text(
        "Please type the password. I'll delete your message right after."
    )


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    up = context.user_data.pop("up", None)
    if not up:
        await update.message.reply_text("Nothing to finish. Send /addphotos to add photos.")
        return
    await update.message.reply_text(f"Done. Added {up.get('count', 0)} file(s). The session is closed.")


def get_session(context: ContextTypes.DEFAULT_TYPE):
    """Returns (session or None, expired?). Sessions live in memory only."""
    up = context.user_data.get("up")
    if up and up["until"] < time.time():
        context.user_data.pop("up", None)
        return None, True
    return up, False


async def handle_upload_step(update: Update, context: ContextTypes.DEFAULT_TYPE, up: dict) -> None:
    msg = update.message
    user = update.effective_user
    chat_id = msg.chat_id
    text = (msg.text or "").strip()
    key = f"pw:{user.id}"

    if up["stage"] == "password":
        try:
            await msg.delete()  # don't leave the password sitting in the chat
        except TelegramError:
            pass
        lock = await locked_for(key)
        if lock:
            context.user_data.pop("up", None)
            await context.bot.send_message(chat_id, lock_text(lock))
            return
        if hmac.compare_digest(text.encode(), ADMIN_PASSWORD.encode()):
            await clear_fails(key)
            up.update(stage="family", until=time.time() + SESSION_SECONDS)
            await context.bot.send_message(
                chat_id,
                "✅ Password accepted. Which family? Send the last name and the 6-digit code, "
                "for example: Garcia 123456",
            )
        else:
            locked = await record_fail(key)
            context.user_data.pop("up", None)
            await context.bot.send_message(
                chat_id, "Wrong password." + (" " + lock_text(LOCK_SECONDS) if locked else "")
            )

    elif up["stage"] == "family":
        parsed = parse_family_and_code(text)
        fam = (
            await db.families.find_one({"key": normalize(parsed[0]), "code": parsed[1]})
            if parsed
            else None
        )
        if not fam:
            await msg.reply_text("No family matches that. Send the last name and code again, or /done to cancel.")
            return
        up.update(stage="files", thread_id=fam["thread_id"], name=fam["name"], count=0, last_mg=None,
                  until=time.time() + SESSION_SECONDS)
        await msg.reply_text(
            f"Ready for the {fam['name']} family. Send the photos as Files (full quality). "
            "A caption like “2026-07-04 | Central Park” sets the date and place. "
            "Send /done when you're finished."
        )

    else:  # files stage but the person typed text
        await msg.reply_text("Send the photos as Files, or /done to finish.")


async def on_private_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    up, expired = get_session(context)
    if expired:
        await msg.reply_text("That upload session timed out. Send /addphotos to start again.")
        return
    if not up or up["stage"] != "files":
        return  # not in an upload session: ignore
    if msg.photo:
        await warn_compressed(msg, context)
        return
    doc = msg.document
    try:
        sent = await send_with_retry(
            lambda: context.bot.send_document(
                GROUP_ID, doc.file_id, message_thread_id=up["thread_id"], caption=msg.caption
            )
        )
    except (TelegramError, RuntimeError):
        log.exception("Failed copying a file into the group")
        await msg.reply_text("I couldn't add that file to the group. Check that I'm still an admin there.")
        return
    await index_file(up["thread_id"], sent.message_id, doc.file_id, msg.caption, msg.media_group_id, msg.date)
    up["count"] += 1
    up["until"] = time.time() + SESSION_SECONDS
    if not msg.media_group_id or msg.media_group_id != up.get("last_mg"):
        up["last_mg"] = msg.media_group_id
        await msg.reply_text(f"✅ Added to the {up['name']} family. Send more, or /done.")


async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    up, expired = get_session(context)
    if expired:
        await msg.reply_text("That upload session timed out. Send /addphotos to start again.")
        return
    if up:
        await handle_upload_step(update, context, up)
        return
    link = context.user_data.get("link")
    if link:
        await handle_link_step(update, context, link)
        return
    if await db.users.find_one({"_id": update.effective_user.id}):
        await msg.reply_text("Pick what you'd like to see:", reply_markup=menu_markup())
    else:
        await msg.reply_text("Send /start to link your account.")


# ---------- private chat: browsing ----------

def paged_keyboard(items: list[tuple[str, str]], page: int, prefix: str) -> InlineKeyboardMarkup:
    """items = [(label, callback_data)]; prefix = 'd' or 'l' for the prev/next buttons."""
    chunk = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    rows = [
        [InlineKeyboardButton(lbl, callback_data=cb) for lbl, cb in chunk[i : i + 2]]
        for i in range(0, len(chunk), 2)
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Newer" if prefix == "d" else "⬅️ Prev", callback_data=f"{prefix}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < len(items):
        nav.append(InlineKeyboardButton("Older ➡️" if prefix == "d" else "Next ➡️", callback_data=f"{prefix}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


async def send_photos(bot, chat_id: int, query: dict) -> None:
    cursor = db.photos.find(query).sort([("date", 1), ("message_id", 1)]).limit(MAX_SEND + 1)
    docs = await cursor.to_list()
    if not docs:
        await bot.send_message(chat_id, "No photos here yet.")
        return
    truncated = len(docs) > MAX_SEND
    docs = docs[:MAX_SEND]

    # Albums hold up to 10 items and share ONE caption, so only files with the same
    # date and location go in the same album (otherwise the caption would be wrong).
    groups: list[list[dict]] = []
    for d in docs:
        same = (
            groups
            and len(groups[-1]) < 10
            and (groups[-1][0]["date"], groups[-1][0].get("loc_id")) == (d["date"], d.get("loc_id"))
        )
        if same:
            groups[-1].append(d)
        else:
            groups.append([d])

    failed = 0
    for group in groups:
        first = group[0]
        caption = pretty_date(first["date"]) + (f" · {first['location']}" if first.get("location") else "")
        await bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        try:
            if len(group) == 1:
                await send_with_retry(
                    lambda: bot.send_document(
                        chat_id, first["file_id"], caption=caption, protect_content=PROTECT_CONTENT
                    )
                )
            else:
                media = [
                    InputMediaDocument(media=d["file_id"], caption=caption if j == 0 else None)
                    for j, d in enumerate(group)
                ]
                await send_with_retry(
                    lambda: bot.send_media_group(chat_id, media, protect_content=PROTECT_CONTENT)
                )
        except (TelegramError, RuntimeError):
            log.exception("Failed sending a batch")
            failed += len(group)
        await asyncio.sleep(1.2)  # stay under Telegram rate limits

    note = ""
    if truncated:
        note += f"\n\nShowing the first {MAX_SEND}. Use “By date” or “By location” to see the rest."
    if failed:
        note += f"\n\n{failed} file(s) couldn't be sent. Please tell the photographer."
    await bot.send_message(chat_id, "That's everything! 📸" + note + "\n\nAnything else?", reply_markup=menu_markup())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    user_doc = await db.users.find_one({"_id": q.from_user.id})
    if not user_doc:
        await q.message.reply_text("Please send /start to link your account first.")
        return
    thread_id = user_doc["thread_id"]
    data = q.data
    chat_id = q.message.chat_id

    async def show(text: str, markup: InlineKeyboardMarkup) -> None:
        try:
            await q.edit_message_text(text, reply_markup=markup)
        except TelegramError:
            pass  # e.g. "message is not modified"

    if data == "menu":
        await show("What would you like to see?", menu_markup())

    elif data.startswith("d:"):
        cur = await db.photos.aggregate(
            [
                {"$match": {"thread_id": thread_id}},
                {"$group": {"_id": "$date", "n": {"$sum": 1}}},
                {"$sort": {"_id": -1}},
            ]
        )
        rows = await cur.to_list()
        if not rows:
            await show("No photos yet. Check back soon!", menu_markup())
            return
        items = [(f"{pretty_date(r['_id'])} ({r['n']})", f"D:{r['_id']}") for r in rows]
        await show("Pick a date:", paged_keyboard(items, int(data[2:]), "d"))

    elif data.startswith("l:"):
        cur = await db.photos.aggregate(
            [
                {"$match": {"thread_id": thread_id}},
                {"$group": {"_id": "$loc_id", "loc": {"$first": "$location"}, "n": {"$sum": 1}}},
            ]
        )
        rows = await cur.to_list()
        if not rows:
            await show("No photos yet. Check back soon!", menu_markup())
            return
        rows.sort(key=lambda r: (r["_id"] == "none", (r["loc"] or "").casefold()))
        items = [((r["loc"] or "No location")[:40] + f" ({r['n']})", f"L:{r['_id']}") for r in rows]
        await show("Pick a location:", paged_keyboard(items, int(data[2:]), "l"))

    elif data.startswith("D:"):
        await send_photos(context.bot, chat_id, {"thread_id": thread_id, "date": data[2:]})

    elif data.startswith("L:"):
        await send_photos(context.bot, chat_id, {"thread_id": thread_id, "loc_id": data[2:]})

    elif data == "all":
        await send_photos(context.bot, chat_id, {"thread_id": thread_id})


# ---------- misc ----------

async def chat_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"This chat's ID is: {update.effective_chat.id}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)


class Health(BaseHTTPRequestHandler):
    """Tiny web endpoint so UptimeRobot can ping the bot (hosts like Render need a port)."""

    def _ok(self, body: bool = True) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if body:
            self.wfile.write(b"ok")

    def do_GET(self) -> None:  # noqa: N802
        self._ok()

    def do_HEAD(self) -> None:  # noqa: N802
        self._ok(body=False)

    def log_message(self, *args) -> None:
        pass


def start_health_server() -> None:
    port = os.getenv("PORT")  # set automatically by hosts like Render
    if not port:
        return
    server = HTTPServer(("0.0.0.0", int(port)), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health endpoint listening on port %s", port)


async def post_init(app: Application) -> None:
    global db
    db = AsyncMongoClient(MONGODB_URI)[DB_NAME]
    await db.families.create_index("thread_id", unique=True)
    await db.families.create_index("code", unique=True)
    await db.families.create_index("key")
    await db.photos.create_index("message_id", unique=True)
    await db.photos.create_index([("thread_id", 1), ("date", 1)])
    await db.photos.create_index([("thread_id", 1), ("loc_id", 1)])
    await db.photos.create_index("media_group_id")
    await db.users.create_index("thread_id")
    log.info("Connected to MongoDB database '%s'", DB_NAME)


def main() -> None:
    start_health_server()
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("id", chat_id_cmd))

    if GROUP_ID:
        grp = filters.Chat(chat_id=GROUP_ID)
        app.add_handler(CommandHandler("register", register, filters=grp))
        app.add_handler(CommandHandler("code", code_cmd, filters=grp))
        app.add_handler(CommandHandler("members", members, filters=grp))
        app.add_handler(CommandHandler("unlink", unlink, filters=grp))
        app.add_handler(CommandHandler("families", families, filters=grp))
        app.add_handler(CommandHandler("remove", remove_cmd, filters=grp))
        app.add_handler(MessageHandler(grp & (filters.PHOTO | filters.Document.IMAGE), on_group_media))
        app.add_handler(CallbackQueryHandler(on_remove_button, pattern=r"^rm:(y|n):\d+$"))
    else:
        log.warning("GROUP_ID is not set: send /id in your group, then put it in the environment.")

    priv = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start, filters=priv))
    app.add_handler(CommandHandler("menu", menu, filters=priv))
    app.add_handler(CommandHandler("addphotos", addphotos, filters=priv))
    app.add_handler(CommandHandler("done", done, filters=priv))
    app.add_handler(MessageHandler(priv & filters.TEXT & ~filters.COMMAND, on_private_text))
    app.add_handler(MessageHandler(priv & (filters.PHOTO | filters.Document.IMAGE), on_private_media))
    app.add_handler(
        CallbackQueryHandler(on_button, pattern=r"^(menu|all|d:\d+|l:\d+|D:\d{4}-\d{2}-\d{2}|L:[0-9a-f]{10}|L:none)$")
    )
    app.add_error_handler(on_error)

    log.info("Bot running")
    app.run_polling()


if __name__ == "__main__":
    main()
