"""VK front for the photo bot (families in Russia can look at their photos in VK).

The photos stay in Telegram (that is where the photographer uploads). This file lets a VK community chat show the
same galleries to the same families: link with the family code (or a one-person /invite link), see photos by date,
by place, all, best shots and favourites, and get the originals as files.

It turns on only when VK_TOKEN and VK_GROUP_ID are set. It uses VK's "Bots Long Poll", so it needs no public address,
and plain HTTP calls (httpx, which python-telegram-bot already installs). If anything goes wrong here the Telegram
bot keeps working: this runs as its own background task and never raises into the Telegram side.
"""
import asyncio
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

try:
    import httpx  # (python-telegram-bot installs it)
    HTTP_ERROR = httpx.HTTPError
except ImportError:  # (only in the offline test sandbox)
    httpx = None
    HTTP_ERROR = OSError

log = logging.getLogger("photobot.vk")

VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
VK_GROUP_ID = os.environ.get("VK_GROUP_ID", "").strip().lstrip("-")
API_URL = "https://api.vk.com/method/"
API_V = "5.199"
PAGE = 8  # photos per batch
DATES_PER_PAGE = 8
UP_GATE = asyncio.Semaphore(2)  # (at most two uploads to VK at a time)

B = None  # the bot module (database, family rules), set by run()
_http = None
_locks: dict[int, asyncio.Lock] = {}
_tasks: set = set()  # (keeps running handlers from being garbage-collected)

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
COPYRIGHT_RU = "⚠️ Фото защищены авторским правом. Пожалуйста, не публикуйте и не пересылайте их без разрешения."


def enabled() -> bool:
    return bool(VK_TOKEN and VK_GROUP_ID)


class VKError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(f"VK error {code}: {msg}")
        self.code = code


def ru_date(day: str) -> str:
    try:
        y, m, d = (int(x) for x in day.split("-"))
        return f"{d} {MONTHS[m - 1]} {y}"
    except Exception:
        return "Без даты"


# ---------- talking to VK ----------

async def call(method: str, **params):
    """One VK API call. Returns the 'response' part; raises VKError."""
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=40)
    data = {k: v for k, v in params.items() if v is not None}
    data.update(access_token=VK_TOKEN, v=API_V)
    for attempt in range(3):
        r = await _http.post(API_URL + method, data=data)
        j = r.json()
        err = j.get("error")
        if not err:
            return j.get("response")
        if err.get("error_code") == 6 and attempt < 2:  # too many requests per second
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        raise VKError(err.get("error_code"), err.get("error_msg"))


async def upload(url: str, field: str, name: str, data: bytes) -> dict:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=60)
    r = await _http.post(url, files={field: (name, data)})
    return r.json()


async def send(peer: int, text: str, keyboard: dict | None = None, attachment: str | None = None) -> None:
    await call("messages.send", peer_id=peer, random_id=secrets.randbits(31), message=text[:4000],
               keyboard=json.dumps(keyboard, ensure_ascii=False) if keyboard else None, attachment=attachment, dont_parse_links=1)


# ---------- keyboards ----------

def text_btn(label: str, cmd: str, color: str = "secondary") -> dict:
    return {"action": {"type": "text", "label": label[:40], "payload": json.dumps({"c": cmd})}, "color": color}


def cb_btn(label: str, payload: dict, color: str = "secondary") -> dict:
    return {"action": {"type": "callback", "label": label[:40], "payload": json.dumps(payload, separators=(",", ":"), ensure_ascii=False)}, "color": color}


def link_btn(label: str, url: str) -> dict:
    return {"action": {"type": "open_link", "link": url, "label": label[:40]}}


async def main_keyboard(tid: int) -> dict:
    rows = [[text_btn("📅 По датам", "dates", "primary"), text_btn("📍 По местам", "locs", "primary")],
            [text_btn("📷 Все фото", "all", "primary")]]
    second = []
    if await B.has_best(tid):
        second.append(text_btn("⭐ Лучшие", "best"))
    second.append(text_btn("♥ Избранное", "favs"))
    rows.append(second)
    fam = await B.db.families.find_one({"thread_id": tid})
    url = B.web_link_url(fam) if fam else ""
    last = [text_btn("❓ Помощь", "help")]
    if url:
        last.insert(0, link_btn("🌐 Сайт", url))
    rows.append(last)
    return {"one_time": False, "buttons": rows}


