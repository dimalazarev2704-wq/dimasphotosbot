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
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pymongo import AsyncMongoClient
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
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

try:
    import webapp  # the Mini App's web server (webapp.py, next to this file)
except Exception:  # missing or broken file: the bot still runs, only the photo app is off
    logging.getLogger("photobot").exception("Couldn't load webapp.py, so the photo app is OFF")
    webapp = None

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
COPYRIGHT = (os.getenv("COPYRIGHT_NOTICE", "") or "© 2024 Dima Lazarev. All photos are protected by copyright.").strip()
COPYRIGHT_WARNING = f"⚠️ {COPYRIGHT}"
PROTECT_CONTENT = os.getenv("PROTECT_CONTENT", "false").lower() == "true"
# The public https address of this service, for the Mini App. Render sets RENDER_EXTERNAL_URL by itself.
WEBAPP_URL = (os.getenv("WEBAPP_URL", "") or os.getenv("RENDER_EXTERNAL_URL", "")).strip().rstrip("/")
if WEBAPP_URL and not WEBAPP_URL.startswith("https://"):
    WEBAPP_URL = ""  # Telegram only opens https pages
if webapp is None:
    WEBAPP_URL = ""  # no photo app without its server file
# "no" = the bot never changes the blue Open button; you set it yourself in BotFather
AUTO_MENU_BUTTON = os.getenv("AUTO_MENU_BUTTON", "yes").strip().lower() not in ("no", "off", "false", "0")
MAX_SEND = int(os.getenv("MAX_SEND", "200"))  # max files sent per button press
PAGE_SIZE = 20  # buttons per page in the date/location lists
MAX_FAILS = 5  # wrong guesses allowed before a lock ...
LOCK_SECONDS = 3600  # ... and how long the lock lasts
FAMILY_MAX_MEMBERS = int(os.getenv("FAMILY_MAX_MEMBERS", "12") or 12)  # most people one family can have
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
CAPTION_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}|no date)(?!\w)\s*(?:[|,\-–]\s*)?(.*)$", re.S | re.I)


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
    thumb: str | None = None,
) -> None:
    """Save one photo or file in the index with its date/location (from the caption).
    kind is "photo" (a normal Telegram photo, which Telegram compresses) or "document"
    (sent as a File, which keeps the original quality)."""
    default_day = sent_at.astimezone(TZ).date().isoformat()
    star = "⭐" in (caption or "")  # a ⭐ in the caption marks the photo(s) as a best shot
    if star:
        caption = caption.replace("⭐", " ").strip()
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
    if thumb:
        set_doc["thumb"] = thumb
    if star:
        set_doc["best"] = True
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
                {"$set": {"date": day, "location": loc, "loc_id": lid, **({"best": True} if star else {})}},
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
        if not star:  # (a star already goes in $set; the same field can't be in both)
            insert_doc["best"] = bool(inherit and inherit.get("best"))

    await db.photos.update_one(
        {"message_id": message_id},
        {"$set": set_doc, "$setOnInsert": insert_doc},
        upsert=True,
    )


def once(context: ContextTypes.DEFAULT_TYPE, key: str) -> bool:
    """True the first time a key is seen (so an album of 20 files only gets one reply)."""
    store = context.user_data if context.user_data is not None else context.bot_data  # per person
    seen = store.setdefault("once", [])
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


def thumb_of(msg) -> str | None:
    """The file_id of Telegram's own small preview of a photo or image file (about 320 px).
    The Mini App uses it for the grid, so it loads fast and also works for files over 20 MB."""
    try:
        if msg.photo:
            sizes = sorted(msg.photo, key=lambda p: p.width or 0)
            for p in sizes:
                if (p.width or 0) >= 300:
                    return p.file_id
            return sizes[-1].file_id if sizes else None
        th = getattr(msg.document, "thumbnail", None) if msg.document else None
        return th.file_id if th else None
    except Exception:
        return None


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
    if await db.families.find_one({"sug_thread_id": thread_id}):
        await msg.reply_text("This is a suggestions topic, so it can't be a family. Use the family's own topic.")
        return
    if await db.meta.find_one({"_id": "addfam", "thread_id": thread_id}):
        await msg.reply_text("This is the ➕ Add families topic, so it can't be a family. Use the family's own topic.")
        return
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


async def reply_long(msg, text: str) -> None:
    """Telegram messages are limited to 4096 characters; split long lists on line breaks."""
    while len(text) > 4000:
        cut = text.rfind("\n", 0, 4000)
        cut = cut if cut > 0 else 4000
        await msg.reply_text(text[:cut])
        text = text[cut:].lstrip("\n")
    await msg.reply_text(text)


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
    await reply_long(msg, f"{fam['name']} family members:\n" + "\n".join(lines))


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
    await reply_long(msg, "Families:\n" + "\n".join(lines))


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
        if "not found" in (getattr(e, "message", "") or str(e)).lower():  # already gone from the group
            await db.photos.delete_one({"message_id": message_id})
            await q.edit_message_text("That photo was already deleted in the group. I removed it from the list too.")
            return
        await q.edit_message_text(
            f"I couldn't delete it ({e.message}). Make sure I'm an admin with the "
            "“Delete messages” right, and that the photo is less than 48 hours old or I'm an admin."
        )
        return
    await db.photos.delete_one({"message_id": message_id})
    await q.edit_message_text("🗑 That photo was deleted.")


async def delete_photo_everywhere(bot, message_id: int) -> str | None:
    """Deletes a photo from the group and from the index (what /remove does). Returns a problem text, or None."""
    try:
        await bot.delete_message(GROUP_ID, message_id)
    except TelegramError as e:
        if "message to delete not found" not in (getattr(e, "message", "") or str(e)).lower():
            return (f"I couldn't delete it ({getattr(e, 'message', e)}). Make sure I'm an admin with the "
                    "“Delete messages” right.")
        # (already gone from the group: just clean up the list)
    await db.photos.delete_one({"message_id": message_id})
    return None


async def on_group_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg.is_topic_message:  # ignore the General topic
        return
    file_id, kind = media_of(msg)
    if not file_id:
        return
    if await db.families.find_one({"sug_thread_id": msg.message_thread_id}):
        return  # a family's suggestions topic: photos there are only waiting for approval
    if await db.meta.find_one({"_id": "addfam", "thread_id": msg.message_thread_id}):
        return  # the ➕ Add families topic is never a family
    await index_file(
        msg.message_thread_id, msg.message_id, file_id, msg.caption, msg.media_group_id, msg.date, kind,
        thumb_of(msg),
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

def app_row() -> list:
    """The 📱 button that opens the Mini App (only when the service has a public https address)."""
    if not WEBAPP_URL:
        return []
    return [[InlineKeyboardButton("📱 Open the photo app", web_app=WebAppInfo(url=f"{WEBAPP_URL}/app"))]]


def menu_markup(extra: list | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        app_row()
        + (extra or [])
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
            [InlineKeyboardButton("👪 Add a family member", callback_data="fm:new")],
        ]
    )


async def my_thread(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int | None:
    """The family (topic id) this person browses: their linked family, or, for the owner only,
    the family they picked with /connect. The owner's pick lives in memory only, never in the database."""
    doc = await db.users.find_one({"_id": user_id})
    if doc:
        return None if doc.get("pending") else doc["thread_id"]  # (pending = added by a relative, hasn't confirmed their name yet)
    if is_owner(user_id):
        return context.bot_data.get("preview", {}).get(user_id)
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if await handle_pending(update, context, None):
        return
    if await maybe_welcome(update, context):
        return
    tid = await my_thread(context, user.id)
    if tid:
        await update.message.reply_text(
            "Welcome back! What would you like to see?", reply_markup=await family_menu(tid)
        )
        await ensure_keyboard(context.bot, update.effective_message.chat_id, user.id)
        return
    lock = await locked_for(f"link:{user.id}")
    if lock:
        await update.message.reply_text(lock_text(lock))
        return
    if not is_owner(user.id) and await pending_newfam(user.id):
        await update.message.reply_text(
            f"Your request is waiting for {OWNER_NAME}. I'll message you here as soon as your family is set up. 🙂"
        )
        return
    clear_flows(context)
    if not is_owner(user.id):
        await record_try(user, "started, didn't finish", keep_existing=True)
    context.user_data["link"] = {"stage": "first"}
    await update.message.reply_text("Hi! 👋 Let's get you to your photos. What's your first name?")


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/gallery and /menu: open the gallery."""
    context.user_data.pop("sug", None)
    if await handle_pending(update, context, None):
        return
    if await maybe_welcome(update, context):
        return
    tid = await my_thread(context, update.effective_user.id)
    if tid:
        await update.message.reply_text("What would you like to see?", reply_markup=await family_menu(tid))
        await ensure_keyboard(context.bot, update.effective_message.chat_id, update.effective_user.id)
    elif is_owner(update.effective_user.id):
        await update.message.reply_text("No family picked yet. Send /connect to choose one to browse.")
    else:
        await update.message.reply_text("Send /start to link your account first.")


async def connect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner only: pick a family to browse as its clients do. Kept in memory, never saved."""
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("That command isn't available. Send /start to link your account.")
        return
    clear_flows(context)
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
        link.update(stage="last", first=" ".join(text.split()))
        await msg.reply_text(f"Nice to meet you, {text}! What's your family's last name?")

    elif stage == "last":
        if not normalize(text) or len(text) > 60:
            await msg.reply_text("Please send your family's last name.")
            return
        link.update(stage="code", last=text)
        await msg.reply_text(
            "Thanks! Now the 6-digit code you were given.\n\nNew here and don't have a code?",
            reply_markup=one_button("🆕 I'm new, I don't have a code", "nf:new"),
        )

    elif stage == "nf":
        await msg.reply_text("Please tap ✅ Yes or ✏️ No above.")

    elif stage == "code":
        key = f"link:{user.id}"
        lock = await locked_for(key)
        if lock:
            context.user_data.pop("link", None)
            await msg.reply_text(lock_text(lock))
            return
        if await db.users.find_one({"_id": user.id}):  # e.g. the owner added them while they were typing
            context.user_data.pop("link", None)
            await msg.reply_text("You're already in a family. Send /gallery to see the photos.", reply_markup=menu_markup())
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
            reply_markup=await family_menu(fam["thread_id"]),
        )
        await ensure_keyboard(context.bot, msg.chat_id, user.id)


# ---------- private chat: /addphotos (password every time, nothing saved about you) ----------

