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
    /announce            (private, owner only) message everyone or one family
    /connect, /unlink    (private, owner only) browse a family like its clients; leave again
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
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
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
# The name clients see when you add them yourself ("Dima has already added you to the ... family")
OWNER_NAME = (os.getenv("OWNER_NAME", "") or "The photographer").strip()[:40]
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


UNKNOWN_DATE = "0000-00-00"  # stored for photos whose date nobody knows; sorts last, shown as "No date"
CAPTION_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}|no date)\s*(?:[|,\-–]\s*)?(.*)$", re.S | re.I)


def parse_caption(caption: str | None, default_date: str) -> tuple[str, str | None]:
    """Return (date 'YYYY-MM-DD', location or None) from a caption."""
    caption = (caption or "").strip()
    day, loc = default_date, caption
    m = CAPTION_RE.match(caption)
    if m:
        try:
            day = UNKNOWN_DATE if m.group(1).casefold() == "no date" else date.fromisoformat(m.group(1)).isoformat()
            loc = m.group(2)
        except ValueError:
            loc = caption  # looked like a date but wasn't one: treat it all as location
    loc = " ".join(loc.split())[:80]
    return day, (loc or None)


def parse_date_answer(text: str) -> str | None:
    """'today' / 'yesterday' / '2026-07-04' -> 'YYYY-MM-DD'; None if it isn't a usable date."""
    t = " ".join(text.strip().casefold().replace("'", "").replace("’", "").split())
    if t in UNKNOWN_WORDS:
        return UNKNOWN_DATE
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
ASK_DATE = ("📅 What date were they taken? Type it like 2026-07-04, or type today or yesterday. "
            "If you don't know, type unknown.")


def loc_id(location: str | None) -> str:
    import hashlib

    if not location:
        return "none"
    return hashlib.md5(location.casefold().encode()).hexdigest()[:10]


def pretty_date(day: str) -> str:
    if day == UNKNOWN_DATE:
        return "No date"
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


async def send_with_retry(coro_factory, retry_timeouts: bool = False):
    """Retries when Telegram says "slow down". With retry_timeouts=True it also retries slow or
    dropped connections (safe for sends to clients; NOT used when copying into the group, because a
    timed-out send may still have gone through and a retry would post it twice)."""
    for attempt in range(5):
        try:
            return await coro_factory()
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (TimedOut, NetworkError):
            if not retry_timeouts or attempt == 4:
                raise
            await asyncio.sleep(2 + attempt * 2)
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
    if await db.families.find_one({"sug_thread_id": msg.message_thread_id}):
        return  # a family's suggestions topic: photos there are only waiting for approval
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

def menu_markup(extra: list | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        (extra or [])
        + [
            [
                InlineKeyboardButton("📅 By date", callback_data="d:0"),
                InlineKeyboardButton("📍 By location", callback_data="l:0"),
            ],
            [InlineKeyboardButton("🖼 All photos", callback_data="all")],
            [
                InlineKeyboardButton("➕ Suggest photos", callback_data="sg:new"),
                InlineKeyboardButton("💡 Family suggestions", callback_data="sg:gal"),
            ],
        ]
    )


