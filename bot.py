"""Telegram family photo bot.

The idea
--------
* A private Telegram group with Topics on. Each family has one topic. The bot is an admin.
* Photos are posted in a family's topic, either as normal photos (Telegram shrinks those) or
  as Files (Telegram never compresses files, so they keep full quality). Clients get back
  whichever kind was posted. The photos stay on Telegram; MongoDB only stores an index
  (file id, kind, family, date, location) plus who is linked to which family.
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

Private chat, for you (the owner):
    /admin               your admin panel with buttons: families, add a family (the bot makes
                         the topic for you), codes, members, add/remove photos, delete a
                         family, stats. Only works for the Telegram ID in OWNER_ID.
                         Nothing about you is saved in the database.
    /addphotos           for the owner: pick a family from buttons, then send files, no
                         password. For anyone else: needs ADMIN_PASSWORD every time, then
                         the family (last name + code). /done ends it.

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
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pymongo import AsyncMongoClient
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
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
# Your own Telegram user ID (send /id to the bot in a private chat). This account gets the
# /admin panel and skips the /addphotos password. The bot stores nothing about you.
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
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

# The HTTP library logs every request URL, and Telegram's URLs contain the bot token.
# Keep those lines out of the logs so the token can't leak through them.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

db = None  # set in post_init (the Mongo client must be created inside the running loop)


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


def parse_date_answer(text: str) -> str | None:
    """'today' / 'yesterday' / '2026-07-04' -> 'YYYY-MM-DD'; None if it isn't a usable date."""
    t = text.strip().casefold()
    today = datetime.now(TZ).date()
    if t == "today":
        return today.isoformat()
    if t == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    try:
        d = date.fromisoformat(t)
    except ValueError:
        return None
    return d.isoformat() if d <= today else None


ASK_PLACE = "📍 Where were these photos taken? Type the place (for example: Central Park)."
ASK_DATE = ("📅 What date were they taken? Type it like 2026-07-04, or type today or yesterday.")


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


def is_owner(user_id: int | None) -> bool:
    """True only for the Telegram ID in OWNER_ID. Nothing about the owner is stored."""
    return bool(OWNER_ID) and user_id == OWNER_ID