async def addphotos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if is_owner(user.id):  # the owner picks a family from buttons, no password
        clear_flows(context)
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
    clear_flows(context)
    context.user_data["up"] = {"stage": "password", "until": time.time() + SESSION_SECONDS}
    await update.message.reply_text(
        "Please type the password. I'll delete your message right after."
    )


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    up = context.user_data.pop("up", None)
    if not up:
        await update.message.reply_text("Nothing to finish. Send /addphotos to add photos.")
        return
    offer, markup = await notify_offer(context, up, update.effective_user.id)
    await update.message.reply_text(
        f"Done. Added {up.get('count', 0)} file(s). The session is closed." + offer, reply_markup=markup
    )


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
        if is_owner(update.effective_user.id):
            if once(context, f"noup:{msg.media_group_id or msg.message_id}"):
                await msg.reply_text(
                    "No upload is running, so I did not add that. Open /admin → Add photos, "
                    "pick the family, then send the files."
                )
        elif await my_thread(context, update.effective_user.id) and once(context, f"idle:{msg.media_group_id or msg.message_id}"):
            await msg.reply_text(
                "I can only take photos when you tap ➕ Suggest photos first (it may have been reset). "
                "Open the menu with /gallery.",
                reply_markup=menu_markup(),
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
    await index_file(up["thread_id"], sent.message_id, file_id, caption, msg.media_group_id, msg.date, kind,
                     thumb_of(sent) or thumb_of(msg))
    note_batch(up, caption, msg.date)
    up["count"] += 1
    up["until"] = time.time() + SESSION_SECONDS
    if not msg.media_group_id or msg.media_group_id != up.get("last_mg"):
        up["last_mg"] = msg.media_group_id
        await msg.reply_text(f"✅ Added to the {up['name']} family. Send more, or /done.")


async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if await handle_pending(update, context, msg.text or ""):
        return
    if (msg.text or "").strip() in (PHOTOS_BUTTON, HELP_BUTTON) and not is_owner(update.effective_user.id):
        clear_flows(context)  # tapping a pinned button always works, even in the middle of something
        if msg.text.strip() == HELP_BUTTON:
            await help_cmd(update, context)
        elif await my_thread(context, update.effective_user.id):
            await menu(update, context)
        else:
            await msg.reply_text(help_text(False))
        return
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
    fm = context.user_data.get("fm")
    if fm:
        await handle_family_member_text(update, context, fm)
        return
    adm = context.user_data.get("adm")
    if adm and is_owner(update.effective_user.id):
        await handle_admin_text(update, context, adm)
        return
    link = context.user_data.get("link")
    if link:
        await handle_link_step(update, context, link)
        return
    tid = await my_thread(context, update.effective_user.id)
    if tid:
        await msg.reply_text(
            "Hi! 👋 I can show you your family's photos. Tap what you'd like to see (or tap ❓ Help):",
            reply_markup=await family_menu(tid),
        )
        await ensure_keyboard(context.bot, msg.chat_id, update.effective_user.id)
    elif is_owner(update.effective_user.id):
        await msg.reply_text("Send /admin for the admin panel, or /connect to browse a family.")
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
        try:
            await bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO if is_photo else ChatAction.UPLOAD_DOCUMENT)
        except TelegramError:
            pass  # just the "sending…" indicator, not worth stopping for
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
    tid = query.get("thread_id")
    last = await bot.send_message(
        chat_id, "That's everything! 📸" + note + "\n\n" + COPYRIGHT_WARNING + "\n\nAnything else?", reply_markup=await family_menu(tid)
    )
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
            await bot.edit_message_reply_markup(chat_id, last.message_id, reply_markup=await family_menu(tid, extra))
        except TelegramError:
            pass


BACKGROUND_SENDS = True  # tests switch this off to wait for the sends


async def start_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, query: dict) -> None:
    """Sends a gallery without holding up the bot: a big gallery takes minutes (Telegram's limits),
    and updates are handled one at a time, so doing it inline would freeze everyone else."""
    sending = context.bot_data.setdefault("sending", set())
    if chat_id in sending:
        await context.bot.send_message(chat_id, "I'm still sending your photos. One moment! ⏳")
        return

    async def run() -> None:
        try:
            await send_photos(context.bot, chat_id, query)
        except Exception:
            log.exception("Sending a gallery failed")
            try:
                await context.bot.send_message(chat_id, "Something went wrong while sending. Please try again.")
            except TelegramError:
                pass
        finally:
            sending.discard(chat_id)

    sending.add(chat_id)
    if not BACKGROUND_SENDS:
        await run()
        return
    task = asyncio.create_task(run())
    tasks = context.bot_data.setdefault("tasks", set())
    tasks.add(task)  # keep a reference so it isn't garbage-collected mid-send
    task.add_done_callback(tasks.discard)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    thread_id = await my_thread(context, q.from_user.id)
    if not thread_id:
        await q.message.reply_text(
            "Send /connect to pick a family to browse." if is_owner(q.from_user.id)
            else "Please send /start to link your account first."
        )
        return
    data = q.data
    chat_id = q.message.chat_id

    async def show(text: str, markup: InlineKeyboardMarkup) -> None:
        try:
            await q.edit_message_text(text, reply_markup=markup)
        except TelegramError as e:
            if "not modified" in str(e.message).lower():
                return
            try:  # e.g. the message is a photo (the add-a-relative summary): replace it instead
                await q.message.delete()
            except (TelegramError, AttributeError):
                pass
            try:
                await context.bot.send_message(chat_id, text, reply_markup=markup)
            except TelegramError:
                log.warning("Couldn't update the screen: %s", e.message)

    if data == "menu":
        context.user_data.pop("sug", None)
        await show("What would you like to see?", await family_menu(thread_id))

    elif data == "bs":  # ⭐ Best shots
        if not await has_best(thread_id):
            await show("No best shots have been picked for your family yet. Check back soon!", await family_menu(thread_id))
            return
        await start_send(context, chat_id, {"thread_id": thread_id, "best": True})

    elif data in ("sg:new", "sg:send") and is_owner(q.from_user.id):
        await show(
            "Suggestions are for clients, so this preview doesn't send them (it would save your account). "
            "You can add photos yourself from /admin → Add photos.",
            await family_menu(thread_id),
        )

    elif data in ("fm:new", "fm:x", "fm:ok") and is_owner(q.from_user.id):
        await show(
            "This is for clients (it would save your account). To add someone yourself, use /admin → Add person.",
            await family_menu(thread_id),
        )

    elif data == "fm:new":  # a client adds a relative
        clear_flows(context)
        context.user_data["fm"] = {"thread_id": thread_id, "until": time.time() + SESSION_SECONDS, "stage": "who"}
        await db.users.update_one({"_id": q.from_user.id}, {"$set": {"kb": False}})  # so the pinned buttons come back later
        await show(FM_ASK, one_button("Cancel", "fm:x"))
        await context.bot.send_message(chat_id, "👇 Tap “Choose a person”", reply_markup=fm_pick_keyboard())

    elif data == "fm:ok":
        fm = context.user_data.get("fm")
        if not fm or fm.get("stage") != "ok" or fm["until"] < time.time() or fm["thread_id"] != thread_id:
            context.user_data.pop("fm", None)
            await context.bot.send_message(chat_id, "That timed out. Tap 👪 Add a family member to start again.",
                                           reply_markup=await family_menu(thread_id))
            return
        await fm_confirm(update, context, fm, q)

    elif data == "fm:x":
        had = context.user_data.pop("fm", None)
        await show("What would you like to see?", await family_menu(thread_id))
        if had:
            await restore_keyboard(context.bot, chat_id)

    elif data == "sg:new":  # start suggesting photos
        clear_flows(context)
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
            await q.message.reply_text(note, reply_markup=await family_menu(thread_id) if not sug else sug_files_markup())
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
            await family_menu(thread_id),
        )

    elif data == "sg:gal":  # the family-suggestions gallery
        await start_send(context, chat_id, {"thread_id": thread_id, "sug": True})

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
            await family_menu(thread_id),
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
            await show("No photos yet. Check back soon!", await family_menu(thread_id))
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
            await show("No photos yet. Check back soon!", await family_menu(thread_id))
            return
        rows.sort(key=lambda r: (r["_id"] == "none", (r["loc"] or "").casefold()))
        items = [((r["loc"] or "No location")[:40] + f" ({r['n']})", f"L:{r['_id']}") for r in rows]
        await show("Pick a location:", paged_keyboard(items, int(data[2:]), "l"))

    elif data.startswith("D:"):
        await start_send(context, chat_id, {"thread_id": thread_id, "date": data[2:]})

    elif data.startswith("L:"):
        await start_send(context, chat_id, {"thread_id": thread_id, "loc_id": data[2:]})

    elif data == "all":
        await start_send(context, chat_id, {"thread_id": thread_id})


# ---------- friendly help + pinned button, best shots, "new photos are ready" ----------

PHOTOS_BUTTON = "📷 My photos"
HELP_BUTTON = "❓ Help"


def client_keyboard() -> ReplyKeyboardMarkup:
    """The two buttons that stay at the bottom of a client's chat."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(PHOTOS_BUTTON), KeyboardButton(HELP_BUTTON)]],
        resize_keyboard=True,
        is_persistent=True,
    )


async def has_best(thread_id: int | None) -> bool:
    return bool(thread_id) and await db.photos.count_documents({"thread_id": thread_id, "best": True}) > 0


async def family_menu(thread_id: int | None, extra: list | None = None) -> InlineKeyboardMarkup:
    """The client menu, with ⭐ Best shots on top when the photographer has picked some."""
    rows = list(extra or [])
    if await has_best(thread_id):
        rows.append([InlineKeyboardButton("⭐ Best shots", callback_data="bs")])
    return menu_markup(rows)


async def ensure_keyboard(bot, chat_id: int, user_id: int) -> None:
    """Shows the pinned 📷 My photos / ❓ Help buttons once to every linked client."""
    doc = await db.users.find_one({"_id": user_id})
    if not doc or doc.get("kb"):
        return
    try:
        await bot.send_message(
            chat_id,
            "👇 I put a “📷 My photos” button at the bottom of this chat, so you can always find your "
            "photos. “❓ Help” explains everything.",
            reply_markup=client_keyboard(),
        )
        await db.users.update_one({"_id": user_id}, {"$set": {"kb": True}})
    except TelegramError:
        pass


def help_text(linked: bool) -> str:
    if not linked:
        return (
            "👋 Hi! This bot gives you your family's photos.\n\n"
            "Send /start, then type your first name, your family's last name and the 6-digit code you "
            f"were given. If you don't have a code, ask {OWNER_NAME}."
        )
    return (
        "ℹ️ How this works\n\n"
        + ("📱 Open the photo app is the nicest way to look at your photos: big pictures, swipe, zoom, "
           "and a button to send any photo to this chat in full quality.\n" if WEBAPP_URL else "")
        + "📷 My photos opens your menu. Pick By date, By location or All photos.\n"
        "⭐ Best shots (when it's there) has the favourites picked for you.\n"
        "🧹 Under your photos, “Hide these photos” clears your chat. They stay in the gallery, so you "
        "can open them again any time.\n"
        "➕ Suggest photos lets you share your own for the gallery.\n"
        "👪 Add a family member lets you add a relative: pick them from your contacts, then type their name.\n\n"
        f"{COPYRIGHT_WARNING}\n\n"
        f"Stuck? Message {OWNER_NAME}."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    linked = bool(await my_thread(context, uid))
    await update.effective_message.reply_text(help_text(linked))
    if linked:
        await ensure_keyboard(context.bot, update.effective_message.chat_id, uid)


@admin_only
async def best_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply to a photo with /best to add it to (or take it out of) the family's ⭐ Best shots."""
    msg = update.effective_message
    target = msg.reply_to_message
    if not target or not (target.photo or target.document):
        await msg.reply_text(
            "Reply to a photo with /best to add it to the family's ⭐ Best shots (reply again to take it out).\n"
            "You can also put a ⭐ in a caption when you post, for example: 2026-07-04 | Central Park ⭐"
        )
        return
    row = await db.photos.find_one({"message_id": target.message_id})
    if not row:
        await msg.reply_text("I don't have that photo in my list, so I can't mark it.")
        return
    now_best = not row.get("best", False)
    await db.photos.update_one({"message_id": target.message_id}, {"$set": {"best": now_best}})
    await msg.reply_text("⭐ Added to Best shots." if now_best else "Taken out of Best shots.")