HELP = (
    "ℹ️ Как это работает\n\n"
    "📷 Все фото, 📅 По датам и 📍 По местам показывают фотографии вашей семьи.\n"
    "⭐ Лучшие (если есть) — снимки, которые выбрал фотограф.\n"
    "♥ Под фото нажмите номер, чтобы добавить его в избранное; «♥ Избранное» покажет их все.\n"
    "📥 «Оригиналы» присылает файлы в полном качестве.\n"
    "🎬 Видео смотрите на сайте (кнопка «🌐 Сайт»).\n\n"
    + COPYRIGHT_RU
)


# ---------- getting photos to VK ----------

async def tg_download(file_id: str) -> bytes | None:
    try:
        tgfile = await asyncio.wait_for(B.tg_bot().get_file(file_id), 30)
        return bytes(await asyncio.wait_for(tgfile.download_as_bytearray(), 90))
    except Exception as e:  # too big for bots (20 MB), deleted, or Telegram was slow
        log.warning("Couldn't fetch a file from Telegram: %s", e)
        return None


async def vk_photo(doc: dict, peer: int) -> str | None:
    """The VK attachment for a photo (uploaded the first time, then remembered)."""
    key = f"p{doc['message_id']}"
    hit = await B.db.vkcache.find_one({"_id": key})
    if hit:
        return hit["att"]
    async with UP_GATE:
        hit = await B.db.vkcache.find_one({"_id": key})
        if hit:
            return hit["att"]
        data = await tg_download(doc["file_id"])
        if not data:
            return None
        try:
            srv = await call("photos.getMessagesUploadServer", peer_id=peer)
            up = await upload(srv["upload_url"], "photo", "photo.jpg", data)
            saved = await call("photos.saveMessagesPhoto", photo=up.get("photo"), server=up.get("server"), hash=up.get("hash"))
            p = saved[0]
            att = f"photo{p['owner_id']}_{p['id']}" + (f"_{p['access_key']}" if p.get("access_key") else "")
        except (VKError, KeyError, IndexError, TypeError, ValueError, HTTP_ERROR) as e:
            log.warning("Uploading a photo to VK failed: %s", e)
            return None
    await B.db.vkcache.update_one({"_id": key}, {"$set": {"att": att, "at": datetime.now(timezone.utc)}}, upsert=True)
    return att


async def vk_doc(doc: dict, peer: int) -> str | None:
    """The VK attachment for the full-quality file of a photo."""
    key = f"d{doc['message_id']}"
    hit = await B.db.vkcache.find_one({"_id": key})
    if hit:
        return hit["att"]
    async with UP_GATE:
        data = await tg_download(doc["file_id"])
        if not data:
            return None
        try:
            srv = await call("docs.getMessagesUploadServer", type="doc", peer_id=peer)
            up = await upload(srv["upload_url"], "file", f"photo_{doc.get('date', '')}_{doc['message_id']}.jpg", data)
            saved = await call("docs.save", file=up.get("file"), title=f"{doc.get('date', 'photo')}_{doc['message_id']}.jpg")
            d = saved["doc"]
            att = f"doc{d['owner_id']}_{d['id']}" + (f"_{d['access_key']}" if d.get("access_key") else "")
        except (VKError, KeyError, TypeError, ValueError, HTTP_ERROR) as e:
            log.warning("Uploading a file to VK failed: %s", e)
            return None
    await B.db.vkcache.update_one({"_id": key}, {"$set": {"att": att, "at": datetime.now(timezone.utc)}}, upsert=True)
    return att


# ---------- galleries ----------

async def build_query(spec: str, tid: int, uid: int):
    """(query, ascending) for a gallery spec: a (all), b (best), f (favourites), d:DATE, l:PLACE."""
    if spec == "b":
        return await B.scope(tid, best=True), False
    if spec == "f":
        mids = [f["mid"] for f in await B.db.vkfavs.find({"uid": uid}).to_list()]
        return await B.scope(tid, message_id={"$in": mids}), False
    if spec.startswith("d:"):
        return await B.scope(tid, date=spec[2:]), True
    if spec.startswith("l:"):
        return await B.scope(tid, loc_id=spec[2:]), True
    return await B.scope(tid), False