async def is_group_admin(bot, chat_id: int, user_id: int) -> bool:
    if is_owner(user_id):  # the owner counts as an admin everywhere
        return True
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
    kind: str = "document",
) -> None:
    """Save one photo or file in the index with its date/location (from the caption).
    kind is "photo" (a normal Telegram photo, which Telegram compresses) or "document"
    (sent as a File, which keeps the original quality)."""
    default_day = sent_at.astimezone(TZ).date().isoformat()
    day, loc = parse_caption(caption, default_day)
    has_caption = bool((caption or "").strip())
    lid = loc_id(loc)
    now = datetime.now(timezone.utc)

    set_doc = {
        "thread_id": thread_id,
        "file_id": file_id,
        "kind": kind,
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


def once(context: ContextTypes.DEFAULT_TYPE, key: str) -> bool:
    """True the first time a key is seen (so an album of 20 files only gets one reply)."""
    seen = context.bot_data.setdefault("once", [])
    if key in seen:
        return False
    seen.append(key)
    del seen[:-300]
    return True


def media_of(msg):
    """(file_id, kind) for a message's photo or image file, or (None, None) if it has neither.
    A normal photo is kind "photo" (the largest size Telegram keeps); an image sent as a
    File is kind "document" (original quality)."""
    if msg.photo:
        return msg.photo[-1].file_id, "photo"
    doc = msg.document
    if doc and (doc.mime_type or "").startswith("image/"):
        return doc.file_id, "document"
    return None, None


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
    file_id, kind = media_of(msg)
    if not file_id:
        return
    await index_file(
        msg.message_thread_id, msg.message_id, file_id, msg.caption, msg.media_group_id, msg.date, kind
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
    context.user_data.pop("adm", None)
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
    if is_owner(user.id):  # the owner picks a family from buttons, no password
        for k in ("up", "link", "adm"):
            context.user_data.pop(k, None)
        markup, empty = await family_picker(0, "a:ph", "a:phf:{tid}")
        await update.message.reply_text(
            "No families yet. Open /admin and add one first." if empty
            else "Which family are you adding photos to?",
            reply_markup=markup,
        )
        return
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
        up.update(stage="place", thread_id=fam["thread_id"], name=fam["name"], count=0, last_mg=None,
                  until=time.time() + SESSION_SECONDS)
        await msg.reply_text(f"{fam['name']} family. {ASK_PLACE}")

    elif up["stage"] == "place":
        place = " ".join(text.split())[:80]
        if not place:
            await msg.reply_text(ASK_PLACE)
            return
        up.update(stage="date", place=place, until=time.time() + SESSION_SECONDS)
        await msg.reply_text(ASK_DATE)

    elif up["stage"] == "date":
        day = parse_date_answer(text)
        if not day:
            await msg.reply_text("I couldn't read that date. " + ASK_DATE)
            return
        up.update(stage="files", date=day, until=time.time() + SESSION_SECONDS)
        await msg.reply_text(
            f"Ready: {up['name']} family · {up['place']} · {pretty_date(day)}.\n"
            "Now send the photos (or send them as Files to keep the full quality). "
            "A photo with its own caption like “2026-07-04 | Central Park” keeps that instead. "
            "Send /done when you're finished.",
            reply_markup=one_button("✅ Done", "a:done") if is_owner(user.id) else None,
        )

    else:  # files stage but the person typed text
        await msg.reply_text("Send the photos, or /done to finish.")


async def on_private_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    up, expired = get_session(context)
    if expired:
        await msg.reply_text("That upload session timed out. Send /addphotos to start again.")
        return
    if up and up["stage"] in ("place", "date"):
        if once(context, f"early:{msg.media_group_id or msg.message_id}"):
            await msg.reply_text(
                "Not yet. I need the place and date first, so I didn't add that. "
                + (ASK_PLACE if up["stage"] == "place" else ASK_DATE) + " Then send the photos again."
            )
        return
    if not up or up["stage"] != "files":
        # Not in an upload session. For the owner that usually means the bot restarted (or the
        # session timed out), so say so instead of staying silent. Everyone else is ignored.
        if is_owner(update.effective_user.id) and once(context, f"noup:{msg.media_group_id or msg.message_id}"):
            await msg.reply_text(
                "No upload is running, so I did not add that. Open /admin → Add photos, "
                "pick the family, then send the files."
            )
        return
    file_id, kind = media_of(msg)
    if not file_id:
        if once(context, f"skip:{msg.media_group_id or msg.message_id}"):
            await msg.reply_text(
                "I only add photos and image files (JPG, PNG, HEIC and so on). That one was skipped."
            )
        return
    send = context.bot.send_photo if kind == "photo" else context.bot.send_document
    arg = {"photo": file_id} if kind == "photo" else {"document": file_id}
    # the photo's own caption wins; otherwise use the place and date I asked for
    caption = msg.caption if (msg.caption or "").strip() else f"{up['date']} | {up['place']}"
    try:
        sent = await send_with_retry(
            lambda: send(GROUP_ID, message_thread_id=up["thread_id"], caption=caption, **arg)
        )
    except (TelegramError, RuntimeError):
        log.exception("Failed copying a file into the group")
        await msg.reply_text("I couldn't add that file to the group. Check that I'm still an admin there.")
        return
    await index_file(up["thread_id"], sent.message_id, file_id, caption, msg.media_group_id, msg.date, kind)
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
    adm = context.user_data.get("adm")
    if adm and is_owner(update.effective_user.id):
        await handle_admin_text(update, context, adm)
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

    # Albums hold up to 10 items and share ONE caption, so only items with the same date,
    # location and type go in the same album (photos and files can't be mixed in one album).
    def album_key(d: dict):
        return (d["date"], d.get("loc_id"), d.get("kind", "document"))

    groups: list[list[dict]] = []
    for d in docs:
        same = groups and len(groups[-1]) < 10 and album_key(groups[-1][0]) == album_key(d)
        if same:
            groups[-1].append(d)
        else:
            groups.append([d])

    failed = 0
    for group in groups:
        first = group[0]
        caption = pretty_date(first["date"]) + (f" · {first['location']}" if first.get("location") else "")
        is_photo = first.get("kind") == "photo"
        await bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO if is_photo else ChatAction.UPLOAD_DOCUMENT)
        try:
            if len(group) == 1:
                send_one = bot.send_photo if is_photo else bot.send_document
                arg = {"photo": first["file_id"]} if is_photo else {"document": first["file_id"]}
                await send_with_retry(
                    lambda: send_one(chat_id, caption=caption, protect_content=PROTECT_CONTENT, **arg)
                )
            else:
                media_cls = InputMediaPhoto if is_photo else InputMediaDocument
                media = [
                    media_cls(media=d["file_id"], caption=caption if j == 0 else None)
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


# ---------- owner admin panel (/admin) ----------
# Only the Telegram ID in OWNER_ID can use any of this. Nothing about the owner is saved.

def _btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=cb)


def admin_home_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("📋 Families", "a:fl:0"), _btn("➕ Add family", "a:add")],
            [_btn("📤 Add photos", "a:ph:0"), _btn("🗑 Remove photos", "a:rm:0")],
            [_btn("📊 Stats", "a:stats")],
        ]
    )