# --- "New photos are ready": after you finish adding photos, offer to tell the family ---

def note_batch(up: dict, caption: str | None, sent_at: datetime) -> None:
    """Remember which dates and places this batch covered (for the notification)."""
    day, loc = parse_caption((caption or "").replace("⭐", ""), sent_at.astimezone(TZ).date().isoformat())
    dates = up.setdefault("dates", [])
    if day not in dates and len(dates) < 10:
        dates.append(day)
    places = up.setdefault("places", [])
    if loc and loc not in places and len(places) < 10:
        places.append(loc)


async def notify_offer(context: ContextTypes.DEFAULT_TYPE, up: dict, who: int):
    """Returns (extra text, buttons) offering to tell the family, or ("", None) if there's no one to tell."""
    if not up or not up.get("count"):
        return "", None
    people = [u for u in await db.users.find({"thread_id": up["thread_id"], "pending": {"$ne": True}}).to_list() if u["_id"] != who]
    if not people:
        return "", None
    token = secrets.token_hex(4)
    pending = context.bot_data.setdefault("notify", {})
    pending[token] = {
        "tid": up["thread_id"], "name": up["name"], "who": who,
        "dates": list(up.get("dates", [])), "places": list(up.get("places", [])),
    }
    while len(pending) > 20:
        pending.pop(next(iter(pending)))
    n = len(people)
    text = f"\n\n📣 Tell the {up['name']} family their new photos are ready? ({n} {'person' if n == 1 else 'people'})"
    return text, InlineKeyboardMarkup(
        [[_btn(f"✅ Yes, tell them", f"nt:y:{token}"), _btn("No thanks", f"nt:n:{token}")]]
    )


def notify_message(info: dict) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"📸 {OWNER_NAME} added new photos for your family!"]
    if len(info["dates"]) == 1:
        lines.append("📅 " + pretty_date(info["dates"][0]))
    if len(info["places"]) == 1:
        lines.append("📍 " + info["places"][0])
    rows = [
        [InlineKeyboardButton(f"📅 Open {pretty_date(d)}", callback_data=f"D:{d}")] for d in info["dates"][:3]
    ]
    rows.append([InlineKeyboardButton("🏠 Open the gallery", callback_data="menu")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def on_notify_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    _, action, token = q.data.split(":")
    info = context.bot_data.get("notify", {}).get(token)
    if info and q.from_user.id != info["who"]:
        await q.answer("This isn't yours to answer.", show_alert=True)
        return
    await q.answer()

    async def show(text: str) -> None:
        try:
            await q.edit_message_text(text)
        except TelegramError:
            pass

    if not info:
        await show("That was already handled (or the bot restarted). If you need to, you can send a message from /admin → 📣 Announce.")
        return
    context.bot_data["notify"].pop(token, None)
    if action == "n":
        await show("OK, I won't message them.")
        return
    text, markup = notify_message(info)
    sent = failed = 0
    for u in await db.users.find({"thread_id": info["tid"], "pending": {"$ne": True}}).to_list():
        if u["_id"] == info["who"]:
            continue
        try:
            await send_with_retry(lambda: context.bot.send_message(u["_id"], text, reply_markup=markup), retry_timeouts=True)
            sent += 1
        except (TelegramError, RuntimeError):
            failed += 1  # e.g. they blocked the bot
        await asyncio.sleep(0.05)
    await show(
        f"✅ Told {sent} {'person' if sent == 1 else 'people'} in the {info['name']} family."
        + (f" {failed} couldn't be reached." if failed else "")
    )


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


FLOW_KEYS = ("up", "link", "adm", "sug", "fm")


def clear_flows(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starting any flow ends every other half-finished one, so text and photos never go to the wrong one."""
    for k in FLOW_KEYS:
        context.user_data.pop(k, None)


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
        text = text[:400]  # (keeps the replies and the photographer's message under Telegram's size limit)
        if clean_words(text) not in ("no", "none", "nope", "n", "nobody", "-"):
            own = await db.families.find_one({"thread_id": sug["thread_id"]})
            for raw in re.split(r"[,\n;]| and ", text)[:10]:
                name = " ".join(raw.split())[:40]
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
        typed = ", ".join(" ".join(r.split())[:40] for r in re.split(r"[,\n;]| and ", text)[:10] if normalize(r))
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
    sug["items"].append({"file_id": file_id, "kind": kind, **({"thumb": thumb_of(msg)} if thumb_of(msg) else {})})
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
    if user_doc and user_doc.get("thread_id") not in (None, sug["thread_id"]):
        return "Your family was changed, so please start the suggestion again."
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

    async def edit(text: str, markup=None) -> None:
        try:
            await q.edit_message_text(text, reply_markup=markup)
        except TelegramError:
            pass

    await process_suggestion(context.bot, sid, action, edit)


BG_TASKS: set = set()  # keeps background jobs (like an announcement being sent) from being forgotten
SUG_BUSY: set = set()  # suggestions being handled right now (so a double tap can't add the photos twice)


async def process_suggestion(bot, sid: str, action: str, edit) -> None:
    """Approve (y = all the families in the photos, o = only the sender's family) or reject (n) a suggestion.
    `edit(text, markup)` shows the result: in the group's message, or (from the app) the same message."""
    if sid in SUG_BUSY:
        await edit("I'm already working on this one. Please wait a moment.")
        return
    SUG_BUSY.add(sid)
    try:
        await _process_suggestion(bot, sid, action, edit)
    finally:
        SUG_BUSY.discard(sid)


async def _process_suggestion(bot, sid: str, action: str, edit) -> None:
    doc = await db.suggestions.find_one({"_id": sid})
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
                            **({"thumb": thumb_of(sm)} if thumb_of(sm) else {}),
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
    q = {"pending": {"$ne": True}}
    if aud != "all":
        q["thread_id"] = int(aud)
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


WHEN_HELP = (
    "🕒 When should it go out? (your time, " + str(TZ) + ")\n\n"
    "Examples:\n• tomorrow 9am\n• 2026-10-20 18:30\n• today 7:15pm"
)


def parse_when(text: str) -> datetime | None:
    """'tomorrow 9am' / '2026-10-20 18:30' / 'today 7pm' -> an aware datetime in TZ; None if unusable."""
    t = " ".join(text.strip().casefold().split())
    m = re.fullmatch(r"(today|tomorrow|\d{4}-\d{2}-\d{2})?\s*(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        return None
    day_s, hh, mm, ap = m.group(1), int(m.group(2)), int(m.group(3) or 0), m.group(4)
    if not ap and m.group(3) is None:
        return None  # a bare number like "9" is too easy to misread
    if ap:
        if not 1 <= hh <= 12:
            return None
        hh = hh % 12 + (12 if ap == "pm" else 0)
    if hh > 23 or mm > 59:
        return None
    today = datetime.now(TZ).date()
    if day_s == "tomorrow":
        d = today + timedelta(days=1)
    elif day_s in (None, "today"):
        d = today
    else:
        try:
            d = date.fromisoformat(day_s)
        except ValueError:
            return None
    local = datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ)
    utc = local.astimezone(timezone.utc)
    when = utc.astimezone(TZ)  # (a time that doesn't exist on a clock-change night shifts to the real one)
    now = datetime.now(timezone.utc)
    if utc <= now + timedelta(minutes=1) or utc > now + timedelta(days=366):
        return None
    return when


async def broadcast(bot, aud: str, text: str) -> tuple[int, int, int]:
    """Sends an announcement to everyone in the audience. Returns (sent, failed, total)."""
    people = await ann_recipients(aud)
    sent = failed = 0
    for uid in people:
        try:
            await send_with_retry(
                lambda: bot.send_message(uid, "📣 Announcement\n\n" + text),
                retry_timeouts=True,
            )
            sent += 1
        except (TelegramError, RuntimeError):
            failed += 1  # e.g. they blocked the bot
        await asyncio.sleep(0.05)
    return sent, failed, len(people)


def when_label(at: datetime) -> str:
    local = at.astimezone(TZ)
    return local.strftime("%a %b %d, %Y at %I:%M %p").replace(" 0", " ")


async def scheduled_view(page: int = 0):
    docs = await db.scheduled.find().sort("at", 1).to_list()
    items = []
    for d in docs:
        at = d["at"].replace(tzinfo=timezone.utc) if d["at"].tzinfo is None else d["at"]
        items.append((f"{when_label(at)} · {d['text'][:25]}", f"a:scv:{d['_id']}"))
    markup = admin_pager(items, page, "a:sc", "a:home")
    text = "🕒 Scheduled announcements (tap one to see or cancel it):" if docs else "🕒 Nothing is scheduled."
    return text, markup


async def scheduler_loop(app: Application) -> None:
    """Every 30 seconds: send announcements whose time has come. They live in Mongo, so a restart
    doesn't lose them (if the bot was asleep, they go out as soon as it wakes up)."""
    while True:
        try:
            await asyncio.sleep(30)
            while True:
                now = datetime.now(timezone.utc)
                # "Claim" it (not delete) so a crash or restart mid-send doesn't lose it: an unfinished
                # claim is picked up again after 10 minutes. It's deleted only once it has been sent.
                doc = await db.scheduled.find_one_and_update(
                    {"at": {"$lte": now}, "claimed": {"$lte": now - timedelta(minutes=10)}},
                    {"$set": {"claimed": now}},
                )
                if not doc:
                    break
                at = doc["at"].replace(tzinfo=timezone.utc) if doc["at"].tzinfo is None else doc["at"]
                if now - at > timedelta(hours=12):  # the bot was down for ages: don't send it out of the blue
                    await db.scheduled.delete_one({"_id": doc["_id"]})
                    if OWNER_ID:
                        try:
                            await app.bot.send_message(
                                OWNER_ID,
                                f"⚠️ A scheduled announcement for {when_label(at)} was NOT sent, because the bot "
                                f"was offline until much later.\n\n{doc['text'][:300]}",
                            )
                        except TelegramError:
                            pass
                    continue
                sent, failed, total = await broadcast(app.bot, doc["aud"], doc["text"])
                await db.scheduled.delete_one({"_id": doc["_id"]})
                log.info("Sent a scheduled announcement to %s of %s", sent, total)
                if OWNER_ID:
                    try:
                        await app.bot.send_message(
                            OWNER_ID,
                            f"🕒 Your scheduled announcement went out to {await ann_audience_name(doc['aud'])}: "
                            f"{sent} of {total} received it."
                            + (f" {failed} couldn't be reached." if failed else "")
                            + f"\n\n{doc['text'][:200]}",
                        )
                    except TelegramError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Scheduler problem (will try again)")


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
                [_btn(f"✅ Send to {len(people)} now", "a:ans")],
                [_btn("🕒 Schedule it", "a:ansc")],
                [_btn("✏️ Rewrite", f"a:ana:{adm['aud']}"), _btn("Cancel", "a:home")],
            ]
        ),
    )


async def announce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("That command isn't available.")
        return
    clear_flows(context)
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


async def client_add_problem(uid: int, tid: int, final: bool = False) -> str:
    """Why a family member can't add this account (or '' if they can). Never reveals other families.
    The check that could reveal whether an account is known elsewhere only runs at the very end (final)."""
    existing = await db.users.find_one({"_id": uid})
    if existing and existing["thread_id"] == tid:
        return "That person is already in your family."
    if final and (existing or is_owner(uid)):
        return f"I couldn't add them. Please ask {OWNER_NAME} for help."
    if await db.users.count_documents({"thread_id": tid}) >= FAMILY_MAX_MEMBERS:
        return f"Your family already has the most people I can add ({FAMILY_MAX_MEMBERS}). Please ask {OWNER_NAME}."
    return ""


async def add_person(bot, uid: int, tid: int, first_name: str, by: str | None = None) -> tuple[str, bool]:
    """Links a Telegram account to a family, on the owner's say-so, or (when `by` is the first name of a
    family member) because a family member added a relative.
    Returns (problem or '', whether the person could be messaged right away)."""
    fam = await db.families.find_one({"thread_id": tid})
    if not fam:
        return "That family no longer exists.", False
    existing = await db.users.find_one({"_id": uid})
    if by:
        problem = await client_add_problem(uid, tid, final=True)
        if problem:
            return problem, False
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
            "linked_at": datetime.now(timezone.utc), "added_by_owner": not by, "welcomed": False,
            "added_by": by, "pending": bool(by),
        }},
        upsert=True,
    )
    await db.tries.delete_one({"_id": uid})
    await db.newfam.delete_many({"uid": uid})  # (a waiting new-family request is settled now)
    await clear_fails(f"link:{uid}")
    log.info("%s added %s (%s) to family %s", by or "Owner", first_name, uid, fam["name"])
    try:
        if by:  # a relative added them: they confirm who they are before seeing anything
            await bot.send_message(uid, confirm_prompt(by))
            return "", True
        await bot.send_message(uid, welcome_text(fam["name"], by), reply_markup=await family_menu(tid))
        await db.users.update_one({"_id": uid}, {"$set": {"welcomed": True}})
        return "", True
    except TelegramError:
        return "", False  # they haven't opened the bot yet; they'll get the welcome when they do