async def show_photos(peer: int, tid: int, spec: str, offset: int = 0) -> None:
    uid = peer
    try:  # ("typing…": the first time a batch is copied to VK it takes a few seconds)
        await call("messages.setActivity", peer_id=peer, type="typing")
    except (VKError, HTTP_ERROR):
        pass
    query, asc = await build_query(spec, tid, uid)
    order = 1 if asc else -1
    docs = await B.db.photos.find(query).sort([("date", order), ("message_id", order)]).skip(offset).limit(PAGE + 1).to_list()
    if not docs:
        await send(peer, "Пока здесь нет фото. Загляните позже! 📸" if offset == 0 else "Это всё! 📸", await main_keyboard(tid))
        return
    more = len(docs) > PAGE
    docs = docs[:PAGE]
    total = await B.db.photos.count_documents(query)
    atts, lines, failed, videos = [], [], 0, 0
    shown = []
    for d in docs:
        n = len(shown) + 1
        label = ru_date(d["date"]) + (f" · {d['location']}" if d.get("location") else "")
        if d.get("kind") == "video":
            videos += 1
            lines.append(f"🎬 {label} (видео — на сайте)")
            continue
        att = await vk_photo(d, peer)
        if not att:
            failed += 1
            continue
        atts.append(att)
        shown.append(d)
        lines.append(f"{n}. {label}")
    head = f"📷 {offset + 1}–{offset + len(docs)} из {total}"
    text = head + "\n" + "\n".join(lines)
    if failed:
        text += f"\n⚠️ Не удалось загрузить фото: {failed}. Попробуйте ещё раз позже."
    favs = {f["mid"] for f in await B.db.vkfavs.find({"uid": uid}).to_list()}
    rows, row = [], []
    for i, d in enumerate(shown, 1):
        row.append(cb_btn(("♥ " if d["message_id"] in favs else "♡ ") + str(i), {"c": "fav", "m": d["message_id"]}))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if more:
        nav.append(cb_btn("➡️ Ещё", {"c": "more", "s": spec, "o": offset + PAGE}, "primary"))
    if shown:
        nav.append(cb_btn("📥 Оригиналы", {"c": "orig", "ms": [d["message_id"] for d in shown]}))
    if nav:
        rows.append(nav)
    kb = {"inline": True, "buttons": rows} if rows else None
    for i in range(0, max(len(atts), 1), 10):
        chunk = atts[i:i + 10]
        await send(peer, text if i == 0 else "…", kb if i == 0 and len(atts) <= 10 else None, ",".join(chunk) or None)
    if not more:
        await send(peer, "Это всё! 📸\n\n" + COPYRIGHT_RU, await main_keyboard(tid))


async def show_dates(peer: int, tid: int, page: int = 0) -> None:
    cur = await B.db.photos.aggregate([{"$match": await B.scope(tid)}, {"$group": {"_id": "$date", "n": {"$sum": 1}}}, {"$sort": {"_id": -1}}])
    rows = await cur.to_list()
    if not rows:
        await send(peer, "Пока нет фото. Загляните позже! 📸", await main_keyboard(tid))
        return
    await send(peer, "Выберите дату:", pick_keyboard([(f"{ru_date(r['_id'])} ({r['n']})", f"d:{r['_id']}") for r in rows], page, "dates"))


async def show_locs(peer: int, tid: int, page: int = 0) -> None:
    cur = await B.db.photos.aggregate([{"$match": await B.scope(tid)}, {"$group": {"_id": "$loc_id", "loc": {"$first": "$location"}, "n": {"$sum": 1}}}])
    rows = await cur.to_list()
    if not rows:
        await send(peer, "Пока нет фото. Загляните позже! 📸", await main_keyboard(tid))
        return
    rows.sort(key=lambda r: (r["_id"] == "none", (r["loc"] or "").casefold()))
    await send(peer, "Выберите место:", pick_keyboard([((r["loc"] or "Без места")[:30] + f" ({r['n']})", f"l:{r['_id']}") for r in rows], page, "locs"))