def one_button(text: str, cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(text, cb)]])


def admin_pager(items: list[tuple[str, str]], page: int, nav: str, back: str) -> InlineKeyboardMarkup:
    """One button per item, with Prev/Next. `nav` is the callback prefix for the page buttons."""
    page = max(page, 0)
    chunk = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    rows = [[_btn(label[:60], cb)] for label, cb in chunk]
    navrow = []
    if page > 0:
        navrow.append(_btn("⬅️ Prev", f"{nav}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < len(items):
        navrow.append(_btn("Next ➡️", f"{nav}:{page + 1}"))
    if navrow:
        rows.append(navrow)
    rows.append([_btn("⬅️ Back", back)])
    return InlineKeyboardMarkup(rows)


async def family_picker(page: int, nav: str, pick: str):
    """Buttons listing every family. `pick` is a callback template containing {tid}.
    Returns (markup, no_families_yet)."""
    fams = await db.families.find().sort("name", 1).to_list()
    items = [(f"{f['name']} · {f['code']}", pick.format(tid=f["thread_id"])) for f in fams]
    return admin_pager(items, page, nav, "a:home"), not fams


async def family_page(tid: int):
    fam = await db.families.find_one({"thread_id": tid})
    if not fam:
        return None
    photos = await db.photos.count_documents({"thread_id": tid})
    people = await db.users.count_documents({"thread_id": tid})
    text = (
        f"🏠 {fam['name']} family\n"
        f"Code: {fam['code']}\n"
        f"Photos: {photos} · People linked: {people}"
    )
    markup = InlineKeyboardMarkup(
        [
            [_btn("📤 Add photos", f"a:phf:{tid}"), _btn("🗑 Remove photos", f"a:rmf:{tid}:0")],
            [_btn("👥 Members", f"a:mem:{tid}"), _btn("🔄 New code", f"a:nc:{tid}")],
            [_btn("❌ Delete family", f"a:df:{tid}")],
            [_btn("⬅️ Families", "a:fl:0")],
        ]
    )
    return text, markup


async def members_view(tid: int):
    fam = await db.families.find_one({"thread_id": tid})
    if not fam:
        return None
    people = await db.users.find({"thread_id": tid}).sort("first_name", 1).to_list()
    rows = [[_btn(f"Unlink {p['first_name']}"[:60], f"a:ul:{tid}:{p['_id']}")] for p in people[:50]]
    rows.append([_btn("⬅️ Back", f"a:f:{tid}")])
    if not people:
        text = f"No one has linked to the {fam['name']} family yet."
    else:
        lines = [
            f"• {p['first_name']} (id {p['_id']}" + (f", @{p['username']}" if p.get("username") else "") + ")"
            for p in people[:50]
        ]
        text = f"👥 {fam['name']} family members:\n" + "\n".join(lines)
    return text, InlineKeyboardMarkup(rows)


async def stats_text() -> str:
    fams = await db.families.find().sort("name", 1).to_list()
    cur = await db.photos.aggregate([{"$group": {"_id": "$thread_id", "n": {"$sum": 1}}}])
    photos = {d["_id"]: d["n"] for d in await cur.to_list()}
    cur = await db.users.aggregate([{"$group": {"_id": "$thread_id", "n": {"$sum": 1}}}])
    people = {d["_id"]: d["n"] for d in await cur.to_list()}
    lines = [
        f"• {f['name']}: {photos.get(f['thread_id'], 0)} photos, {people.get(f['thread_id'], 0)} people"
        for f in fams[:30]
    ]
    if len(fams) > 30:
        lines.append(f"…and {len(fams) - 30} more")
    return (
        f"📊 Stats\nFamilies: {len(fams)}\nPeople linked: {sum(people.values())}\n"
        f"Photos: {sum(photos.values())}\n\n" + "\n".join(lines)
    ).strip()


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not OWNER_ID:
        await update.message.reply_text(
            "The admin panel isn't set up yet. Send /id here to get your number, "
            "then put it in the OWNER_ID setting."
        )
        return
    if not is_owner(user.id):
        await update.message.reply_text("Sorry, the admin panel is only for the owner.")
        return
    for k in ("up", "link", "adm"):
        context.user_data.pop(k, None)
    await update.message.reply_text("👑 Admin panel", reply_markup=admin_home_markup())


async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE, adm: dict) -> None:
    """The owner typed a last name after tapping 'Add family'."""
    msg = update.message
    name = " ".join((msg.text or "").split())[:60]
    if not normalize(name):
        await msg.reply_text("Please send the family's last name, for example: Garcia")
        return
    if not GROUP_ID:
        context.user_data.pop("adm", None)
        await msg.reply_text("GROUP_ID isn't set yet, so I can't make a topic. Add it in the settings first.")
        return
    try:
        topic = await context.bot.create_forum_topic(GROUP_ID, name)
    except TelegramError as e:
        await msg.reply_text(
            f"I couldn't make the topic ({e.message}). Check that Topics are on in the group and "
            "that I'm an admin with the “Manage topics” right. Then send the name again."
        )
        return
    tid = topic.message_thread_id
    code = await new_code()
    await db.families.update_one(
        {"thread_id": tid},
        {
            "$set": {"name": name, "key": normalize(name)},
            "$setOnInsert": {"code": code, "created": datetime.now(timezone.utc)},
        },
        upsert=True,
    )
    context.user_data.pop("adm", None)
    log.info("Owner made family %s", name)
    # Also post the name and code inside the new topic, so it's there for reference.
    posted = True
    try:
        await context.bot.send_message(
            GROUP_ID, f"🏠 {name} family\nCode: {code}", message_thread_id=tid
        )
    except TelegramError:
        log.exception("Couldn't post the code in the new topic")
        posted = False
    await msg.reply_text(
        f"✅ Family made: {name}\nCode: {code}\n\n"
        "Their topic is ready in the group. Give them their last name and this code."
        + ("" if posted else "\n\n(I couldn't post the code inside the topic, but the family is saved.)"),
        reply_markup=InlineKeyboardMarkup(
            [[_btn("🏠 Open family", f"a:f:{tid}")], [_btn("⬅️ Admin panel", "a:home")]]
        ),
    )


async def on_admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("This is only for the owner.", show_alert=True)
        return
    await q.answer()
    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    async def show(text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        try:
            await q.edit_message_text(text, reply_markup=markup)
        except TelegramError:
            pass  # e.g. "message is not modified"

    async def need_family(tid: int):
        fam = await db.families.find_one({"thread_id": tid})
        if not fam:
            await show("That family no longer exists.", one_button("⬅️ Families", "a:fl:0"))
        return fam

    if action == "home":
        context.user_data.pop("adm", None)
        await show("👑 Admin panel", admin_home_markup())

    elif action in ("fl", "ph", "rm"):  # pick a family: view / add photos / remove photos
        page = int(parts[2])
        pick = {"fl": "a:f:{tid}", "ph": "a:phf:{tid}", "rm": "a:rmf:{tid}:0"}[action]
        title = {"fl": "📋 Families (tap one):", "ph": "📤 Add photos to which family?",
                 "rm": "🗑 Remove photos from which family?"}[action]
        markup, empty = await family_picker(page, f"a:{action}", pick)
        await show("No families yet. Tap “Add family” in the admin panel." if empty else title, markup)

    elif action == "f":
        view = await family_page(int(parts[2]))
        if view:
            await show(*view)
        else:
            await show("That family no longer exists.", one_button("⬅️ Families", "a:fl:0"))

    elif action == "add":
        for k in ("up", "link"):
            context.user_data.pop(k, None)
        context.user_data["adm"] = {"stage": "name"}
        await show(
            "➕ Send the family's last name (for example: Garcia). I'll make their topic in the "
            "group and give you their code.",
            one_button("Cancel", "a:home"),
        )

    elif action == "stats":
        await show(await stats_text(), one_button("⬅️ Admin panel", "a:home"))

    elif action == "phf":  # start an upload session for this family (no password for the owner)
        tid = int(parts[2])
        fam = await need_family(tid)
        if not fam:
            return
        if not GROUP_ID:
            await show("GROUP_ID isn't set yet, so I can't add photos to the group.", one_button("⬅️ Back", f"a:f:{tid}"))
            return
        context.user_data.pop("adm", None)
        context.user_data.pop("link", None)
        context.user_data["up"] = {
            "stage": "place", "thread_id": tid, "name": fam["name"], "count": 0,
            "last_mg": None, "until": time.time() + SESSION_SECONDS,
        }
        await show(f"📤 {fam['name']} family.\n{ASK_PLACE}", one_button("Cancel", "a:done"))

    elif action == "done":  # end an upload session started from the panel
        up = context.user_data.pop("up", None)
        count = up.get("count", 0) if up else 0
        back = f"a:f:{up['thread_id']}" if up else "a:home"
        await show(
            f"✅ Done. Added {count} file(s). The session is closed." if up else "No upload was running.",
            one_button("⬅️ Back", back),
        )

    elif action == "rmf":  # choose a date to delete
        tid, page = int(parts[2]), int(parts[3])
        fam = await need_family(tid)
        if not fam:
            return
        cur = await db.photos.aggregate(
            [
                {"$match": {"thread_id": tid}},
                {"$group": {"_id": "$date", "n": {"$sum": 1}}},
                {"$sort": {"_id": -1}},
            ]
        )
        rows = await cur.to_list()
        if not rows:
            await show(f"The {fam['name']} family has no photos.", one_button("⬅️ Back", f"a:f:{tid}"))
            return
        items = [(f"{pretty_date(r['_id'])} ({r['n']})", f"a:rmd:{tid}:{r['_id']}") for r in rows]
        await show(
            f"🗑 Which date should I delete for the {fam['name']} family?",
            admin_pager(items, page, f"a:rmf:{tid}", f"a:f:{tid}"),
        )

    elif action in ("rmd", "rmy"):  # confirm / do the deletion of one date
        tid, day = int(parts[2]), parts[3]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return
        fam = await need_family(tid)
        if not fam:
            return
        n = await db.photos.count_documents({"thread_id": tid, "date": day})
        if n == 0:
            await show("There are no photos left on that date.", one_button("⬅️ Back", f"a:rmf:{tid}:0"))
            return
        if action == "rmd":
            await show(
                f"Delete all {n} photo(s) from {pretty_date(day)} for the {fam['name']} family?\n\n"
                "They are deleted from the group too. This can't be undone.",
                InlineKeyboardMarkup(
                    [[_btn(f"Yes, delete {n}", f"a:rmy:{tid}:{day}")], [_btn("Cancel", f"a:rmf:{tid}:0")]]
                ),
            )
            return
        if not GROUP_ID:
            await show("GROUP_ID isn't set yet.", one_button("⬅️ Back", f"a:f:{tid}"))
            return
        docs = await db.photos.find({"thread_id": tid, "date": day}).to_list()
        ids = [d["message_id"] for d in docs]
        try:
            for i in range(0, len(ids), 100):
                await context.bot.delete_messages(GROUP_ID, ids[i : i + 100])
        except TelegramError as e:
            await show(
                f"I couldn't delete them from the group ({e.message}). Nothing was removed from the "
                "list. Check that I'm an admin with the “Delete messages” right.",
                one_button("⬅️ Back", f"a:f:{tid}"),
            )
            return
        await db.photos.delete_many({"thread_id": tid, "date": day})
        await show(f"🗑 Deleted {len(ids)} photo(s) from {pretty_date(day)}.", one_button("⬅️ Back to family", f"a:f:{tid}"))

    elif action in ("nc", "ncy"):  # new code
        tid = int(parts[2])
        fam = await need_family(tid)
        if not fam:
            return
        if action == "nc":
            await show(
                f"Make a new code for the {fam['name']} family?\n\nThe old code ({fam['code']}) stops "
                "working for new sign-ups. People who already linked stay linked.",
                InlineKeyboardMarkup([[_btn("Yes, new code", f"a:ncy:{tid}")], [_btn("Cancel", f"a:f:{tid}")]]),
            )
            return
        code = await new_code()
        await db.families.update_one({"thread_id": tid}, {"$set": {"code": code}})
        await show(f"🔄 New code for the {fam['name']} family: {code}", one_button("⬅️ Back to family", f"a:f:{tid}"))

    elif action == "mem":
        view = await members_view(int(parts[2]))
        if view:
            await show(*view)
        else:
            await show("That family no longer exists.", one_button("⬅️ Families", "a:fl:0"))

    elif action == "ul":
        tid, uid = int(parts[2]), int(parts[3])
        await db.users.delete_one({"_id": uid, "thread_id": tid})
        view = await members_view(tid)
        if view:
            await show(*view)

    elif action == "df":  # delete a family: ask first
        tid = int(parts[2])
        fam = await need_family(tid)
        if not fam:
            return
        await show(
            f"❌ Delete the {fam['name']} family?\n\nThis removes the family, its code, its linked "
            "people and its photo list from the bot. What should happen to the topic in the group?",
            InlineKeyboardMarkup(
                [
                    [_btn("Delete family, keep topic and files in the group", f"a:dfk:{tid}")],
                    [_btn("⚠️ Delete family AND the topic with ALL its files", f"a:dfd:{tid}")],
                    [_btn("Cancel", f"a:f:{tid}")],
                ]
            ),
        )

    elif action in ("dfk", "dfd"):
        tid = int(parts[2])
        fam = await need_family(tid)
        if not fam:
            return
        if action == "dfd":
            if not GROUP_ID:
                await show("GROUP_ID isn't set yet.", one_button("⬅️ Back", f"a:f:{tid}"))
                return
            try:
                await context.bot.delete_forum_topic(GROUP_ID, tid)
            except TelegramError as e:
                await show(
                    f"I couldn't delete the topic ({e.message}), so nothing was changed. Check that I'm an "
                    "admin with the “Delete messages” and “Manage topics” rights.",
                    one_button("⬅️ Back", f"a:f:{tid}"),
                )
                return
        await db.users.delete_many({"thread_id": tid})
        await db.photos.delete_many({"thread_id": tid})
        await db.families.delete_one({"thread_id": tid})
        log.info("Owner deleted family %s (topic deleted: %s)", fam["name"], action == "dfd")
        await show(
            f"✅ The {fam['name']} family was deleted"
            + (" together with its topic." if action == "dfd" else ". The topic and files are still in the group."),
            one_button("⬅️ Families", "a:fl:0"),
        )


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
    app.add_handler(CommandHandler("admin", admin_cmd, filters=priv))
    app.add_handler(CommandHandler("addphotos", addphotos, filters=priv))
    app.add_handler(CommandHandler("done", done, filters=priv))
    app.add_handler(MessageHandler(priv & filters.TEXT & ~filters.COMMAND, on_private_text))
    app.add_handler(MessageHandler(priv & (filters.PHOTO | filters.Document.ALL | filters.VIDEO), on_private_media))
    app.add_handler(CallbackQueryHandler(on_admin_button, pattern=r"^a:"))
    app.add_handler(
        CallbackQueryHandler(on_button, pattern=r"^(menu|all|d:\d+|l:\d+|D:\d{4}-\d{2}-\d{2}|L:[0-9a-f]{10}|L:none)$")
    )
    app.add_error_handler(on_error)

    log.info("Bot running")
    app.run_polling()


if __name__ == "__main__":
    main()