def welcome_text(family_name: str, by: str | None = None) -> str:
    return f"🎉 {by or OWNER_NAME} has already added you to the {family_name} family. Here are your photos!\n\n{COPYRIGHT_WARNING}"


def confirm_prompt(by: str) -> str:
    return (
        f"👋 {by} added you to a family photo gallery.\n\nTo see the photos, please type your first and last "
        f"name, written the same way {by} wrote it."
    )


async def handle_pending(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str | None) -> bool:
    """Someone a relative added must type their own name exactly as the relative wrote it before they get in.
    Returns True when this message was handled here."""
    uid = update.effective_user.id
    if is_owner(uid):
        return False
    doc = await db.users.find_one({"_id": uid})
    if not doc or not doc.get("pending"):
        return False
    msg = update.effective_message
    by = doc.get("added_by") or "A family member"
    if text is None or text.strip() in (PHOTOS_BUTTON, HELP_BUTTON) or text.strip().startswith("/"):
        await msg.reply_text(confirm_prompt(by))
        return True
    key = f"confirm:{uid}"
    lock = await locked_for(key)
    if lock:
        await msg.reply_text(lock_text(lock))
        return True
    if normalize(text) != normalize(doc.get("first_name", "")) or not normalize(text):
        locked = await record_fail(key)
        await msg.reply_text(
            "That isn't the name I was given. Please type your first and last name exactly as "
            f"{by} wrote it (ask them if you're not sure)."
            + ("\n\nToo many tries, so please wait a bit." if locked else "")
        )
        return True
    await db.users.update_one({"_id": uid}, {"$set": {"pending": False, "welcomed": True}})
    await clear_fails(key)
    fam = await db.families.find_one({"thread_id": doc["thread_id"]})
    log.info("%s confirmed (added by %s)", doc.get("first_name"), by)
    await msg.reply_text(
        welcome_text(fam["name"] if fam else "your", by), reply_markup=await family_menu(doc["thread_id"])
    )
    await ensure_keyboard(context.bot, msg.chat_id, uid)
    return True


async def maybe_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """The first time an owner-added person opens the bot, tell them they're already in."""
    uid = update.effective_user.id
    doc = await db.users.find_one({"_id": uid})
    if not doc or not doc.get("added_by_owner") or doc.get("welcomed") or doc.get("pending"):
        return False
    fam = await db.families.find_one({"thread_id": doc["thread_id"]})
    await db.users.update_one({"_id": uid}, {"$set": {"welcomed": True}})
    await update.effective_message.reply_text(
        welcome_text(fam["name"] if fam else "your", doc.get("added_by")), reply_markup=await family_menu(doc["thread_id"])
    )
    await ensure_keyboard(context.bot, update.effective_message.chat_id, uid)
    return True


async def tries_view(page: int):
    rows = await db.tries.find().sort("at", -1).to_list()
    if not rows:
        return "🕵️ Nobody has tried to log in recently.", one_button("⬅️ Admin panel", "a:home")
    items = [(try_label(t), f"a:trp:{t['_id']}") for t in rows]
    markup = admin_pager(
        items, page, "a:tr", "a:home", extra_rows=[[_btn(f"🗑 Clear the whole list ({len(rows)})", "a:trx")]]
    )
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
    if not parts or not (parts[0].isascii() and parts[0].isdigit()) or not (5 <= len(parts[0]) <= 15):
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


FM_ASK = (
    "👪 Add a family member\n\nStep 1: tap “👤 Choose a person” at the bottom and pick them from your "
    "contacts (or send me their contact card). Then I'll ask for their first name and last name, and show you "
    "everything to check.\n\nWhen they open this bot they'll have to type that same name before they can see "
    "your family's photos (never anyone else's). Only the photographer can remove someone."
)
FM_NOT_FOUND = (
    "I can't look people up by name or @username (Telegram doesn't let bots do that). Please tap "
    "“👤 Choose a person” at the bottom and pick them from your contacts, or send me their contact card."
)


def fm_pick_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(
                "👤 Choose a person",
                request_users=KeyboardButtonRequestUsers(
                    request_id=1, user_is_bot=False, max_quantity=1,
                    request_name=True, request_username=True, request_photo=True,
                ),
            )],
            [KeyboardButton("Cancel")],
        ],
        resize_keyboard=True, one_time_keyboard=True,
    )


async def restore_keyboard(bot, chat_id: int, text: str = "👍") -> None:
    """Puts the pinned 📷 My photos / ❓ Help buttons back after the person-picker."""
    try:
        await bot.send_message(chat_id, text, reply_markup=client_keyboard())
        await db.users.update_one({"_id": chat_id}, {"$set": {"kb": True}})  # (a private chat's id is the person's id)
    except TelegramError:
        pass


async def fm_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The add-a-relative flow for this person if it's still valid, else None (and says why)."""
    fm = context.user_data.get("fm")
    if not fm:
        return None
    msg = update.effective_message
    if fm["until"] < time.time():
        context.user_data.pop("fm", None)
        await msg.reply_text("That timed out. Tap 👪 Add a family member to start again.")
        await restore_keyboard(context.bot, msg.chat_id)
        return None
    tid = await my_thread(context, update.effective_user.id)
    if not tid or tid != fm["thread_id"]:
        context.user_data.pop("fm", None)
        await msg.reply_text("Send /start to link your account.")
        return None
    fm["until"] = time.time() + SESSION_SECONDS
    return fm


async def fm_got_person(update: Update, context: ContextTypes.DEFAULT_TYPE, fm: dict, uid: int, tg_name: str,
                        username: str | None, photo_id: str | None) -> None:
    msg = update.effective_message
    if uid == update.effective_user.id or uid == context.bot.id:
        await msg.reply_text("That's you! Please choose someone else." if uid != context.bot.id else "That's me! Please choose a person.")
        return
    problem = await client_add_problem(uid, fm["thread_id"])
    if problem:
        await msg.reply_text(problem + " Choose someone else, or tap Cancel.")
        return
    fm.update(stage="first", uid=uid, tg_name=tg_name, username=username, photo=photo_id)
    await msg.reply_text(
        f"Got it: {tg_name}" + (f" (@{username})" if username else "") + " ✅",
        reply_markup=client_keyboard(),
    )
    await msg.reply_text("Step 2: type their FIRST name.", reply_markup=one_button("Cancel", "fm:x"))