async def my_thread(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int | None:
    """The family (topic id) this person browses: their linked family, or, for the owner only,
    the family they picked with /connect. The owner's pick lives in memory only, never in the database."""
    doc = await db.users.find_one({"_id": user_id})
    if doc:
        return doc["thread_id"]
    if is_owner(user_id):
        return context.bot_data.get("preview", {}).get(user_id)
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if await maybe_welcome(update, context):
        return
    if await my_thread(context, user.id):
        await update.message.reply_text(
            "Welcome back! What would you like to see?", reply_markup=menu_markup()
        )
        return
    lock = await locked_for(f"link:{user.id}")
    if lock:
        await update.message.reply_text(lock_text(lock))
        return
    for k in ("up", "adm", "sug"):
        context.user_data.pop(k, None)
    if not is_owner(user.id):
        await record_try(user, "started, didn't finish", keep_existing=True)
    context.user_data["link"] = {"stage": "first"}
    await update.message.reply_text("Hi! 👋 Let's get you to your photos. What's your first name?")


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/gallery and /menu: open the gallery."""
    context.user_data.pop("sug", None)
    if await maybe_welcome(update, context):
        return
    if await my_thread(context, update.effective_user.id):
        await update.message.reply_text("What would you like to see?", reply_markup=menu_markup())
    elif is_owner(update.effective_user.id):
        await update.message.reply_text("No family picked yet. Send /connect to choose one to browse.")
    else:
        await update.message.reply_text("Send /start to link your account first.")


async def connect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner only: pick a family to browse as its clients do. Kept in memory, never saved."""
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("That command isn't available. Send /start to link your account.")
        return
    for k in ("up", "link", "adm"):
        context.user_data.pop(k, None)
    markup, empty = await family_picker(0, "a:cn", "a:cnf:{tid}")
    await update.message.reply_text(
        "No families yet. Open /admin and add one first." if empty else "Which family do you want to browse?",
        reply_markup=markup,
    )


async def unlink_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """In a private chat, /unlink is for the owner only: it stops the /connect browsing."""
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("Only the photographer can unlink accounts.")
        return
    gone = context.bot_data.get("preview", {}).pop(user.id, None)
    await update.message.reply_text(
        "Disconnected. You're not browsing any family now. Send /connect to pick one." if gone
        else "You weren't connected to a family. Send /connect to pick one."
    )


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
            await record_try(user, "locked out" if locked else "wrong details", link["first"], link["last"])
            if locked and OWNER_ID:
                try:
                    await context.bot.send_message(
                        OWNER_ID,
                        f"🔒 {link['first']} {link['last']} (ID {user.id}) entered wrong details {MAX_FAILS} times "
                        "and is locked out for an hour. Open /admin → 🕵️ Login attempts to add them or clear the lock.",
                    )
                except TelegramError:
                    pass
            await msg.reply_text(
                "Those details didn't match any family."
                + (" " + lock_text(LOCK_SECONDS) if locked else " Send /start to try again.")
            )
            return
        await clear_fails(key)
        await db.tries.delete_one({"_id": user.id})
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
    sug, sug_expired = get_sug(context)
    if sug_expired:
        if once(context, f"sugexp:{msg.media_group_id or msg.message_id}"):
            await msg.reply_text("That suggestion timed out. Tap ➕ Suggest photos to start again.", reply_markup=menu_markup())
        return
    if sug:
        await handle_sug_media(update, context, sug)
        return
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
    caption = msg.caption if (msg.caption or "").strip() else (
        f"{'No date' if up['date'] == UNKNOWN_DATE else up['date']} | {up['place']}"
    )
    try:
        sent = await send_with_retry(
            lambda: send(GROUP_ID, message_thread_id=up["thread_id"], caption=caption, **arg)
        )
    except (TimedOut, NetworkError):
        log.warning("Timed out copying a file into the group")
        if once(context, f"slow:{msg.media_group_id or msg.message_id}"):
            await msg.reply_text(
                "⏳ Telegram was too slow and the connection timed out. That file may or may not "
                "have been added. Look in the family's topic, and send it again only if it's missing."
            )
        return
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
    sug, sug_expired = get_sug(context)
    if sug_expired:
        await msg.reply_text("That suggestion timed out. Tap ➕ Suggest photos to start again.", reply_markup=menu_markup())
        return
    if sug:
        await handle_sug_step(update, context, sug)
        return
    adm = context.user_data.get("adm")
    if adm and is_owner(update.effective_user.id):
        await handle_admin_text(update, context, adm)
        return
    link = context.user_data.get("link")
    if link:
        await handle_link_step(update, context, link)
        return
    if await my_thread(context, update.effective_user.id):
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
        return (d["date"], d.get("loc_id"), d.get("kind", "document"), d.get("by"))

    groups: list[list[dict]] = []
    for d in docs:
        same = groups and len(groups[-1]) < 10 and album_key(groups[-1][0]) == album_key(d)
        if same:
            groups[-1].append(d)
        else:
            groups.append([d])

    failed = 0
    sent_ids: list[int] = []
    for group in groups:
        first = group[0]
        caption = (
            pretty_date(first["date"])
            + (f" · {first['location']}" if first.get("location") else "")
            + (f" · Added by {first['by']}" if first.get("by") else "")
        )
        is_photo = first.get("kind") == "photo"
        await bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO if is_photo else ChatAction.UPLOAD_DOCUMENT)
        try:
            if len(group) == 1:
                send_one = bot.send_photo if is_photo else bot.send_document
                arg = {"photo": first["file_id"]} if is_photo else {"document": first["file_id"]}
                res = await send_with_retry(
                    lambda: send_one(chat_id, caption=caption, protect_content=PROTECT_CONTENT, **arg),
                    retry_timeouts=True,
                )
                sent_ids.append(res.message_id)
            else:
                media_cls = InputMediaPhoto if is_photo else InputMediaDocument
                media = [
                    media_cls(media=d["file_id"], caption=caption if j == 0 else None)
                    for j, d in enumerate(group)
                ]
                res = await send_with_retry(
                    lambda: bot.send_media_group(chat_id, media, protect_content=PROTECT_CONTENT),
                    retry_timeouts=True,
                )
                sent_ids.extend(m.message_id for m in (res or []))
        except (TelegramError, RuntimeError):
            log.exception("Failed sending a batch")
            failed += len(group)
        await asyncio.sleep(1.2)  # stay under Telegram rate limits

    note = ""
    if truncated:
        note += f"\n\nShowing the first {MAX_SEND}. Use “By date” or “By location” to see the rest."
    if failed:
        note += f"\n\n{failed} file(s) couldn't be sent. Please tell the photographer."
    last = await bot.send_message(chat_id, "That's everything! 📸" + note + "\n\nAnything else?", reply_markup=menu_markup())
    extra = []
    if sent_ids:
        # remember which messages were photos so the 🧹 button can remove them from the chat
        await db.sent.update_one(
            {"_id": f"{chat_id}:{last.message_id}"},
            {"$set": {"ids": sent_ids, "at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        extra = [[InlineKeyboardButton("🧹 Hide these photos", callback_data=f"x:{last.message_id}")]]
        try:
            await bot.edit_message_reply_markup(chat_id, last.message_id, reply_markup=menu_markup(extra))
        except TelegramError:
            pass


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    thread_id = await my_thread(context, q.from_user.id)
    if not thread_id:
        await q.message.reply_text("Please send /start to link your account first.")
        return
    data = q.data
    chat_id = q.message.chat_id

    async def show(text: str, markup: InlineKeyboardMarkup) -> None:
        try:
            await q.edit_message_text(text, reply_markup=markup)
        except TelegramError:
            pass  # e.g. "message is not modified"

    if data == "menu":
        context.user_data.pop("sug", None)
        await show("What would you like to see?", menu_markup())

    elif data == "sg:new":  # start suggesting photos
        for k in ("up", "adm", "link"):
            context.user_data.pop(k, None)
        context.user_data["sug"] = {
            "stage": "place", "items": [], "thread_id": thread_id, "until": time.time() + SESSION_SECONDS,
        }
        await show("💡 Suggest photos for your family's gallery.\n\n" + ASK_SUG_PLACE, one_button("Cancel", "menu"))

    elif data == "sg:send":  # send the suggestion to the photographer for approval
        sug, expired = get_sug(context)
        if not sug or sug["stage"] != "files" or not sug["items"]:
            if not sug:
                note = "That suggestion timed out. Tap ➕ Suggest photos to start again."
            elif sug["stage"] != "files":
                note = "Please answer my question first, then send the photos."
            else:
                note = "Nothing to send yet. Send at least one photo first."
            await q.message.reply_text(note, reply_markup=menu_markup() if not sug else sug_files_markup())
            return
        user_doc = await db.users.find_one({"_id": q.from_user.id})
        problem = await submit_suggestion(context, q.from_user, user_doc, sug)
        if problem:
            await show(problem, sug_files_markup())
            return
        context.user_data.pop("sug", None)
        await show(
            f"✅ Sent {len(sug['items'])} photo(s) to the photographer for approval. "
            "I'll message you when they're added.",
            menu_markup(),
        )

    elif data == "sg:gal":  # the family-suggestions gallery
        await send_photos(context.bot, chat_id, {"thread_id": thread_id, "sug": True})

    elif data.startswith("x:"):  # 🧹 hide the photos I sent into this chat
        doc = await db.sent.find_one({"_id": f"{chat_id}:{data[2:]}"})
        ids = doc["ids"] if doc else []
        deleted = False
        for i in range(0, len(ids), 100):
            try:
                await context.bot.delete_messages(chat_id, ids[i : i + 100])
                deleted = True
            except TelegramError:
                log.warning("Couldn't delete some messages while hiding photos")
        await db.sent.delete_one({"_id": f"{chat_id}:{data[2:]}"})
        await show(
            "🧹 Photos hidden. Open the gallery again whenever you like." if deleted
            else "Those photos are too old for me to hide now. You can delete them by hand.",
            menu_markup(),
        )

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


# ---------- photo suggestions: clients suggest, the owner approves ----------

SUG_MAX_ITEMS = 30
UNKNOWN_WORDS = {"unknown", "no date", "nodate", "idk", "dont know", "i dont know", "dunno", "not sure", "skip"}
ASK_SUG_PLACE = "📍 Where were they taken? Type the place, or type unknown if you don't know."
ASK_SUG_OTHER = (
    "👪 Are other families in these photos too? Type their last names separated by commas "
    "(for example: Novak, Smith), or type no."
)


def clean_words(text: str) -> str:
    return " ".join(text.strip().casefold().replace("'", "").replace("’", "").split())


def sug_name(user_doc, user) -> str:
    return (user_doc or {}).get("first_name") or getattr(user, "first_name", None) or "Guest"


def get_sug(context: ContextTypes.DEFAULT_TYPE):
    """Returns (suggestion-in-progress or None, expired?). Lives in memory only."""
    sug = context.user_data.get("sug")
    if sug and sug["until"] < time.time():
        context.user_data.pop("sug", None)
        return None, True
    return sug, False


def sug_buttons(has_others: bool, sid: str) -> InlineKeyboardMarkup:
    rows = []
    if has_others:
        rows.append([InlineKeyboardButton("✅ Approve for all families listed", callback_data=f"sa:y:{sid}")])
        rows.append([InlineKeyboardButton("✅ Approve for their own family only", callback_data=f"sa:o:{sid}")])
    else:
        rows.append([InlineKeyboardButton("✅ Approve", callback_data=f"sa:y:{sid}")])
    rows.append([InlineKeyboardButton("❌ Reject", callback_data=f"sa:n:{sid}")])
    return InlineKeyboardMarkup(rows)


def sug_files_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Send for approval", callback_data="sg:send")],
            [InlineKeyboardButton("Cancel", callback_data="menu")],
        ]
    )


def kind_chunks(items: list[dict]) -> list[list[dict]]:
    """Groups of up to 10 of the same type (photos and files can't share an album)."""
    out: list[list[dict]] = []
    for it in items:
        if out and len(out[-1]) < 10 and out[-1][0]["kind"] == it["kind"]:
            out[-1].append(it)
        else:
            out.append([it])
    return out


async def handle_sug_step(update: Update, context: ContextTypes.DEFAULT_TYPE, sug: dict) -> None:
    """The client typed text while suggesting photos."""
    msg = update.message
    text = (msg.text or "").strip()
    stage = sug["stage"]
    sug["until"] = time.time() + SESSION_SECONDS

    if stage == "place":
        if not text:
            await msg.reply_text(ASK_SUG_PLACE)
            return
        sug["place"] = None if clean_words(text) in UNKNOWN_WORDS else " ".join(text.split())[:80]
        sug["stage"] = "date"
        await msg.reply_text(ASK_DATE)

    elif stage == "date":
        day = parse_date_answer(text)
        if not day:
            await msg.reply_text("I couldn't read that date. " + ASK_DATE)
            return
        sug["date"] = day
        sug["stage"] = "other"
        await msg.reply_text(ASK_SUG_OTHER)

    elif stage == "other":
        others, unmatched = [], []
        if clean_words(text) not in ("no", "none", "nope", "n", "nobody", "-"):
            own = await db.families.find_one({"thread_id": sug["thread_id"]})
            for raw in re.split(r"[,\n;]| and ", text):
                name = " ".join(raw.split())
                if not normalize(name):
                    continue
                found = await db.families.find({"key": normalize(name)}).to_list()
                if len(found) == 1 and found[0]["thread_id"] != sug["thread_id"] and own:
                    if found[0]["thread_id"] not in others:
                        others.append(found[0]["thread_id"])
                elif len(found) == 1:
                    continue  # that's their own family, nothing to add
                else:
                    unmatched.append(name)
        sug.update(others=others, unmatched=unmatched, stage="files")
        typed = ", ".join(" ".join(r.split()) for r in re.split(r"[,\n;]| and ", text) if normalize(r))
        # Don't confirm which families exist: the photographer sees the matches when approving.
        note = f"Noted other families: {typed}.\n" if others or unmatched else ""
        await msg.reply_text(
            note + "Now send your photos (or send them as Files to keep the full quality). "
            "Then tap Send for approval.",
            reply_markup=sug_files_markup(),
        )

    else:  # files stage but they typed text
        await msg.reply_text("Send your photos, then tap Send for approval.", reply_markup=sug_files_markup())


async def handle_sug_media(update: Update, context: ContextTypes.DEFAULT_TYPE, sug: dict) -> None:
    msg = update.message
    sug["until"] = time.time() + SESSION_SECONDS
    key = msg.media_group_id or msg.message_id
    if sug["stage"] != "files":
        if once(context, f"sugearly:{key}"):
            await msg.reply_text("Not yet. Please answer my question first, then send the photos again.")
        return
    file_id, kind = media_of(msg)
    if not file_id:
        if once(context, f"sugskip:{key}"):
            await msg.reply_text("I only take photos and image files. That one was skipped.")
        return
    if len(sug["items"]) >= SUG_MAX_ITEMS:
        if once(context, f"sugmax:{key}"):
            await msg.reply_text(
                f"That's the most I can take in one go ({SUG_MAX_ITEMS}). Tap Send for approval.",
                reply_markup=sug_files_markup(),
            )
        return
    sug["items"].append({"file_id": file_id, "kind": kind})
    if once(context, f"sugok:{key}"):
        await msg.reply_text("📥 Got it. Send more, or tap Send for approval.", reply_markup=sug_files_markup())


async def ensure_sug_topic(bot, fam: dict, force_new: bool = False) -> int:
    """The family's 💡 suggestions topic in the group (made the first time it's needed)."""
    if fam.get("sug_thread_id") and not force_new:
        return fam["sug_thread_id"]
    topic = await bot.create_forum_topic(GROUP_ID, f"💡 {fam['name']} suggestions")
    await db.families.update_one(
        {"thread_id": fam["thread_id"]}, {"$set": {"sug_thread_id": topic.message_thread_id}}
    )
    fam["sug_thread_id"] = topic.message_thread_id
    return topic.message_thread_id


async def post_items(bot, tid: int, items: list[dict], ids: list[int]) -> None:
    """Posts photos/files into a topic (albums of up to 10). The new message ids are added to `ids`
    as they go, so the caller can clean up if something fails halfway."""
    for chunk in kind_chunks(items):
        is_photo = chunk[0]["kind"] == "photo"
        if len(chunk) == 1:
            send_one = bot.send_photo if is_photo else bot.send_document
            arg = {"photo": chunk[0]["file_id"]} if is_photo else {"document": chunk[0]["file_id"]}
            res = await send_with_retry(lambda: send_one(GROUP_ID, message_thread_id=tid, **arg))
            ids.append(res.message_id)
        else:
            cls = InputMediaPhoto if is_photo else InputMediaDocument
            media = [cls(media=d["file_id"]) for d in chunk]
            res = await send_with_retry(lambda: bot.send_media_group(GROUP_ID, media, message_thread_id=tid))
            ids.extend(m.message_id for m in (res or []))
        await asyncio.sleep(1.2)


async def delete_ids(bot, ids: list[int]) -> bool:
    ok = True
    for i in range(0, len(ids), 100):
        try:
            await bot.delete_messages(GROUP_ID, ids[i : i + 100])
        except TelegramError:
            ok = False
    return ok


async def submit_suggestion(context: ContextTypes.DEFAULT_TYPE, user, user_doc, sug: dict) -> str | None:
    """Posts the suggestion to the family's suggestions topic with Approve/Reject buttons.
    Returns an error text for the client, or None when it worked."""
    bot = context.bot
    fam = await db.families.find_one({"thread_id": sug["thread_id"]})
    if not fam:
        return "That family no longer exists."
    if not GROUP_ID:
        return "Suggestions aren't set up yet. Please tell the photographer."
    name = sug_name(user_doc, user)
    sid = secrets.token_hex(5)
    board: list[int] = []
    sug_tid = None
    for attempt in (1, 2):
        try:
            sug_tid = await ensure_sug_topic(bot, fam, force_new=attempt == 2)
            await post_items(bot, sug_tid, sug["items"], board)
            break
        except (TimedOut, NetworkError):
            log.warning("Timed out posting a suggestion")
            await delete_ids(bot, board)
            board = []
            return "Telegram was too slow. Please tap Send for approval again in a moment."
        except (TelegramError, RuntimeError) as e:
            log.exception("Couldn't post a suggestion")
            await delete_ids(bot, board)
            board = []
            if attempt == 1 and "thread" in (getattr(e, "message", "") or str(e)).lower():
                continue  # the topic was deleted by hand: make a new one and retry once
            return "I couldn't send that to the photographer. Please try again later."
    others = await db.families.find({"thread_id": {"$in": sug.get("others", [])}}).to_list()
    place, day = sug.get("place"), sug["date"]
    lines = [
        f"💡 Photo suggestion from {name} ({fam['name']} family)",
        f"Photos: {len(sug['items'])}",
        f"Place: {place or 'unknown'}",
        f"Date: {pretty_date(day)}",
    ]
    if others:
        lines.append("Other families in the photos: " + ", ".join(f["name"] for f in others))
    if sug.get("unmatched"):
        lines.append("Names I couldn't match: " + ", ".join(sug["unmatched"]))
    try:
        prompt = await bot.send_message(
            GROUP_ID, "\n".join(lines), message_thread_id=sug_tid,
            reply_markup=sug_buttons(bool(others), sid),
        )
    except TelegramError:
        log.exception("Couldn't post the approval buttons")
        await delete_ids(bot, board)
        return "I couldn't send that to the photographer. Please try again later."
    await db.suggestions.update_one(
        {"_id": sid},
        {"$set": {
            "status": "pending", "from_id": user.id, "from_name": name, "thread_id": sug["thread_id"],
            "others": [f["thread_id"] for f in others], "place": place, "date": day,
            "items": sug["items"], "board_ids": board, "prompt_id": prompt.message_id,
            "done": [], "created": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    if OWNER_ID:
        try:
            await bot.send_message(
                OWNER_ID,
                f"💡 {name} ({fam['name']} family) suggested {len(sug['items'])} photo(s). "
                f"Open the “{fam['name']} suggestions” topic in the group to approve or reject.",
            )
        except TelegramError:
            pass  # the owner may not have opened the bot yet; the topic still has the buttons
    return None


async def on_suggestion_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner taps Approve / Reject under a suggestion in the group."""
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Only the photographer can do this.", show_alert=True)
        return
    await q.answer()
    _, action, sid = q.data.split(":")
    bot = context.bot
    doc = await db.suggestions.find_one({"_id": sid})

    async def edit(text: str, markup=None) -> None:
        try:
            await q.edit_message_text(text, reply_markup=markup)
        except TelegramError:
            pass

    if not doc or doc.get("status") != "pending":
        await edit("This suggestion was already handled.")
        return
    name = doc["from_name"]

    async def tell_client(text: str) -> None:
        try:
            await bot.send_message(doc["from_id"], text)
        except TelegramError:
            pass  # they may have blocked the bot

    if action == "n":
        await delete_ids(bot, doc.get("board_ids", []))
        await db.suggestions.update_one({"_id": sid}, {"$set": {"status": "rejected"}})
        await edit(f"❌ Rejected: {len(doc['items'])} photo(s) from {name}.")
        await tell_client("Thanks for the suggestion! The photographer didn't add those photos this time.")
        return

    targets = [doc["thread_id"]] + (doc.get("others", []) if action == "y" else [])
    done = set(doc.get("done", []))
    day, place = doc["date"], doc.get("place")
    caption = pretty_date(day) + (f" · {place}" if place else "") + f" · Added by {name}"
    chunks = kind_chunks(doc["items"])  # albums of up to 10 keep Telegram's per-minute limit happy
    added_to, failed, timed_out = [], False, False
    for tid in targets:
        fam = await db.families.find_one({"thread_id": tid})
        if not fam:
            continue
        for ci, chunk in enumerate(chunks):
            key = f"{tid}:{ci}"
            if key in done:
                continue
            is_photo = chunk[0]["kind"] == "photo"
            try:
                if len(chunk) == 1:
                    send_one = bot.send_photo if is_photo else bot.send_document
                    arg = {"photo": chunk[0]["file_id"]} if is_photo else {"document": chunk[0]["file_id"]}
                    res = await send_with_retry(
                        lambda: send_one(GROUP_ID, message_thread_id=tid, caption=caption, **arg)
                    )
                    sent_msgs = [res]
                else:
                    cls = InputMediaPhoto if is_photo else InputMediaDocument
                    media = [
                        cls(media=d["file_id"], caption=caption if j == 0 else None)
                        for j, d in enumerate(chunk)
                    ]
                    res = await send_with_retry(
                        lambda: bot.send_media_group(GROUP_ID, media, message_thread_id=tid)
                    )
                    sent_msgs = list(res or [])
            except (TimedOut, NetworkError):
                log.warning("Timed out adding a suggested photo")
                failed = timed_out = True
                break
            except (TelegramError, RuntimeError):
                log.exception("Couldn't add a suggested photo")
                failed = True
                break
            for it, sm in zip(chunk, sent_msgs):
                await db.photos.update_one(
                    {"message_id": sm.message_id},
                    {
                        "$set": {
                            "thread_id": tid, "file_id": it["file_id"], "kind": it["kind"],
                            "media_group_id": None, "date": day, "location": place, "loc_id": loc_id(place),
                            "has_caption": True, "by": name, "by_id": doc["from_id"], "sug": True,
                        },
                        "$setOnInsert": {"added": datetime.now(timezone.utc)},
                    },
                    upsert=True,
                )
            done.add(key)
            await db.suggestions.update_one({"_id": sid}, {"$set": {"done": sorted(done)}})
        if failed:
            break
        added_to.append(fam["name"])
    if failed:
        await edit(
            f"⚠️ I couldn't add everything from {name}. Nothing was deleted from this topic."
            + (" Telegram timed out, so the last batch may have gone through: check the family topic for "
               "a doubled batch after you retry." if timed_out else "")
            + "\nTap the same button again to continue where I stopped.",
            sug_buttons(bool(doc.get("others")), sid),
        )
        return
    await delete_ids(bot, doc.get("board_ids", []))
    await db.suggestions.update_one({"_id": sid}, {"$set": {"status": "approved"}})
    await edit(
        f"✅ Approved: {len(doc['items'])} photo(s) from {name}, added to "
        + (", ".join(added_to) or "no family")
        + "."
    )
    await tell_client("🎉 Your suggested photos were added to the gallery. Thank you!")


# ---------- announcements (owner only) ----------

ANN_MAX = 3500


async def ann_recipients(aud: str) -> list[int]:
    q = {} if aud == "all" else {"thread_id": int(aud)}
    return [u["_id"] for u in await db.users.find(q).to_list()]


async def ann_audience_name(aud: str) -> str:
    if aud == "all":
        return "everyone"
    fam = await db.families.find_one({"thread_id": int(aud)})
    return f"the {fam['name']} family" if fam else "that family"


async def ann_picker(page: int):
    fams = await db.families.find().sort("name", 1).to_list()
    items = [("📣 Everyone (all families)", "a:ana:all")] + [(f"{f['name']}", f"a:ana:{f['thread_id']}") for f in fams]
    return admin_pager(items, page, "a:an", "a:home")


async def handle_announce_text(update: Update, context: ContextTypes.DEFAULT_TYPE, adm: dict) -> None:
    msg = update.message
    text = (msg.text or "").strip()
    if not text:
        await msg.reply_text("Please type the announcement.")
        return
    if len(text) > ANN_MAX:
        await msg.reply_text(f"That's too long ({len(text)} characters). Please keep it under {ANN_MAX}.")
        return
    people = await ann_recipients(adm["aud"])
    adm.update(stage="ann_ok", text=text)
    await msg.reply_text(
        f"📣 Preview (this goes to {len(people)} {'person' if len(people) == 1 else 'people'} in "
        f"{await ann_audience_name(adm['aud'])}):\n\n{text}",
        reply_markup=InlineKeyboardMarkup(
            [
                [_btn(f"✅ Send to {len(people)}", "a:ans")],
                [_btn("✏️ Rewrite", f"a:ana:{adm['aud']}"), _btn("Cancel", "a:home")],
            ]
        ),
    )


async def announce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("That command isn't available.")
        return
    for k in ("up", "link", "adm", "sug"):
        context.user_data.pop(k, None)
    await update.message.reply_text("📣 Who should get the announcement?", reply_markup=await ann_picker(0))


# ---------- login attempts and adding people yourself (owner) ----------

def fmt_when(dt) -> str:
    """MongoDB hands back naive UTC times; show them in your timezone."""
    if dt is None:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).strftime("%b %d, %H:%M")


async def record_try(user, reason: str, first: str | None = None, last: str | None = None,
                     keep_existing: bool = False) -> None:
    """Remember that this person tried to get in (so the owner can see them and add them).
    Never stores the code they typed."""
    if keep_existing and await db.tries.find_one({"_id": user.id}):
        return
    await db.tries.update_one(
        {"_id": user.id},
        {
            "$set": {
                "username": getattr(user, "username", None),
                "tg_name": getattr(user, "first_name", None),
                "reason": reason, "first": first, "last": last, "at": datetime.now(timezone.utc),
            },
            "$inc": {"count": 1},
        },
        upsert=True,
    )


def try_label(t: dict) -> str:
    who = " ".join(x for x in (t.get("first"), t.get("last")) if x) or t.get("tg_name") or str(t["_id"])
    return f"{who} · {t.get('reason', '?')}"


async def add_person(bot, uid: int, tid: int, first_name: str) -> tuple[str, bool]:
    """Links a Telegram account to a family on the owner's say-so.
    Returns (problem or '', whether the person could be messaged right away)."""
    fam = await db.families.find_one({"thread_id": tid})
    if not fam:
        return "That family no longer exists.", False
    existing = await db.users.find_one({"_id": uid})
    if existing:
        other = await db.families.find_one({"thread_id": existing["thread_id"]})
        where = f"the {other['name']} family" if other else "another family"
        if existing["thread_id"] == tid:
            return f"They're already in the {fam['name']} family.", False
        return f"They're already linked to {where}. Unlink them there first.", False
    await db.users.update_one(
        {"_id": uid},
        {"$set": {
            "thread_id": tid, "first_name": first_name, "username": None,
            "linked_at": datetime.now(timezone.utc), "added_by_owner": True, "welcomed": False,
        }},
        upsert=True,
    )
    await db.tries.delete_one({"_id": uid})
    await clear_fails(f"link:{uid}")
    log.info("Owner added %s (%s) to family %s", first_name, uid, fam["name"])
    try:
        await bot.send_message(uid, welcome_text(fam["name"]), reply_markup=menu_markup())
        await db.users.update_one({"_id": uid}, {"$set": {"welcomed": True}})
        return "", True
    except TelegramError:
        return "", False  # they haven't opened the bot yet; they'll get the welcome when they do


def welcome_text(family_name: str) -> str:
    return f"🎉 {OWNER_NAME} has already added you to the {family_name} family. Here are your photos!"


async def maybe_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """The first time an owner-added person opens the bot, tell them they're already in."""
    uid = update.effective_user.id
    doc = await db.users.find_one({"_id": uid})
    if not doc or not doc.get("added_by_owner") or doc.get("welcomed"):
        return False
    fam = await db.families.find_one({"thread_id": doc["thread_id"]})
    await db.users.update_one({"_id": uid}, {"$set": {"welcomed": True}})
    await update.effective_message.reply_text(
        welcome_text(fam["name"] if fam else "your"), reply_markup=menu_markup()
    )
    return True


async def tries_view(page: int):
    rows = await db.tries.find().sort("at", -1).to_list()
    if not rows:
        return "🕵️ Nobody has tried to log in recently.", one_button("⬅️ Admin panel", "a:home")
    items = [(try_label(t), f"a:trp:{t['_id']}") for t in rows]
    markup = admin_pager(items, page, "a:tr", "a:home")
    markup.inline_keyboard.insert(-1, [_btn(f"🗑 Clear the whole list ({len(rows)})", "a:trx")])
    return "🕵️ Recent login attempts (tap one):", markup


async def try_detail(uid: int):
    t = await db.tries.find_one({"_id": uid})
    if not t:
        return "That attempt is gone (they may already be added).", one_button("⬅️ Attempts", "a:tr:0")
    lock = await locked_for(f"link:{uid}")
    guess = None
    if t.get("last"):
        found = await db.families.find({"key": normalize(t["last"])}).to_list()
        if len(found) == 1:
            guess = found[0]
    lines = [
        "🕵️ Login attempt",
        f"Telegram name: {t.get('tg_name') or '?'}" + (f" (@{t['username']})" if t.get("username") else ""),
        f"Telegram ID: {uid}",
        f"They typed: first name “{t.get('first') or '-'}”, family “{t.get('last') or '-'}”",
        f"Result: {t.get('reason', '?')} · {t.get('count', 1)} time(s) · last {fmt_when(t.get('at'))}",
        f"Locked out: {'yes, about ' + str(max(1, -(-lock // 60))) + ' more minute(s)' if lock else 'no'}",
    ]
    rows = []
    if guess:
        rows.append([_btn(f"✅ Add to the {guess['name']} family", f"a:tra:{uid}:{guess['thread_id']}")])
    rows.append([_btn("➕ Add to another family…", f"a:trf:{uid}:0")])
    if lock:
        rows.append([_btn("🔓 Clear the lockout", f"a:trl:{uid}")])
    rows.append([_btn("🗑 Dismiss", f"a:trd:{uid}"), _btn("⬅️ Attempts", "a:tr:0")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def msg_reply_pick_family(update: Update) -> None:
    await update.message.reply_text("Please tap a family above, or Cancel from the admin panel (/admin).")


async def handle_newperson_text(update: Update, context: ContextTypes.DEFAULT_TYPE, adm: dict) -> None:
    """From the main panel: the owner typed a Telegram ID and a first name; next they pick the family."""
    msg = update.message
    parts = (msg.text or "").split(None, 1)
    if not parts or not parts[0].isdigit() or not (5 <= len(parts[0]) <= 15):
        await msg.reply_text(
            "Please send their Telegram ID (a number) and their first name, like: 123456789 Maria\n"
            "They can get their ID by sending /id to this bot in a private chat."
        )
        return
    name = " ".join(parts[1].split())[:40] if len(parts) > 1 else ""
    if not name:
        await msg.reply_text("I also need their first name after the ID, like: 123456789 Maria")
        return
    adm.update(stage="np_fam", uid=int(parts[0]), name=name)
    markup, empty = await family_picker(0, "a:npf", "a:npa:{tid}")
    if empty:
        context.user_data.pop("adm", None)
        await msg.reply_text("No families yet. Add a family first.", reply_markup=one_button("⬅️ Admin panel", "a:home"))
        return
    await msg.reply_text(f"Which family should {name} join?", reply_markup=markup)


async def handle_addperson_text(update: Update, context: ContextTypes.DEFAULT_TYPE, adm: dict) -> None:
    """The owner typed a Telegram ID (and maybe a first name) to add to a family."""
    msg = update.message
    parts = (msg.text or "").split(None, 1)
    if not parts or not parts[0].isdigit() or not (5 <= len(parts[0]) <= 15):
        await msg.reply_text(
            "Please send their Telegram ID (a number like 123456789), then optionally their first name: "
            "123456789 Maria\nThey can get their ID by sending /id to this bot in a private chat."
        )
        return
    uid = int(parts[0])
    name = " ".join(parts[1].split())[:40] if len(parts) > 1 else ""
    if not name:
        t = await db.tries.find_one({"_id": uid})
        name = (t or {}).get("first") or (t or {}).get("tg_name") or "Friend"
    problem, messaged = await add_person(context.bot, uid, adm["tid"], name)
    if problem:
        await msg.reply_text(problem + " Send another ID, or tap Cancel.")
        return
    context.user_data.pop("adm", None)
    fam = await db.families.find_one({"thread_id": adm["tid"]})
    await msg.reply_text(
        f"✅ Added {name} to the {fam['name']} family. "
        + ("They were told in the bot." if messaged
           else "I couldn't message them yet; they'll see the welcome the first time they open the bot."),
        reply_markup=InlineKeyboardMarkup([[_btn("🏠 Family", f"a:f:{adm['tid']}")], [_btn("⬅️ Admin panel", "a:home")]]),
    )


# ---------- owner admin panel (/admin) ----------
# Only the Telegram ID in OWNER_ID can use any of this. Nothing about the owner is saved.

def _btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=cb)


def admin_home_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn("📋 Families", "a:fl:0"), _btn("➕ Add family", "a:add")],
            [_btn("📤 Add photos", "a:ph:0"), _btn("🗑 Remove photos", "a:rm:0")],
            [_btn("➕ Add person", "a:np"), _btn("🕵️ Login attempts", "a:tr:0")],
            [_btn("📣 Announce", "a:an:0")],
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
            [_btn("➕ Add person by ID", f"a:ap:{tid}")],
            [_btn("👁 Browse as this family", f"a:cnf:{tid}"), _btn("❌ Delete family", f"a:df:{tid}")],
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
    """The owner typed text: an announcement, or a last name after tapping 'Add family'."""
    if adm.get("stage") in ("ann", "ann_ok"):
        await handle_announce_text(update, context, adm)
        return
    if adm.get("stage") == "addp":
        await handle_addperson_text(update, context, adm)
        return
    if adm.get("stage") == "np":
        await handle_newperson_text(update, context, adm)
        return
    if adm.get("stage") == "np_fam":
        await msg_reply_pick_family(update)
        return
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

    elif action == "tr":  # who tried to log in
        context.user_data.pop("adm", None)
        await show(*await tries_view(int(parts[2])))

    elif action == "trp":
        await show(*await try_detail(int(parts[2])))

    elif action == "trf":  # pick the family to add them to
        uid, page = int(parts[2]), int(parts[3])
        markup, empty = await family_picker(page, f"a:trf:{uid}", "a:tra:" + str(uid) + ":{tid}")
        await show("No families yet." if empty else "Add them to which family?", markup)

    elif action == "tra":  # add that person
        uid, tid = int(parts[2]), int(parts[3])
        t = await db.tries.find_one({"_id": uid})
        name = (t or {}).get("first") or (t or {}).get("tg_name") or "Friend"
        problem, messaged = await add_person(context.bot, uid, tid, name)
        fam = await db.families.find_one({"thread_id": tid})
        if problem:
            await show(problem, one_button("⬅️ Attempts", "a:tr:0"))
            return
        await show(
            f"✅ Added {name} to the {fam['name']} family. "
            + ("They were told in the bot." if messaged
               else "I couldn't message them yet; they'll see the welcome the first time they open the bot."),
            InlineKeyboardMarkup([[_btn("🕵️ More attempts", "a:tr:0")], [_btn("⬅️ Admin panel", "a:home")]]),
        )

    elif action == "trl":  # clear a lockout
        uid = int(parts[2])
        await clear_fails(f"link:{uid}")
        await show("🔓 The lockout is cleared. They can try again with /start.", one_button("⬅️ Back", f"a:trp:{uid}"))

    elif action == "trx":  # clear the whole list (just the list: nobody is unlinked or unlocked)
        await db.tries.delete_many({})
        await show(*await tries_view(0))

    elif action == "trd":  # dismiss an attempt from the list
        await db.tries.delete_one({"_id": int(parts[2])})
        await show(*await tries_view(0))

    elif action == "np":  # add a person from the main panel: ID + first name, then family
        for k in ("up", "link", "sug"):
            context.user_data.pop(k, None)
        context.user_data["adm"] = {"stage": "np"}
        await show(
            "➕ Add a person to a family.\n\nSend their Telegram ID and their first name, like: "
            "123456789 Maria\n\nThey can get their ID by sending /id to this bot in a private chat. "
            "Next you'll pick their family.",
            one_button("Cancel", "a:home"),
        )

    elif action == "npf":  # page through the family list
        adm = context.user_data.get("adm")
        if not adm or adm.get("stage") != "np_fam":
            await show("Start again from the admin panel.", one_button("⬅️ Admin panel", "a:home"))
            return
        markup, empty = await family_picker(int(parts[2]), "a:npf", "a:npa:{tid}")
        await show(f"Which family should {adm['name']} join?", markup)

    elif action == "npa":  # family picked: add the person
        adm = context.user_data.get("adm")
        if not adm or adm.get("stage") != "np_fam":
            await show("Start again from the admin panel.", one_button("⬅️ Admin panel", "a:home"))
            return
        tid = int(parts[2])
        problem, messaged = await add_person(context.bot, adm["uid"], tid, adm["name"])
        fam = await db.families.find_one({"thread_id": tid})
        if problem:
            await show(problem, one_button("⬅️ Admin panel", "a:home"))
            context.user_data.pop("adm", None)
            return
        context.user_data.pop("adm", None)
        await show(
            f"✅ Added {adm['name']} to the {fam['name']} family. "
            + ("They were told in the bot." if messaged
               else "I couldn't message them yet; they'll see the welcome the first time they open the bot."),
            InlineKeyboardMarkup([[_btn("➕ Add another", "a:np")], [_btn("⬅️ Admin panel", "a:home")]]),
        )

    elif action == "ap":  # add a person by Telegram ID
        tid = int(parts[2])
        fam = await need_family(tid)
        if not fam:
            return
        for k in ("up", "link", "sug"):
            context.user_data.pop(k, None)
        context.user_data["adm"] = {"stage": "addp", "tid": tid}
        await show(
            f"➕ Add someone to the {fam['name']} family.\n\nSend their Telegram ID (a number), and "
            "optionally their first name, like: 123456789 Maria\n\nThey can get their ID by sending /id to "
            "this bot in a private chat. When they open the bot they'll be told you added them.",
            one_button("Cancel", f"a:f:{tid}"),
        )

    elif action == "an":  # announcement: who gets it?
        context.user_data.pop("adm", None)
        await show("📣 Who should get the announcement?", await ann_picker(int(parts[2])))

    elif action == "ana":  # audience picked: ask for the text
        aud = parts[2]
        if aud != "all" and not await need_family(int(aud)):
            return
        people = await ann_recipients(aud)
        if not people:
            await show(
                f"Nobody is linked in {await ann_audience_name(aud)} yet, so there's no one to message.",
                one_button("⬅️ Back", "a:an:0"),
            )
            return
        for k in ("up", "link", "sug"):
            context.user_data.pop(k, None)
        context.user_data["adm"] = {"stage": "ann", "aud": aud}
        await show(
            f"📣 Type your announcement. It goes to {len(people)} {'person' if len(people) == 1 else 'people'} "
            f"in {await ann_audience_name(aud)} as a private message from the bot. I'll show a preview first.",
            one_button("Cancel", "a:home"),
        )

    elif action == "ans":  # send it
        adm = context.user_data.get("adm")
        if not adm or adm.get("stage") != "ann_ok":
            await show("There's nothing to send. Start again from the admin panel.", one_button("⬅️ Admin panel", "a:home"))
            return
        context.user_data.pop("adm", None)
        people = await ann_recipients(adm["aud"])
        sent = failed = 0
        for uid in people:
            try:
                await send_with_retry(
                    lambda: context.bot.send_message(uid, "📣 Announcement\n\n" + adm["text"]),
                    retry_timeouts=True,
                )
                sent += 1
            except (TelegramError, RuntimeError):
                failed += 1  # e.g. they blocked the bot
            await asyncio.sleep(0.05)
        log.info("Owner sent an announcement to %s of %s", sent, len(people))
        await show(
            f"✅ Sent to {sent} of {len(people)}."
            + (f" {failed} couldn't be reached (they may have blocked the bot)." if failed else ""),
            one_button("⬅️ Admin panel", "a:home"),
        )

    elif action == "cn":  # owner: pick a family to browse like a client
        markup, empty = await family_picker(int(parts[2]), "a:cn", "a:cnf:{tid}")
        await show("No families yet." if empty else "Which family do you want to browse?", markup)

    elif action == "cnf":
        tid = int(parts[2])
        fam = await need_family(tid)
        if not fam:
            return
        context.bot_data.setdefault("preview", {})[q.from_user.id] = tid
        await show(
            f"👁 Connected to the {fam['name']} family. You see exactly what they see. "
            "Nothing is saved about you. /unlink disconnects you.",
            menu_markup(),
        )

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
        if action == "dfd" and fam.get("sug_thread_id"):
            try:
                await context.bot.delete_forum_topic(GROUP_ID, fam["sug_thread_id"])
            except TelegramError:
                log.warning("Couldn't delete the suggestions topic of %s", fam["name"])
        await db.users.delete_many({"thread_id": tid})
        await db.photos.delete_many({"thread_id": tid})
        await db.suggestions.delete_many({"thread_id": tid})
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
    await db.tries.create_index("at", expireAfterSeconds=2592000)  # forget login attempts after 30 days
    await db.sent.create_index("at", expireAfterSeconds=172800)  # forget hide-buttons after 2 days
    log.info("Connected to MongoDB database '%s'", DB_NAME)


def main() -> None:
    start_health_server()
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .connect_timeout(20)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(20)
        .build()
    )

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
        app.add_handler(CallbackQueryHandler(on_suggestion_button, pattern=r"^sa:(y|o|n):[0-9a-f]{10}$"))
    else:
        log.warning("GROUP_ID is not set: send /id in your group, then put it in the environment.")

    priv = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start, filters=priv))
    app.add_handler(CommandHandler(["menu", "gallery"], menu, filters=priv))
    app.add_handler(CommandHandler("connect", connect_cmd, filters=priv))
    app.add_handler(CommandHandler("announce", announce_cmd, filters=priv))
    app.add_handler(CommandHandler("unlink", unlink_private, filters=priv))
    app.add_handler(CommandHandler("admin", admin_cmd, filters=priv))
    app.add_handler(CommandHandler("addphotos", addphotos, filters=priv))
    app.add_handler(CommandHandler("done", done, filters=priv))
    app.add_handler(MessageHandler(priv & filters.TEXT & ~filters.COMMAND, on_private_text))
    app.add_handler(MessageHandler(priv & (filters.PHOTO | filters.Document.ALL | filters.VIDEO), on_private_media))
    app.add_handler(CallbackQueryHandler(on_admin_button, pattern=r"^a:"))
    app.add_handler(
        CallbackQueryHandler(on_button, pattern=r"^(menu|all|d:\d+|l:\d+|D:\d{4}-\d{2}-\d{2}|L:[0-9a-f]{10}|L:none|x:\d+|sg:(new|send|gal))$")
    )
    app.add_error_handler(on_error)

    log.info("Bot running")
    app.run_polling()


if __name__ == "__main__":
    main()