def pick_keyboard(items: list, page: int, kind: str) -> dict:
    """Up to 8 choices on a page (VK allows 10 buttons on an inline keyboard) plus ⬅️ ➡️."""
    chunk = items[page * DATES_PER_PAGE:(page + 1) * DATES_PER_PAGE]
    rows = []
    for i in range(0, len(chunk), 2):
        rows.append([cb_btn(lbl, {"c": "go", "s": spec}) for lbl, spec in chunk[i:i + 2]])
    nav = []
    if page > 0:
        nav.append(cb_btn("⬅️", {"c": "page", "k": kind, "p": page - 1}))
    if (page + 1) * DATES_PER_PAGE < len(items):
        nav.append(cb_btn("➡️", {"c": "page", "k": kind, "p": page + 1}))
    if nav:
        rows.append(nav)
    return {"inline": True, "buttons": rows}


# ---------- commands from buttons ----------

async def do_command(peer: int, user: dict, cmd: str, p: dict) -> str | None:
    """Runs one button/command. Returns a short note for the little pop-up (callback buttons), or None."""
    tid = user["thread_id"]
    if cmd in ("menu", "start"):
        await send(peer, f"Здравствуйте, {user['first_name']}! Что показать? 👇", await main_keyboard(tid))
    elif cmd == "help":
        await send(peer, HELP, await main_keyboard(tid))
    elif cmd == "all":
        await show_photos(peer, tid, "a")
    elif cmd == "best":
        await show_photos(peer, tid, "b")
    elif cmd == "favs":
        await show_photos(peer, tid, "f")
    elif cmd == "dates":
        await show_dates(peer, tid, 0)
    elif cmd == "locs":
        await show_locs(peer, tid, 0)
    elif cmd == "page":
        await (show_dates if p.get("k") == "dates" else show_locs)(peer, tid, max(0, int(p.get("p", 0))))
    elif cmd == "go":
        await show_photos(peer, tid, str(p.get("s", "a"))[:60], 0)
    elif cmd == "more":
        await show_photos(peer, tid, str(p.get("s", "a"))[:60], max(0, int(p.get("o", 0))))
    elif cmd == "fav":
        mid = p.get("m")
        doc = await B.db.photos.find_one({"message_id": mid}) if isinstance(mid, int) else None
        if not doc or doc.get("thread_id") not in await B.visible_tids(tid) or tid in (doc.get("skip") or []):
            return "Фото не найдено"
        key = f"{peer}:{mid}"
        if await B.db.vkfavs.find_one({"_id": key}):
            await B.db.vkfavs.delete_one({"_id": key})
            return "Убрано из избранного"
        await B.db.vkfavs.update_one({"_id": key}, {"$set": {"uid": peer, "mid": mid, "tid": tid, "at": datetime.now(timezone.utc)}}, upsert=True)
        return "♥ Добавлено в избранное"
    elif cmd == "orig":
        mids = [m for m in (p.get("ms") or []) if isinstance(m, int)][:PAGE]
        docs = [d for d in [await B.db.photos.find_one({"message_id": m}) for m in mids] if d and d.get("thread_id") in await B.visible_tids(tid)]
        atts = [a for a in [await vk_doc(d, peer) for d in docs if d.get("kind") != "video"] if a]
        if not atts:
            return "Не получилось отправить файлы"
        await send(peer, "📥 Оригиналы:\n\n" + COPYRIGHT_RU, None, ",".join(atts))
        return "Отправляю файлы…"
    return None


# ---------- people: linking ----------

async def notify_owner(text: str) -> None:
    try:
        if B.OWNER_ID:
            await B.tg_bot().send_message(B.OWNER_ID, text)
    except Exception:
        pass


async def link_user(peer: int, fam: dict, first: str) -> dict:
    user = {"_id": peer, "thread_id": fam["thread_id"], "first_name": first, "linked_at": datetime.now(timezone.utc)}
    await B.db.vkusers.update_one({"_id": peer}, {"$set": {k: v for k, v in user.items() if k != "_id"}}, upsert=True)
    await B.db.vkstate.delete_one({"_id": peer})
    await B.clear_fails(f"vk:{peer}")
    log.info("VK: linked %s (%s) to family %s", first, peer, fam["name"])
    return user


async def vk_first_name(peer: int) -> str:
    try:
        r = await call("users.get", user_ids=peer)
        return " ".join((r[0].get("first_name") or "").split())[:40] or "Друг"
    except Exception:
        return "Друг"