async def on_users_shared(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    fm = await fm_session(update, context)
    if not fm or fm.get("stage") != "who":
        if not fm:  # a stale picker: put the normal buttons back
            await restore_keyboard(context.bot, update.effective_message.chat_id, "That's no longer active.")
        return
    users = list(update.effective_message.users_shared.users or [])
    if not users:
        return
    u = users[0]
    photo = getattr(u, "photo", None)
    name = " ".join(x for x in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if x) or "that person"
    await fm_got_person(update, context, fm, u.user_id, name, getattr(u, "username", None),
                        photo[-1].file_id if photo else None)


async def on_contact_shared(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    fm = await fm_session(update, context)
    if not fm or fm.get("stage") != "who":
        return
    c = update.effective_message.contact
    if not c or not c.user_id:
        await update.effective_message.reply_text(
            "That contact isn't on Telegram, so I can't add them. Choose someone else, or tap Cancel."
        )
        return
    name = " ".join(x for x in (c.first_name, c.last_name) if x) or "that person"
    await fm_got_person(update, context, fm, c.user_id, name, None, None)


def fm_name_ok(text: str) -> str:
    name = " ".join((text or "").split())
    return name if 1 <= len(name) <= 40 and not re.search(r"[\d@/]", name) and normalize(name) else ""


# ---------- new families asking to be set up (a stranger starts the bot and has no code) ----------

NEWFAM_STALE = 24 * 3600  # a request nobody answered for a day can be made again


async def pending_newfam(uid: int):
    """The person's waiting new-family request, or None (an old unanswered one is forgotten)."""
    r = await db.newfam.find_one({"uid": uid})
    if r:
        at = r["at"].replace(tzinfo=timezone.utc) if r["at"].tzinfo is None else r["at"]
        if (datetime.now(timezone.utc) - at).total_seconds() > NEWFAM_STALE:
            await db.newfam.delete_one({"_id": r["_id"]})
            return None
    return r


async def make_family(bot, name: str) -> tuple[int, str, bool]:
    """Makes the family's topic in the group, saves the family and posts its code there.
    Returns (topic id, code, whether the code was posted in the topic). Raises TelegramError."""
    topic = await bot.create_forum_topic(GROUP_ID, name)
    tid = topic.message_thread_id
    code = await new_code()
    await db.families.update_one(
        {"thread_id": tid},
        {"$set": {"name": name, "key": normalize(name)},
         "$setOnInsert": {"code": code, "created": datetime.now(timezone.utc)}},
        upsert=True,
    )
    log.info("Made family %s", name)
    try:
        await bot.send_message(GROUP_ID, f"🏠 {name} family\nCode: {code}", message_thread_id=tid)
        return tid, code, True
    except TelegramError:
        log.exception("Couldn't post the code in the new topic")
        return tid, code, False


async def post_in_addfam_topic(bot, text: str, markup: InlineKeyboardMarkup) -> int:
    """Posts in the one '➕ Add families' topic (made the first time, reused after that)."""
    meta = await db.meta.find_one({"_id": "addfam"})
    for attempt in (1, 2):
        if not meta or attempt == 2:
            topic = await bot.create_forum_topic(GROUP_ID, "➕ Add families")
            await db.meta.update_one({"_id": "addfam"}, {"$set": {"thread_id": topic.message_thread_id}}, upsert=True)
            meta = {"_id": "addfam", "thread_id": topic.message_thread_id}
        try:
            sent = await bot.send_message(GROUP_ID, text, message_thread_id=meta["thread_id"], reply_markup=markup)
            return sent.message_id
        except TelegramError as e:
            if attempt == 2 or "thread" not in str(e.message).lower():
                raise  # (a deleted topic says "message thread not found": then we make it again)
    raise RuntimeError("unreachable")


async def on_newfamily_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A person who started the bot and has no code taps 'I'm new', then confirms the spelling."""
    q = update.callback_query
    user = q.from_user
    await q.answer()
    chat_id = q.message.chat_id
    link = context.user_data.get("link")
    if is_owner(user.id) or not link or not link.get("last") or not link.get("first"):
        await context.bot.send_message(chat_id, "Send /start to begin.")
        return
    if await db.users.find_one({"_id": user.id}):
        context.user_data.pop("link", None)
        await context.bot.send_message(chat_id, "You're already in a family. Send /gallery to see the photos.")
        return

    async def show(text: str, markup=None) -> None:
        try:
            await q.edit_message_text(text, reply_markup=markup)
        except TelegramError:
            await context.bot.send_message(chat_id, text, reply_markup=markup)

    if q.data == "nf:yes" and link.get("stage") != "nf":
        q.data = "nf:new"  # an old button: start the spelling check first
    if q.data == "nf:new":
        link["stage"] = "nf"
        await show(
            f"Welcome, {link['first']}! I'll ask {OWNER_NAME} to set up your family.\n\n"
            f"Is the last name spelled right?\n\n👉 {link['last']}",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ Yes, that's right", callback_data="nf:yes")],
                 [InlineKeyboardButton("✏️ No, let me retype it", callback_data="nf:no")]]
            ),
        )
    elif q.data == "nf:no":
        link.update(stage="last")
        link.pop("last", None)
        await show("OK! What's your family's last name?")
    elif q.data == "nf:yes":
        if await pending_newfam(user.id):
            context.user_data.pop("link", None)
            await show(f"Your request is already waiting for {OWNER_NAME}. I'll message you here when it's done. 🙂")
            return
        lock = await locked_for(f"nf:{user.id}") or await locked_for(f"link:{user.id}")
        if lock:
            context.user_data.pop("link", None)
            await show(lock_text(lock))
            return
        if not GROUP_ID or not OWNER_ID:
            context.user_data.pop("link", None)
            await show("Sorry, I can't set up new families yet. Please ask the photographer.")
            return
        if await db.newfam.count_documents({}) >= 30:  # too many waiting at once: don't flood the group
            context.user_data.pop("link", None)
            await show(f"Sorry, {OWNER_NAME} has a lot of requests waiting. Please try again later.")
            return
        tok = secrets.token_hex(4)
        first = " ".join(link["first"].split())[:40]
        last = " ".join(link["last"].split())[:60]
        tg_name = " ".join((getattr(user, "first_name", None) or "?").split())[:40]
        same = await db.families.find({"key": normalize(last)}).to_list()
        rows, listing = [], []
        for i, f in enumerate(same[:3], 1):
            members = await db.users.count_documents({"thread_id": f["thread_id"]})
            made = f["created"].strftime("%b %d, %Y") if f.get("created") else "?"
            listing.append(f"#{i} {f['name']}: {members} {'person' if members == 1 else 'people'}, made {made}")
            rows.append([InlineKeyboardButton(f"➕ Add to #{i} ({f['name']})"[:60], callback_data=f"nfa:e:{tok}:{f['thread_id']}")])
        rows.append([InlineKeyboardButton(f"🆕 Make a new {last[:25]} family"[:60], callback_data=f"nfa:c:{tok}")])
        rows.append([InlineKeyboardButton("❌ Reject", callback_data=f"nfa:r:{tok}")])
        text = (
            "🆕 New family request\n\n"
            f"Name: {first}\nFamily last name: {last}\n"
            f"Telegram: {tg_name}"
            + (f" (@{user.username})" if getattr(user, "username", None) else "") + f", id {user.id}\n"
            + (f"\n⚠️ A family called {last} already exists. Is this person one of them?\n" + "\n".join(listing) if same else "")
        )
        await db.newfam.insert_one(
            {"_id": tok, "uid": user.id, "first": first, "last": last, "username": getattr(user, "username", None),
             "at": datetime.now(timezone.utc)}
        )
        try:
            await post_in_addfam_topic(context.bot, text, InlineKeyboardMarkup(rows))
        except TelegramError:
            log.exception("Couldn't post a new-family request")
            await db.newfam.delete_one({"_id": tok})
            context.user_data.pop("link", None)
            await show("Sorry, I couldn't send your request right now. Please try again later.")
            return
        await record_fail(f"nf:{user.id}")  # a few requests per hour at most (only counted when it was sent)
        context.user_data.pop("link", None)
        await record_try(user, "asked to add a new family", first, last)
        await show(
            f"✅ Sent to {OWNER_NAME}! As soon as your family is set up, I'll message you here with your photos. "
            "You can close this chat."
        )


async def on_newfamily_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The owner answers a request in the '➕ Add families' topic."""
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("This is only for the photographer.", show_alert=True)
        return
    await q.answer()
    parts = q.data.split(":")
    action, tok = parts[1], parts[2]

    async def show(text: str) -> None:
        try:
            await q.edit_message_text(text)
        except TelegramError:
            pass

    r = await db.newfam.find_one({"_id": tok})
    if not r:
        await show("That request was already answered.")
        return
    if action == "r":
        await db.newfam.delete_one({"_id": tok})
        await db.tries.delete_one({"_id": r["uid"]})
        await show(f"❌ Rejected {r['first']} {r['last']}.")
        try:
            await context.bot.send_message(
                r["uid"], f"Sorry, {OWNER_NAME} couldn't set up your family. Please ask them directly."
            )
        except TelegramError:
            pass
        return
    if await db.users.find_one({"_id": r["uid"]}):  # got in another way in the meantime
        await db.newfam.delete_one({"_id": tok})
        await show(f"{r['first']} {r['last']} is already in a family, so this request was dropped.")
        return
    code = r.get("made_code")
    try:
        if action == "c":
            tid = r.get("made_tid")
            if not tid:  # (if an earlier try got this far, don't make a second family)
                tid, code, _ = await make_family(context.bot, r["last"])
                await db.newfam.update_one({"_id": tok}, {"$set": {"made_tid": tid, "made_code": code}})
        else:
            tid = int(parts[3])
            if not await db.families.find_one({"thread_id": tid}):
                await db.newfam.delete_one({"_id": tok})
                await show("That family no longer exists. The request was dropped.")
                return
        problem, messaged = await add_person(context.bot, r["uid"], tid, r["first"])
    except Exception as e:  # the request stays, so tapping again retries
        log.exception("Couldn't finish a new-family request")
        await q.message.reply_text(
            f"Something went wrong ({getattr(e, 'message', None) or type(e).__name__}). The request is still "
            "waiting: tap the button again to retry. If the topic can't be made, check that I'm an admin "
            "with “Manage topics”."
        )
        return
    await db.newfam.delete_one({"_id": tok})
    fam = await db.families.find_one({"thread_id": tid})
    if problem:
        await show(f"{r['first']} {r['last']}: {problem}" + (f"\nThe {fam['name']} family was made (code {code})." if code and fam else ""))
        return
    await show(
        (f"✅ Made the {fam['name']} family (code {code}) and added {r['first']}." if code
         else f"✅ Added {r['first']} to the {fam['name']} family.")
        + ("" if messaged else " (I couldn't message them yet; they'll see the welcome when they open the bot.)")
    )


async def handle_family_member_text(update: Update, context: ContextTypes.DEFAULT_TYPE, fm: dict) -> None:
    """The person adding a relative typed something: a cancel, a @username, or a first / last name."""
    msg = update.message
    fm = await fm_session(update, context)
    if not fm:
        return
    text = (msg.text or "").strip()
    stage = fm.get("stage")
    if text.casefold() == "cancel":
        context.user_data.pop("fm", None)
        await msg.reply_text("Cancelled.", reply_markup=client_keyboard())
        await msg.reply_text("What would you like to see?", reply_markup=await family_menu(fm["thread_id"]))
        return
    if stage == "who":
        await msg.reply_text(FM_NOT_FOUND, reply_markup=fm_pick_keyboard())
    elif stage == "first":
        name = fm_name_ok(text)
        if not name or len(name.split()) > 1:
            await msg.reply_text("Please type just their FIRST name (letters only).", reply_markup=one_button("Cancel", "fm:x"))
            return
        fm.update(first=name, stage="last")
        await msg.reply_text("Step 3: type their LAST name.", reply_markup=one_button("Cancel", "fm:x"))
    elif stage == "last":
        name = fm_name_ok(text)
        if not name:
            await msg.reply_text("Please type their LAST name (letters only).", reply_markup=one_button("Cancel", "fm:x"))
            return
        fm.update(last=name, stage="ok")
        await fm_summary(context, msg.chat_id, fm)
    else:
        await msg.reply_text("Please tap ✅ Yes or ✏️ Start over above.")


def fm_summary_text(fm: dict) -> str:
    return (
        "Please check that everything is right:\n\n"
        f"👤 Telegram: {fm['tg_name']}\n"
        f"🔗 Username: {'@' + fm['username'] if fm.get('username') else 'none'}\n"
        f"📝 Name: {fm['first']} {fm['last']}\n"
        f"🆔 ID: {fm['uid']}\n"
        f"📸 Photo: {'shown above' if fm.get('photo') else 'none'}\n\n"
        "Is this right? They'll have to type that name when they open the bot."
    )


async def fm_summary(context: ContextTypes.DEFAULT_TYPE, chat_id: int, fm: dict) -> None:
    markup = InlineKeyboardMarkup(
        [[_btn("✅ Yes, that's right", "fm:ok")], [_btn("✏️ Start over", "fm:new"), _btn("Cancel", "fm:x")]]
    )
    if fm.get("photo"):
        try:
            await context.bot.send_photo(chat_id, fm["photo"], caption=fm_summary_text(fm)[:1000], reply_markup=markup)
            return
        except TelegramError:
            fm["photo"] = None
    await context.bot.send_message(chat_id, fm_summary_text(fm), reply_markup=markup)


async def fm_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, fm: dict, q) -> None:
    """They tapped 'Yes, that's right': add the relative (waiting for them to confirm their name)."""
    user = update.effective_user
    tid = fm["thread_id"]
    name = f"{fm['first']} {fm['last']}"
    me = await db.users.find_one({"_id": user.id})
    mine = await db.families.find_one({"thread_id": tid})
    by = f"{sug_name(me, user)} {mine['name'] if mine else ''}".strip()
    problem, messaged = await add_person(context.bot, fm["uid"], tid, name, by=by)
    context.user_data.pop("fm", None)
    chat_id = q.message.chat_id
    if problem:
        await context.bot.send_message(chat_id, problem, reply_markup=await family_menu(tid))
        return
    await context.bot.send_message(
        chat_id,
        f"✅ Added {name}. When they open this bot they'll type their first and last name, exactly like "
        "you wrote it, and then they'll see your photos." + (" I messaged them." if messaged else ""),
        reply_markup=await family_menu(tid),
    )
    if OWNER_ID and mine:
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"👪 {name} was added to the {mine['name']} family (added by {by}). Telegram: {fm['tg_name']}"
                + (f" (@{fm['username']})" if fm.get("username") else "") + f", id {fm['uid']}. "
                "They see nothing until they type that name.",
                reply_markup=InlineKeyboardMarkup([[_btn(f"🗑 Remove {name}"[:60], f"a:ul:{tid}:{fm['uid']}")]]),
            )
        except TelegramError:
            pass


async def handle_addperson_text(update: Update, context: ContextTypes.DEFAULT_TYPE, adm: dict) -> None:
    """The owner typed a Telegram ID (and maybe a first name) to add to a family."""
    msg = update.message
    parts = (msg.text or "").split(None, 1)
    if not parts or not (parts[0].isascii() and parts[0].isdigit()) or not (5 <= len(parts[0]) <= 15):
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
            [_btn("📣 Announce", "a:an:0"), _btn("🕒 Scheduled", "a:sc:0")],
            [_btn("📊 Stats", "a:stats")],
        ]
    )


def one_button(text: str, cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(text, cb)]])


def admin_pager(items: list[tuple[str, str]], page: int, nav: str, back: str,
                extra_rows: list | None = None) -> InlineKeyboardMarkup:
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
    rows.extend(extra_rows or [])
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
    best = await db.photos.count_documents({"thread_id": tid, "best": True})
    text = (
        f"🏠 {fam['name']} family\n"
        f"Code: {fam['code']}\n"
        f"Photos: {photos} · People linked: {people} · ⭐ Best shots: {best}"
    )
    markup = InlineKeyboardMarkup(
        [
            [_btn("📤 Add photos", f"a:phf:{tid}"), _btn("🗑 Remove photos", f"a:rmf:{tid}:0")],
            [_btn("👥 Members", f"a:mem:{tid}"), _btn("🔄 New code", f"a:nc:{tid}")],
            [_btn("➕ Add person by ID", f"a:ap:{tid}")],
            [_btn("👁 Browse as this family", f"a:cnf:{tid}"), _btn("❌ Delete family", f"a:df:{tid}")],
            *([[_btn(f"⭐ Clear best shots ({best})", f"a:bsx:{tid}")]] if best else []),
            [_btn("⬅️ Families", "a:fl:0")],
        ]
    )
    return text, markup


async def members_view(tid: int):
    fam = await db.families.find_one({"thread_id": tid})
    if not fam:
        return None
    people = await db.users.find({"thread_id": tid}).sort("first_name", 1).to_list()
    rows = [[_btn(f"Unlink {p['first_name']}"[:60], f"a:ul:{tid}:{p['_id']}")] for p in people[:25]]
    rows.append([_btn("⬅️ Back", f"a:f:{tid}")])
    if not people:
        text = f"No one has linked to the {fam['name']} family yet."
    else:
        lines = [
            f"• {p['first_name']} (id {p['_id']}" + (f", @{p['username']}" if p.get("username") else "") + ")"
            + (f" — added by {p['added_by']}" if p.get("added_by") else "")
            + (" ⏳ hasn't confirmed their name yet" if p.get("pending") else "")
            for p in people[:25]
        ]
        text = f"👥 {fam['name']} family members:\n" + "\n".join(lines)
        if len(people) > 25:
            text += f"\n…and {len(people) - 25} more."
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
    clear_flows(context)
    await update.message.reply_text("👑 Admin panel", reply_markup=admin_home_markup())