async def claim_invite(peer: int, tok: str) -> bool:
    """A one-person link (the same /invite links as Telegram, in their VK form)."""
    doc = await B.db.invites.find_one({"_id": tok}) if re.fullmatch(r"[\w-]{8,40}", tok) else None
    exp = doc.get("exp") if doc else None
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if not doc or doc.get("used") or (exp and exp < datetime.now(timezone.utc)):
        await send(peer, f"Эта ссылка больше не работает (она действует один раз и {B.INVITE_HOURS} ч). Попросите у {B.OWNER_NAME} новую.")
        return True
    fam = await B.db.families.find_one({"thread_id": doc["tid"]})
    if not fam:
        await send(peer, f"Эта семья больше не существует. Обратитесь к {B.OWNER_NAME}.")
        return True
    r = await B.db.invites.update_one({"_id": tok, "used": None}, {"$set": {"used": f"vk{peer}"}})
    if r is not None and getattr(r, "modified_count", 1) == 0:
        await send(peer, "Эта ссылка больше не работает. Попросите новую.")
        return True
    first = doc.get("name") or await vk_first_name(peer)
    user = await link_user(peer, fam, first)
    await send(peer, f"Вы в семье, {first}! 🎉 Вот фотографии семьи {fam['name']}.", await main_keyboard(fam["thread_id"]))
    await notify_owner(f"✅ {first} (VK) joined the {fam['name']} family with the link.")
    return True


async def link_step(peer: int, text: str, state: dict | None) -> None:
    text = " ".join((text or "").split())
    stage = (state or {}).get("stage")
    lock = await B.locked_for(f"vk:{peer}")
    if lock:  # (too many wrong codes: nothing works until the hour is over)
        await send(peer, f"Слишком много неверных попыток. Попробуйте через {max(1, -(-lock // 60))} мин.")
        return
    if not stage:
        await B.db.vkstate.update_one({"_id": peer}, {"$set": {"stage": "first", "at": datetime.now(timezone.utc)}}, upsert=True)
        await send(peer, "Здравствуйте! 👋 Давайте найдём ваши фото. Как вас зовут (имя)?")
        return
    if stage == "first":
        if not B.normalize(text) or len(text) > 40:
            await send(peer, "Пожалуйста, напишите только своё имя.")
            return
        await B.db.vkstate.update_one({"_id": peer}, {"$set": {"stage": "last", "first": text}})
        await send(peer, f"Приятно познакомиться, {text}! Какая фамилия у вашей семьи?")
        return
    if stage == "last":
        if not B.normalize(text) or len(text) > 60:
            await send(peer, "Пожалуйста, напишите фамилию семьи.")
            return
        await B.db.vkstate.update_one({"_id": peer}, {"$set": {"stage": "code", "last": text}})
        await send(peer, "Спасибо! Теперь 6-значный код, который вам дали.\n" f"Нет кода? Попросите у {B.OWNER_NAME}.")
        return
    # stage == code
    key = f"vk:{peer}"
    lock = await B.locked_for(key)
    if lock:
        await send(peer, f"Слишком много неверных попыток. Попробуйте через {max(1, -(-lock // 60))} мин.")
        return
    code = re.sub(r"\D", "", text)
    fam = near = None
    if len(code) == 6:
        fam = await B.db.families.find_one({"key": B.normalize(state["last"]), "code": code})
        if not fam:
            near = await B.db.families.find_one({"code": code})
            if near and B.name_close(state["last"], near["name"]):
                fam, near = near, None
    if not fam:
        locked = await B.record_fail(key)
        await B.db.vkstate.delete_one({"_id": peer})
        if near and not locked:
            await notify_owner(f"⚠️ {state['first']} {state['last']} (VK id {peer}) typed the right code of the {near['name']} family but wrote the last name as “{state['last']}”.")
        if locked:
            await notify_owner(f"🔒 {state['first']} {state['last']} (VK id {peer}) entered wrong details {B.MAX_FAILS} times and is locked out for an hour.")
        await send(peer, "Эти данные не подошли ни к одной семье." + (" Попробуйте позже." if locked else " Напишите что-нибудь, чтобы начать заново."))
        return
    user = await link_user(peer, fam, state["first"])
    await send(peer, f"Вы в семье, {state['first']}! 🎉 Вот фотографии семьи {fam['name']}.", await main_keyboard(fam["thread_id"]))


MENU_WORDS = {"меню", "начать", "start", "menu", "/start", "привет", "hi"}