async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE, adm: dict) -> None:
    """The owner typed text: an announcement, or a last name after tapping 'Add family'."""
    if adm.get("stage") in ("ann", "ann_ok"):
        await handle_announce_text(update, context, adm)
        return
    if adm.get("stage") == "ann_when":
        when = parse_when(update.message.text or "")
        if not when:
            await update.message.reply_text("I couldn't read that time, or it's already past.\n\n" + WHEN_HELP)
            return
        if await db.scheduled.count_documents({}) >= 50:
            await update.message.reply_text("You already have 50 scheduled. Cancel one first.")
            return
        sid = secrets.token_hex(4)
        await db.scheduled.insert_one(
            {"_id": sid, "aud": adm["aud"], "text": adm["text"], "at": when.astimezone(timezone.utc),
             "claimed": datetime(1970, 1, 1, tzinfo=timezone.utc)}
        )
        context.user_data.pop("adm", None)
        await update.message.reply_text(
            f"✅ Scheduled for {when_label(when)} to {await ann_audience_name(adm['aud'])}.\n"
            "I'll message you when it goes out. (If the bot is asleep then, it goes out as soon as it wakes.)",
            reply_markup=InlineKeyboardMarkup(
                [[_btn("🕒 Scheduled", "a:sc:0")], [_btn("⬅️ Admin panel", "a:home")]]
            ),
        )
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

    elif action == "bsx":  # take every photo out of a family's Best shots
        tid = int(parts[2])
        await db.photos.update_many({"thread_id": tid, "best": True}, {"$set": {"best": False}})
        view = await family_page(tid)
        await show(*view) if view else await show("That family no longer exists.", one_button("⬅️ Families", "a:fl:0"))

    elif action == "trx":  # clear the whole list (just the list: nobody is unlinked or unlocked)
        await db.tries.delete_many({})
        await show(*await tries_view(0))

    elif action == "trd":  # dismiss an attempt from the list
        await db.tries.delete_one({"_id": int(parts[2])})
        await show(*await tries_view(0))

    elif action == "np":  # add a person from the main panel: ID + first name, then family
        clear_flows(context)
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
        clear_flows(context)
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
        clear_flows(context)
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
        sent, failed, total = await broadcast(context.bot, adm["aud"], adm["text"])
        people = range(total)
        log.info("Owner sent an announcement to %s of %s", sent, len(people))
        await show(
            f"✅ Sent to {sent} of {len(people)}."
            + (f" {failed} couldn't be reached (they may have blocked the bot)." if failed else ""),
            one_button("⬅️ Admin panel", "a:home"),
        )

    elif action == "ansc":  # schedule instead of sending now
        adm = context.user_data.get("adm")
        if not adm or adm.get("stage") != "ann_ok":
            await show("There's nothing to schedule. Start again from the admin panel.", one_button("⬅️ Admin panel", "a:home"))
            return
        adm["stage"] = "ann_when"
        await show(WHEN_HELP, one_button("Cancel", "a:home"))

    elif action == "sc":  # list of scheduled announcements
        context.user_data.pop("adm", None)
        text, markup = await scheduled_view(int(parts[2]))
        await show(text, markup)

    elif action in ("scv", "scx"):  # see / cancel one
        doc = await db.scheduled.find_one({"_id": parts[2]})
        if not doc:
            await show("That one already went out or was cancelled.", one_button("⬅️ Scheduled", "a:sc:0"))
            return
        if action == "scx":
            await db.scheduled.delete_one({"_id": parts[2]})
            await show("🗑 Cancelled.", one_button("⬅️ Scheduled", "a:sc:0"))
            return
        at = doc["at"].replace(tzinfo=timezone.utc) if doc["at"].tzinfo is None else doc["at"]
        await show(
            f"🕒 {when_label(at)}\nTo: {await ann_audience_name(doc['aud'])}\n\n{doc['text']}",
            InlineKeyboardMarkup(
                [[_btn("🗑 Cancel this one", f"a:scx:{doc['_id']}")], [_btn("⬅️ Scheduled", "a:sc:0")]]
            ),
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
            await family_menu(tid),
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
        clear_flows(context)
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
        clear_flows(context)
        context.user_data["up"] = {
            "stage": "place", "thread_id": tid, "name": fam["name"], "count": 0,
            "last_mg": None, "until": time.time() + SESSION_SECONDS,
        }
        await show(f"📤 {fam['name']} family.\n{ASK_PLACE}", one_button("Cancel", "a:done"))

    elif action == "done":  # end an upload session started from the panel
        up = context.user_data.pop("up", None)
        count = up.get("count", 0) if up else 0
        back = f"a:f:{up['thread_id']}" if up else "a:home"
        offer, markup = await notify_offer(context, up, q.from_user.id) if up else ("", None)
        rows = [list(r) for r in markup.inline_keyboard] if markup else []
        rows.append([_btn("⬅️ Back", back)])
        await show(
            f"✅ Done. Added {count} file(s). The session is closed." + offer if up else "No upload was running.",
            InlineKeyboardMarkup(rows),
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
                if "not found" in str(e.message).lower():
                    pass  # the topic was already removed by hand, carry on deleting the family
                else:
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
        for _uid, _t in list(context.bot_data.get("preview", {}).items()):
            if _t == tid:  # (nobody is left browsing a family that no longer exists)
                context.bot_data["preview"].pop(_uid, None)
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


# ---------- Mini App: the owner's Admin tab ----------
# webapp.py has already checked that the caller is the owner (Telegram-signed login) before anything here runs.
# Each action reuses the same logic as the chat's /admin buttons.

ANN_LOCK = asyncio.Lock()  # one announcement at a time, so a double tap can't send it twice
ADMIN_ACTIONS = {
    "overview", "family", "photo_best", "photo_remove", "photo_edit", "sugs", "sug_act",
    "ann_preview", "ann_send", "ann_schedule", "scheduled", "sched_cancel",
    "tries", "try_add", "try_dismiss", "try_unlock", "tries_clear",
}


def _int(v, lo: int = -(2 ** 53), hi: int = 2 ** 53) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None


def _safe_date(day) -> str:
    try:
        return pretty_date(day)
    except Exception:
        return str(day or "")


async def _ann_input(req: dict):
    """-> (audience, text, error). The audience is "all" or the topic id of one existing family."""
    aud, text = req.get("aud"), req.get("text")
    if aud != "all":
        tid = _int(aud)
        if tid is None or not await db.families.find_one({"thread_id": tid}):
            return None, None, "Please pick who should get it."
        aud = str(tid)
    if not isinstance(text, str) or not text.strip():
        return None, None, "Please type the announcement."
    text = text.strip()
    if len(text) > ANN_MAX:
        return None, None, f"That's too long ({len(text)} characters). Please keep it under {ANN_MAX}."
    return aud, text, None


async def _send_announcement(bot, aud: str, text: str, who: str) -> None:
    """Sends the announcement in the background (a slow phone connection can't cause a second send), then
    tells the owner how it went. The caller has already taken ANN_LOCK."""
    try:
        sent, failed, total = await broadcast(bot, aud, text)
        log.info("Owner sent an announcement from the app to %s of %s", sent, total)
        if OWNER_ID:
            try:
                await bot.send_message(
                    OWNER_ID,
                    f"✅ Your announcement went out to {who}: {sent} of {total} received it."
                    + (f" {failed} couldn't be reached (they may have blocked the bot)." if failed else ""),
                )
            except TelegramError:
                pass
    except Exception:
        log.exception("Announcement from the app failed")
    finally:
        ANN_LOCK.release()


async def admin_upload(bot, tid: int, place: str | None, day: str, name: str, data: bytes) -> tuple[int, dict]:
    """Posts one uploaded photo into a family's topic (as a File, so the quality is kept) and adds it to the index.
    The caption is built exactly like the chat upload's, so the date and place are read the same way."""
    fam = await db.families.find_one({"thread_id": tid})
    if not fam:
        return 404, {"error": "That family no longer exists."}
    if not GROUP_ID:
        return 400, {"error": "GROUP_ID isn't set yet, so I can't add photos to the group."}
    when = "No date" if day == UNKNOWN_DATE else day
    caption = f"{when} | {place}" if place else when
    try:
        sent = await send_with_retry(
            lambda: bot.send_document(GROUP_ID, document=data, filename=name, message_thread_id=tid,
                                      caption=caption, disable_content_type_detection=True)
        )
    except (TimedOut, NetworkError):
        log.warning("Timed out adding an uploaded photo")
        return 504, {"error": "Telegram was too slow. That photo may or may not have been added: look in the "
                              "family's topic before sending it again.", "maybe": True}
    except (TelegramError, RuntimeError):
        log.exception("Couldn't add an uploaded photo")
        return 502, {"error": "I couldn't add that photo to the group. Check that I'm still an admin there "
                              "and that the family's topic still exists."}
    for attempt in range(3):  # the photo is already in the group: never report a failure (a retry would post it twice)
        try:
            await index_file(tid, sent.message_id, sent.document.file_id, caption, None, sent.date, "document", thumb_of(sent))
            break
        except Exception:
            log.exception("Couldn't index an uploaded photo (try %s)", attempt + 1)
            await asyncio.sleep(1)
    return 200, {"ok": True, "id": sent.message_id}


async def admin_api(bot, action: str, req: dict) -> tuple[int, dict]:
    try:
        return await _admin_api(bot, action, req)
    except Exception:
        log.exception("Admin action failed: %s", action)
        return 500, {"error": "Something went wrong. Please try again."}


async def _admin_api(bot, action: str, req: dict) -> tuple[int, dict]:
    bad = (400, {"error": "Bad request"})
    gone = (404, {"error": "That no longer exists."})
    now = datetime.now(timezone.utc)

    if action == "overview":
        fams = await db.families.find().sort("name", 1).to_list()

        async def per_family(coll, match=None):
            pipe = ([{"$match": match}] if match else []) + [{"$group": {"_id": "$thread_id", "n": {"$sum": 1}}}]
            return {d["_id"]: d["n"] for d in await (await coll.aggregate(pipe)).to_list()}

        photos = await per_family(db.photos)
        best = await per_family(db.photos, {"best": True})
        people = await per_family(db.users)
        sugs = await db.suggestions.find({"status": "pending"}).to_list()
        waiting: dict = {}
        for d in sugs:
            waiting[d.get("thread_id")] = waiting.get(d.get("thread_id"), 0) + 1
        return 200, {
            "stats": {
                "families": len(fams), "people": sum(people.values()), "photos": sum(photos.values()),
                "new7": await db.photos.count_documents({"sug": {"$ne": True}, "added": {"$gte": now - timedelta(days=7)}}),
                "sugs": len(sugs), "scheduled": await db.scheduled.count_documents({}),
                "tries": await db.tries.count_documents({}),
            },
            "families": [
                {"tid": f["thread_id"], "name": f.get("name", "?"), "code": f.get("code", ""), "photos": photos.get(f["thread_id"], 0),
                 "people": people.get(f["thread_id"], 0), "best": best.get(f["thread_id"], 0),
                 "sugs": waiting.get(f["thread_id"], 0)}
                for f in fams[:300]
            ],
        }

    if action == "family":
        tid = _int(req.get("tid"))
        fam = await db.families.find_one({"thread_id": tid}) if tid is not None else None
        if not fam:
            return gone
        people = await db.users.find({"thread_id": tid}).sort("first_name", 1).to_list()
        return 200, {
            "tid": tid, "name": fam["name"], "code": fam["code"],
            "photos": await db.photos.count_documents({"thread_id": tid}),
            "best": await db.photos.count_documents({"thread_id": tid, "best": True}),
            "people": [{"id": p["_id"], "first": p.get("first_name") or "", "username": p.get("username") or "",
                        "by": p.get("added_by") or "", "pending": bool(p.get("pending"))} for p in people[:60]],
        }

    if action in ("photo_best", "photo_remove", "photo_edit"):
        mid = _int(req.get("id"))
        doc = await db.photos.find_one({"message_id": mid}) if mid is not None else None
        if not doc:
            return gone
        if action == "photo_best":
            want = req.get("best")
            now_best = want if isinstance(want, bool) else not doc.get("best", False)
            await db.photos.update_one({"message_id": mid}, {"$set": {"best": now_best}})
            return 200, {"best": now_best}
        if action == "photo_remove":
            if not GROUP_ID:
                return 400, {"error": "GROUP_ID isn't set yet, so I can't delete from the group."}
            problem = await delete_photo_everywhere(bot, mid)
            return (502, {"error": problem}) if problem else (200, {"ok": True})
        day = parse_date_answer(req["date"]) if isinstance(req.get("date"), str) else None
        place = req.get("place")
        if day is None or not (place is None or isinstance(place, str)):
            return 400, {"error": "Please give a date like 2026-07-04 (not in the future), or unknown."}
        place = " ".join((place or "").split())[:80] or None
        if place and place.casefold() in UNKNOWN_WORDS:
            place = None
        await db.photos.update_one(
            {"message_id": mid},
            {"$set": {"date": day, "location": place, "loc_id": loc_id(place), "has_caption": True}},
        )
        return 200, {"d": day, "dt": _safe_date(day), "loc": place or ""}

    if action == "sugs":
        fams = {f["thread_id"]: f["name"] for f in await db.families.find().to_list()}
        docs = await db.suggestions.find({"status": "pending"}).sort("created", 1).to_list()
        return 200, {"rows": [
            {"id": d["_id"], "from": d.get("from_name") or "?", "family": fams.get(d["thread_id"], "?"),
             "dt": _safe_date(d.get("date")), "place": d.get("place") or "", "n": len(d.get("items", [])),
             "others": [fams[t] for t in d.get("others", []) if t in fams], "at": fmt_when(d.get("created"))}
            for d in docs[:50]]}

    if action == "sug_act":
        sid, act = req.get("id"), req.get("action")
        if not isinstance(sid, str) or not re.fullmatch(r"[0-9a-f]{4,20}", sid) or act not in ("y", "o", "n"):
            return bad
        said: list[str] = []
        sdoc = await db.suggestions.find_one({"_id": sid})
        prompt_id = (sdoc or {}).get("prompt_id")

        async def edit(text: str, markup=None) -> None:
            said.append(text)
            if prompt_id and GROUP_ID:  # keep the group's message in step, exactly like the chat buttons do
                try:
                    await bot.edit_message_text(text, chat_id=GROUP_ID, message_id=prompt_id, reply_markup=markup)
                except TelegramError:
                    pass

        await process_suggestion(bot, sid, act, edit)
        return 200, {"message": (said[-1] if said else "Done.")[:240]}

    if action in ("ann_preview", "ann_send", "ann_schedule"):
        aud, text, problem = await _ann_input(req)
        if problem:
            return 400, {"error": problem}
        people = await ann_recipients(aud)
        who = await ann_audience_name(aud)
        if action == "ann_preview":
            return 200, {"count": len(people), "who": who, "tz": str(TZ)}
        if not people:
            return 400, {"error": "Nobody is in that audience yet."}
        if action == "ann_send":
            if ANN_LOCK.locked():
                return 409, {"error": "An announcement is already being sent. Please wait a moment."}
            await ANN_LOCK.acquire()  # (taken right here, so a second tap can't slip in before the job starts)
            task = asyncio.create_task(_send_announcement(bot, aud, text, who))
            BG_TASKS.add(task)
            task.add_done_callback(BG_TASKS.discard)
            return 200, {"started": True, "count": len(people), "who": who}
        when_s = req.get("when")
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2}) (\d{1,2}:\d{2})(?::\d{2})?", when_s.strip()) if isinstance(when_s, str) else None
        if not m:
            return 400, {"error": "Please pick a date and time."}
        when = parse_when(f"{m.group(1)} {m.group(2)}")
        if not when:
            return 400, {"error": "That time has already passed (or is too far away). Please pick a later time."}
        if await db.scheduled.count_documents({}) >= 50:
            return 400, {"error": "You already have 50 scheduled. Cancel one first."}
        await db.scheduled.insert_one(
            {"_id": secrets.token_hex(4), "aud": aud, "text": text, "at": when.astimezone(timezone.utc),
             "claimed": datetime(1970, 1, 1, tzinfo=timezone.utc)}
        )
        return 200, {"label": when_label(when), "who": who}

    if action == "scheduled":
        docs = await db.scheduled.find().sort("at", 1).to_list()
        rows = []
        for d in docs[:60]:
            at = d["at"].replace(tzinfo=timezone.utc) if d["at"].tzinfo is None else d["at"]
            rows.append({"id": d["_id"], "label": when_label(at), "who": await ann_audience_name(d["aud"]),
                         "text": d["text"]})
        return 200, {"rows": rows}

    if action == "sched_cancel":
        sid = req.get("id")
        if not isinstance(sid, str) or not re.fullmatch(r"[0-9a-f]{4,20}", sid):
            return bad
        await db.scheduled.delete_one({"_id": sid})
        return 200, {"ok": True}

    if action == "tries":
        rows = await db.tries.find().sort("at", -1).to_list()
        out = []
        for t in rows[:100]:
            lock = await locked_for(f"link:{t['_id']}")
            guess = None
            if t.get("last"):
                found = await db.families.find({"key": normalize(t["last"])}).to_list()
                if len(found) == 1:
                    guess = {"tid": found[0]["thread_id"], "name": found[0]["name"]}
            out.append({
                "uid": t["_id"], "label": try_label(t), "tg": t.get("tg_name") or "", "username": t.get("username") or "",
                "first": t.get("first") or "", "last": t.get("last") or "", "reason": t.get("reason", "?"),
                "count": t.get("count", 1), "at": fmt_when(t.get("at")), "lock": max(1, -(-lock // 60)) if lock else 0,
                "guess": guess,
            })
        return 200, {"rows": out}

    if action in ("try_add", "try_dismiss", "try_unlock"):
        uid = _int(req.get("uid"), 1)
        if uid is None:
            return bad
        if action == "try_dismiss":
            await db.tries.delete_one({"_id": uid})
            return 200, {"ok": True}
        if action == "try_unlock":
            await clear_fails(f"link:{uid}")
            return 200, {"ok": True}
        tid = _int(req.get("tid"))
        fam = await db.families.find_one({"thread_id": tid}) if tid is not None else None
        if not fam:
            return gone
        t = await db.tries.find_one({"_id": uid})
        if not t:
            return gone  # only people on the login-attempts list can be added from here
        if is_owner(uid):
            return 400, {"error": "That's your own account."}
        name = t.get("first") or t.get("tg_name") or "Friend"
        problem, messaged = await add_person(bot, uid, tid, name)
        if problem:
            return 400, {"error": problem}
        return 200, {"ok": True, "message": f"Added {name} to the {fam['name']} family. "
                     + ("They were told in the bot." if messaged else
                        "I couldn't message them yet; they'll see the welcome when they open the bot.")}

    if action == "tries_clear":
        await db.tries.delete_many({})
        return 200, {"ok": True}

    return 404, {"error": "Not found"}


class WebDeps:
    """What the Mini App's web server needs from the bot (see webapp.WebApp)."""

    token = BOT_TOKEN
    copyright = COPYRIGHT
    owner_name = OWNER_NAME

    def __init__(self, app: Application) -> None:
        self.app = app

    def db(self):
        return db

    def bot(self):
        return self.app.bot

    def is_owner(self, uid: int) -> bool:
        return is_owner(uid)

    async def admin(self, action: str, req: dict) -> tuple[int, dict]:
        return await admin_api(self.app.bot, action, req)

    admin_actions = ADMIN_ACTIONS

    async def upload_photo(self, tid: int, place, day: str, name: str, data: bytes) -> tuple[int, dict]:
        return await admin_upload(self.app.bot, tid, place, day, name, data)

    def parse_date(self, text: str):
        d = parse_date_answer(text)
        if d is None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text.strip()):
            try:  # the owner's own day can be a day ahead of the bot's time zone
                if date.fromisoformat(text.strip()) <= datetime.now(TZ).date() + timedelta(days=1):
                    return text.strip()
            except ValueError:
                pass
        return d

    unknown_words = UNKNOWN_WORDS

    async def suggestion_item(self, sid: str, i: int):
        """One photo of a suggestion that is still waiting for the owner's answer (for the Admin inbox)."""
        doc = await db.suggestions.find_one({"_id": sid, "status": "pending"})
        items = (doc or {}).get("items") or []
        return items[i] if isinstance(i, int) and 0 <= i < len(items) else None

    def preview_family(self, uid: int) -> int | None:
        """The family the owner picked with /connect (in memory only)."""
        return self.app.bot_data.get("preview", {}).get(uid)

    async def has_best(self, tid: int) -> bool:
        return await has_best(tid)

    def help_text(self) -> str:
        return help_text(True)

    def pretty_date(self, day: str) -> str:
        return pretty_date(day)

    async def family_name(self, tid: int) -> str:
        fam = await db.families.find_one({"thread_id": tid})
        return (fam or {}).get("name", "")

    async def send_original(self, uid: int, doc: dict) -> bool:
        """The app's "Send me the photo in chat" button: the full-quality file, in the person's own chat."""
        caption = (
            pretty_date(doc["date"])
            + (f" · {doc['location']}" if doc.get("location") else "")
            + (f" · Added by {doc['by']}" if doc.get("by") else "")
            + f"\n\n{COPYRIGHT_WARNING}"
        )
        is_photo = doc.get("kind") == "photo"
        send_one = self.app.bot.send_photo if is_photo else self.app.bot.send_document
        arg = {"photo": doc["file_id"]} if is_photo else {"document": doc["file_id"]}
        try:
            await send_with_retry(
                lambda: send_one(uid, caption=caption[:1000], protect_content=PROTECT_CONTENT, **arg),
                retry_timeouts=True,
            )
            return True
        except (TelegramError, RuntimeError):
            log.exception("Couldn't send a photo from the app")
            return False


async def start_web(app: Application) -> None:
    """The web server: the Mini App, plus the little "ok" page UptimeRobot pings.
    It runs inside the bot's own event loop. Hosts like Render give the port in $PORT."""
    port = os.getenv("PORT")
    if not port:
        return
    if webapp is None:
        log.error("webapp.py is missing next to bot.py, so the photo app is OFF. Upload webapp.py and app.html to GitHub.")
        try:  # still answer the health check, so Render and UptimeRobot are happy
            async def ok(reader, writer):
                try:
                    await asyncio.wait_for(reader.read(2048), 5)
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                    await writer.drain()
                except Exception:
                    pass
                writer.close()

            app.bot_data["web"] = await asyncio.start_server(ok, "0.0.0.0", int(port))
        except (OSError, ValueError, OverflowError):
            log.exception("Couldn't start the health server on port %s", port)
        return
    try:
        app.bot_data["web"] = await webapp.WebApp(WebDeps(app)).start(int(port))
    except (OSError, ValueError, OverflowError):
        log.exception("Couldn't start the web server on port %s (the bot still works)", port)


async def safe_index(make) -> None:
    try:
        await make()
    except Exception:  # an old index with different options must not stop the bot from starting
        log.exception("Couldn't create a database index (the bot will still work)")


async def post_shutdown(app: Application) -> None:
    task = app.bot_data.get("scheduler")
    if task:
        task.cancel()
    web = app.bot_data.get("web")
    if web:
        web.close()
    if db is not None:
        try:
            await db.client.close()
        except Exception:
            pass


async def post_init(app: Application) -> None:
    global db
    db = AsyncMongoClient(MONGODB_URI)[DB_NAME]  # (made lazily; no connection yet)
    await start_web(app)  # then the web server, so the host sees the port open right away
    await safe_index(lambda: db.families.create_index("thread_id", unique=True))
    await safe_index(lambda: db.families.create_index("code", unique=True))
    await safe_index(lambda: db.families.create_index("key"))
    await safe_index(lambda: db.photos.create_index("message_id", unique=True))
    await safe_index(lambda: db.photos.create_index([("thread_id", 1), ("date", 1)]))
    await safe_index(lambda: db.photos.create_index([("thread_id", 1), ("loc_id", 1)]))
    await safe_index(lambda: db.photos.create_index("media_group_id"))
    await safe_index(lambda: db.photos.create_index([("thread_id", 1), ("best", 1)]))
    await safe_index(lambda: db.photos.create_index([("thread_id", 1), ("added", -1)]))
    await safe_index(lambda: db.users.create_index("thread_id"))
    await safe_index(lambda: db.tries.create_index("at", expireAfterSeconds=2592000))  # forget login attempts after 30 days
    await safe_index(lambda: db.sent.create_index("at", expireAfterSeconds=172800))  # forget hide-buttons after 2 days
    app.bot_data["scheduler"] = asyncio.create_task(scheduler_loop(app))
    if WEBAPP_URL and not AUTO_MENU_BUTTON:
        log.info("AUTO_MENU_BUTTON is off: the blue Open button is yours to set in BotFather (the 📱 menu button still works).")
    elif WEBAPP_URL:  # the blue "Open" button next to the message box, for everyone who opens the bot
        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="📷 Photos", web_app=WebAppInfo(url=f"{WEBAPP_URL}/app"))
            )
        except TelegramError:
            log.exception("Couldn't set the bot's Open button (the 📱 button in the menu still works)")
    else:
        log.warning("WEBAPP_URL / RENDER_EXTERNAL_URL is not set (or isn't https): the photo app is off.")
    log.info("Connected to MongoDB database '%s'", DB_NAME)