async def on_message(m: dict) -> None:
    peer = m.get("from_id")
    if not isinstance(peer, int) or peer <= 0 or m.get("peer_id") != peer:  # only private chats with people
        return
    user = await B.db.vkusers.find_one({"_id": peer})
    ref = str(m.get("ref") or "")
    if ref.startswith("inv_") and not user:
        await claim_invite(peer, ref[4:])
        return
    if not user:
        await link_step(peer, m.get("text") or "", await B.db.vkstate.find_one({"_id": peer}))
        return
    try:
        p = json.loads(m.get("payload") or "{}")
    except ValueError:
        p = {}
    cmd = p.get("c") if isinstance(p, dict) else None
    if not cmd:
        t = (m.get("text") or "").strip().lower()
        cmd = "menu" if t in MENU_WORDS or not t else "help" if t in ("помощь", "help", "?") else "menu"
    await do_command(peer, user, cmd, p if isinstance(p, dict) else {})


async def on_event(e: dict) -> None:
    """A tap on an inline (callback) button."""
    peer, payload = e.get("user_id"), e.get("payload")
    if not isinstance(peer, int) or not isinstance(payload, dict):
        return
    user = await B.db.vkusers.find_one({"_id": peer})
    note = None
    if user:
        note = await do_command(peer, user, payload.get("c"), payload)
    try:
        await call("messages.sendMessageEventAnswer", event_id=e.get("event_id"), user_id=peer, peer_id=e.get("peer_id", peer),
                   event_data=json.dumps({"type": "show_snackbar", "text": (note or "Готово")[:90]}, ensure_ascii=False))
    except VKError:
        pass


async def handle(update: dict) -> None:
    obj = update.get("object") or {}
    who = (obj.get("message") or {}).get("from_id") if update.get("type") == "message_new" else obj.get("user_id")
    lock = _locks.setdefault(who if isinstance(who, int) else 0, asyncio.Lock())  # (one person's taps are handled in order)
    async with lock:
        try:
            if update.get("type") == "message_new":
                await on_message(obj.get("message") or {})
            elif update.get("type") == "message_event":
                await on_event(obj)
        except Exception:
            log.exception("VK: handling an update failed")
    if len(_locks) > 500:
        for k in [k for k, l in _locks.items() if not l.locked()]:
            _locks.pop(k, None)


# ---------- telling VK families about new photos ----------

async def notify_families(tids: list, skip_peer: int | None = None) -> int:
    """Short 'new photos' note to every VK person in these families. Returns how many got it."""
    sent = 0
    for u in await B.db.vkusers.find({"thread_id": {"$in": list(tids)}}).to_list():
        if u["_id"] == skip_peer:
            continue
        try:
            await send(u["_id"], "📷 В вашей семейной галерее появились новые фото! Нажмите «Все фото».", await main_keyboard(u["thread_id"]))
            sent += 1
        except Exception as e:
            log.warning("VK notice failed for %s: %s", u["_id"], e)
        await asyncio.sleep(0.1)
    return sent


# ---------- the loop ----------

async def run(bot_module) -> None:
    """Long Poll forever. Never raises (the Telegram bot must keep running whatever happens here)."""
    global B, _http
    B = bot_module
    _http = httpx.AsyncClient(timeout=40)
    delay = 5
    settings_done = False
    while True:
        try:
            srv = await call("groups.getLongPollServer", group_id=VK_GROUP_ID)
            key, server, ts = srv["key"], srv["server"], srv["ts"]
            if not settings_done:
                try:
                    await call("groups.setLongPollSettings", group_id=VK_GROUP_ID, enabled=1, api_version=API_V, message_new=1, message_event=1)
                except VKError as e:
                    log.warning("Couldn't switch on Long Poll events (%s). Turn on 'message_new' and 'message_event' in the community settings.", e)
                settings_done = True
            log.info("VK Long Poll is running")
            delay = 5
            while True:
                r = await _http.get(server, params={"act": "a_check", "key": key, "ts": ts, "wait": 25}, timeout=40)
                d = r.json()
                if "failed" in d:
                    if d["failed"] == 1:
                        ts = d.get("ts", ts)
                        continue
                    break  # key expired or lost: ask for a new server
                ts = d.get("ts", ts)
                for u in d.get("updates", []):
                    t = asyncio.create_task(handle(u))
                    _tasks.add(t)
                    t.add_done_callback(_tasks.discard)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("VK Long Poll problem: %s (trying again in %s s)", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)