def main() -> None:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
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
        app.add_handler(CommandHandler("best", best_cmd, filters=grp))
        app.add_handler(MessageHandler(grp & (filters.PHOTO | filters.Document.IMAGE), on_group_media))
        app.add_handler(CallbackQueryHandler(on_remove_button, pattern=r"^rm:(y|n):\d+$"))
        app.add_handler(CallbackQueryHandler(on_suggestion_button, pattern=r"^sa:(y|o|n):[0-9a-f]{10}$"))
    else:
        log.warning("GROUP_ID is not set: send /id in your group, then put it in the environment.")

    priv = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE
    app.add_handler(CommandHandler("start", start, filters=priv))
    app.add_handler(CommandHandler(["menu", "gallery"], menu, filters=priv))
    app.add_handler(CommandHandler("connect", connect_cmd, filters=priv))
    app.add_handler(CommandHandler("help", help_cmd, filters=priv))
    app.add_handler(CallbackQueryHandler(on_newfamily_button, pattern=r"^nf:(new|yes|no)$"))
    app.add_handler(CallbackQueryHandler(on_newfamily_owner, pattern=r"^nfa:(c|r|e):[0-9a-f]{8}(:\d+)?$"))
    app.add_handler(CallbackQueryHandler(on_notify_button, pattern=r"^nt:(y|n):[0-9a-f]{8}$"))
    app.add_handler(CommandHandler("announce", announce_cmd, filters=priv))
    app.add_handler(CommandHandler("unlink", unlink_private, filters=priv))
    app.add_handler(CommandHandler("admin", admin_cmd, filters=priv))
    app.add_handler(CommandHandler("addphotos", addphotos, filters=priv))
    app.add_handler(CommandHandler("done", done, filters=priv))
    app.add_handler(MessageHandler(priv & filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, on_private_text))
    app.add_handler(MessageHandler(priv & filters.StatusUpdate.USERS_SHARED, on_users_shared))
    app.add_handler(MessageHandler(priv & filters.UpdateType.MESSAGE & filters.CONTACT, on_contact_shared))
    app.add_handler(MessageHandler(
            priv & filters.UpdateType.MESSAGE & (filters.PHOTO | filters.Document.ALL | filters.VIDEO),
            on_private_media,
        ))
    app.add_handler(CallbackQueryHandler(on_admin_button, pattern=r"^a:"))
    app.add_handler(
        CallbackQueryHandler(on_button, pattern=r"^(menu|all|d:\d+|l:\d+|D:\d{4}-\d{2}-\d{2}|L:[0-9a-f]{10}|L:none|x:\d+|bs|sg:(new|send|gal)|fm:(new|x|ok))$")
    )
    app.add_error_handler(on_error)

    log.info("Bot running")
    app.run_polling()


if __name__ == "__main__":
    main()
