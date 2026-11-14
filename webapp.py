"""The Telegram Mini App's web server (standard library only, runs inside the bot's own event loop).

Safety rules, all enforced here:
  * Every /api call must carry Telegram's signed `initData`. It is verified with the bot token, so nobody
    can pretend to be someone else, and it expires after 24 hours.
  * A person only ever gets their OWN family's photos. The family comes from the database (never from the
    request), and the owner and not-yet-confirmed people get nothing.
  * Photo links are signed, expire after a few hours, and the family is checked again on every image request.
"""
import asyncio
import collections
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

log = logging.getLogger("photobot.web")

INIT_MAX_AGE = 24 * 3600
URL_LIFETIME = 6 * 3600
PAGE = 90  # photos per page in the grid
WELCOME_ITEMS = 12  # photos on the welcome screen
NEW_DAYS = 14  # how long photos count as "new"
FAV_MAX = 2000  # most hearts one person can keep
NEW_MIN = 2  # how many new photos it takes to show the "New photos" welcome
THUMB_SIDE, FULL_SIDE = 360, 1600
MAX_JSON = 16 * 1024
MAX_UPLOAD = 30 * 1024 * 1024  # one photo from the Admin tab (bots can send up to 50 MB; this keeps Render's memory safe)
UPLOAD_PATH = "/api/admin/upload"
CACHE_BYTES = 80 * 1024 * 1024
MAX_CONNECTIONS = 300

HERE = os.path.dirname(os.path.abspath(__file__))
SECURITY = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def validate_init_data(init_data: str, token: str, now: float | None = None) -> dict | None:
    """Telegram's check: returns the user dict when the signature is right and fresh, else None."""
    if not init_data or len(init_data) > 8192:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    given = pairs.pop("hash", None)
    if not given or not re.fullmatch(r"[0-9a-f]{64}", given):
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, given):
        return None
    now = time.time() if now is None else now
    try:
        auth = int(pairs.get("auth_date", "0"))
        user = json.loads(pairs.get("user", ""))
    except (ValueError, TypeError):
        return None
    if auth > now + 300 or now - auth > INIT_MAX_AGE:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int) or user["id"] <= 0:
        return None
    return user


class Resp:
    def __init__(self, status=200, body=b"", ctype="application/json", headers=None):
        self.status, self.body, self.ctype = status, body, ctype
        self.headers = headers or {}


def jresp(obj, status=200) -> Resp:
    return Resp(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8",
                {"Cache-Control": "no-store"})


def err(status: int, text: str) -> Resp:
    return jresp({"error": text}, status)


def sniff(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


MAX_PIXELS = 30_000_000  # for PNG and the like, which must be decoded at full size
MAX_JPEG_PIXELS = 150_000_000  # JPEGs are decoded at a reduced size, so even a big camera original is cheap


HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif")
EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "image/heic": ".heic"}


def sniff_image(data: bytes) -> str | None:
    """What kind of picture this is, judged by the bytes themselves (never by the name the phone gave it)."""
    if len(data) < 16:
        return None  # too small to be a real picture
    ct = sniff(data)
    if ct != "application/octet-stream":
        return ct
    if data[4:8] == b"ftyp" and data[8:12] in HEIC_BRANDS:
        return "image/heic"
    return None


def safe_name(name: str, ct: str) -> str:
    base = re.sub(r"[^\w.\- ]", "_", (name or "").replace("\\", "/").split("/")[-1]).strip(" .")[:80]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return (stem or f"photo_{int(time.time())}") + EXT.get(ct, ".jpg")


def resize(data: bytes, side: int, quality: int) -> bytes:
    """Shrink a picture to fit side x side as a JPEG, using as little memory as possible
    (the free Render plan has only 512 MB)."""
    import warnings

    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_JPEG_PIXELS  # refuse decompression bombs
    warnings.simplefilter("error", Image.DecompressionBombWarning)  # (a warning becomes a refusal)
    im = Image.open(io.BytesIO(data))
    if im.size[0] * im.size[1] > (MAX_JPEG_PIXELS if im.format == "JPEG" else MAX_PIXELS):
        raise ValueError("image too large")
    try:
        im.draft("RGB", (side, side))  # a JPEG is decoded at a smaller scale, which saves most of the memory
    except Exception:
        pass
    im = ImageOps.exif_transpose(im)
    if im.mode.startswith(("I", "F")):  # 16-bit greys: scale down to 8 bits first (convert alone turns them white)
        im = im.point(lambda i: i * (1 / 256)).convert("L")
    elif im.mode == "P" and im.size[0] * im.size[1] <= 6_000_000:
        im = im.convert("RGBA")
    im.thumbnail((side, side))  # shrink BEFORE any colour conversion, so the big copies never exist
    if im.mode in ("RGBA", "LA", "P", "PA"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue()


def naive_utc(dt):
    """Mongo gives back naive UTC datetimes; make any datetime comparable with those."""
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class Cache:
    def __init__(self, limit: int):
        self.limit, self.size, self.d = limit, 0, collections.OrderedDict()

    def get(self, k):
        v = self.d.get(k)
        if v is not None:
            self.d.move_to_end(k)
        return v

    def put(self, k, v: tuple):
        if len(v[1]) > self.limit // 4:
            return
        old = self.d.pop(k, None)
        if old:
            self.size -= len(old[1])
        self.d[k] = v
        self.size += len(v[1])
        while self.size > self.limit and self.d:
            _, ev = self.d.popitem(last=False)
            self.size -= len(ev[1])


class WebApp:
    """`deps` supplies everything that lives in bot.py:
        token, db(), bot(), is_owner(uid), has_best(tid), help_text(), copyright, owner_name, bot_username(),
        send_original(uid, doc) -> bool, pretty_date(day), family_name(tid) (async)"""

    def __init__(self, deps, html_path: str | None = None):
        self.d = deps
        self.html_path = html_path or os.path.join(HERE, "app.html")
        self.url_secret = hmac.new(deps.token.encode(), b"photo-urls", hashlib.sha256).digest()
        self.cache = Cache(CACHE_BYTES)
        self.icons: dict = {}
        self.dl_gate = asyncio.Semaphore(4)
        self.big_gate = asyncio.Semaphore(2)
        self.cpu_gate = asyncio.Semaphore(1)  # (one CPU: more at once only slows the bot down)
        self.inflight: dict = {}
        self.uploads: dict = {}  # upload key -> the running/finished upload, so a repeated tap can't post a photo twice
        self.upload_gate = asyncio.Semaphore(1)  # one big file in memory at a time
        self.last_orig: dict[int, float] = {}
        self.tr_hits: dict[int, list] = {}
        self.conns = 0

    # ---------- plumbing ----------

    def sign(self, mid: int, size: str, uid: int, exp: int) -> str:
        msg = f"{mid}.{size}.{uid}.{exp}".encode()
        return hmac.new(self.url_secret, msg, hashlib.sha256).hexdigest()[:32]

    def photo_url(self, mid: int, size: str, uid: int) -> str:
        # the expiry is rounded, so the same link comes back all day and the phone can keep the picture
        exp = (int(time.time()) // URL_LIFETIME + 2) * URL_LIFETIME
        return f"/img/{mid}?s={size}&u={uid}&e={exp}&sig={self.sign(mid, size, uid, exp)}"

    # ---------- web-link visitors ("guests": no Telegram account) ----------

    GUEST_PATHS = {"/api/home", "/api/dates", "/api/places", "/api/photos", "/api/fav", "/api/seen", "/api/a2hs", "/api/web_pin"}
    GUEST_MAX = 40  # browsers per family link

    def guest_key(self, gid: int, gen: int | None = None) -> str:
        what = f"guest:{gid}" if gen is None else f"guest:{gid}:{gen}"
        return f"{gid}." + hmac.new(self.url_secret, what.encode(), hashlib.sha256).hexdigest()[:32]

    async def person_of(self, uid: int) -> dict | None:
        """The named web person (a child, a grandparent ...) this guest id stands for, if switched on."""
        if not isinstance(uid, int) or uid >= 0:
            return None
        p = await self.d.db().webpeople.find_one({"_id": uid})
        if not p or not p.get("on", True):
            return None
        until = p.get("until")
        if until:  # (the link switches itself off on the chosen day)
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if until < datetime.now(timezone.utc):
                return None
        if not await self.d.db().families.find_one({"thread_id": p["tid"]}):
            return None
        return p

    async def guest_from(self, key: str) -> dict | None:
        """The guest this key belongs to. Turning a link off, replacing it or removing the person cuts it off."""
        gid_s, _, sig = (key or "").rpartition(".")
        try:
            gid = int(gid_s)
        except ValueError:
            return None
        if gid >= 0 or not re.fullmatch(r"[0-9a-f]{32}", sig):
            return None
        db = self.d.db()
        person = await db.webpeople.find_one({"_id": gid})
        if person:  # a named person: one identity on every device, so hearts follow them
            if not hmac.compare_digest(self.guest_key(gid, person.get("gen", 0)), f"{gid}.{sig}") or not await self.person_of(gid):
                return None
            return {"_id": gid, "tid": person["tid"], "person": person}
        if not hmac.compare_digest(self.guest_key(gid), f"{gid}.{sig}"):
            return None
        g = await db.guests.find_one({"_id": gid})
        if not g or not await db.families.find_one({"thread_id": g["tid"], "web": {"$exists": True}}):
            return None
        return {**g, "person": None}

    async def guest_join(self, body: bytes) -> Resp:
        try:
            req = json.loads(body.decode() or "{}")
            if not isinstance(req, dict):
                raise ValueError
        except (ValueError, RecursionError):
            return err(400, "Bad request")
        tok = req.get("token")
        if not isinstance(tok, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,60}", tok):
            return err(404, "This link doesn't work.")
        db = self.d.db()
        fam = await db.families.find_one({"web": tok})
        if not fam:
            return err(404, "This link isn't active any more. Please ask for a new one.")
        old = await self.guest_from(str(req.get("key") or ""))
        if old and old["tid"] == fam["thread_id"] and not old["person"]:  # (this browser has been here before)
            return jresp({"key": req["key"], "family": fam["name"]})
        if await db.guests.count_documents({"tid": fam["thread_id"]}) >= self.GUEST_MAX:
            return err(429, "This link has been opened on too many devices. Please ask for a new one.")
        gid = -(10 ** 9 + secrets.randbelow(10 ** 12))
        await db.guests.insert_one({"_id": gid, "tid": fam["thread_id"], "at": datetime.now(timezone.utc)})
        return jresp({"key": self.guest_key(gid), "family": fam["name"]})

    async def code_blocked(self, ip: str) -> bool:
        """Too many wrong connection codes from one place (or from everywhere) lately?"""
        db = self.d.db()
        now = time.time()
        for key, cap in ((f"web:ip:{ip}", 8), ("web:all", 200)):
            d = await db.attempts.find_one({"_id": key}) or {}
            if d.get("n", 0) >= cap and now - d.get("first", 0) < 3600:
                return True
        return False

    async def code_failed(self, ip: str) -> None:
        db = self.d.db()
        now = time.time()
        for key in (f"web:ip:{ip}", "web:all"):
            d = await db.attempts.find_one({"_id": key}) or {}
            if not d or now - d.get("first", 0) >= 3600:
                await db.attempts.update_one({"_id": key}, {"$set": {"n": 1, "first": now}}, upsert=True)
            else:
                await db.attempts.update_one({"_id": key}, {"$inc": {"n": 1}})

    async def web_connect(self, body: bytes, headers: dict) -> Resp:
        """The one web address: a short one-use code connects this device to a person's account for good."""
        try:
            req = json.loads(body.decode() or "{}")
            if not isinstance(req, dict):
                raise ValueError
        except (ValueError, RecursionError):
            return err(400, "Bad request")
        ip = hashlib.sha256((headers.get("x-forwarded-for", "?").split(",")[0].strip() or "?").encode()).hexdigest()[:16]
        if await self.code_blocked(ip):
            return err(429, "Too many wrong codes. Please wait a while and try again.")
        code = re.sub(r"\D", "", str(req.get("code") or ""))
        db = self.d.db()
        doc = await db.webcodes.find_one({"_id": code}) if len(code) == 8 else None
        exp = doc and doc.get("exp")
        if exp and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        p = await self.person_of(doc["pid"]) if doc and exp and exp > datetime.now(timezone.utc) else None
        if not p:
            await self.code_failed(ip)
            return err(404, "That code isn't right, or it has expired. Please ask for a new one.")
        await db.webcodes.delete_one({"_id": code})  # (one use)
        return jresp({"key": self.guest_key(p["_id"], p.get("gen", 0)), "pid": p["_id"], "name": p["name"], "family": (await self.d.family_name(p["tid"])) or ""})

    # ---------- website accounts for parents (real Telegram accounts; username + password; Telegram confirms each sign-in) ----------
    USER_RE = re.compile(r"[a-z0-9_.]{3,32}")

    def acct_key(self, uid: int, gen: int) -> str:
        return f"u{uid}." + hmac.new(self.url_secret, f"wa:{uid}:{gen}".encode(), hashlib.sha256).hexdigest()[:32]

    async def acct_from(self, key: str) -> dict | None:
        m = re.fullmatch(r"u(\d{1,15})\.([0-9a-f]{32})", key or "")
        if not m:
            return None
        a = await self.d.db().webaccts.find_one({"_id": int(m.group(1))})
        if not a or not a.get("on", True) or not hmac.compare_digest(self.acct_key(a["_id"], a.get("gen", 0)), key):
            return None
        return a

    @staticmethod
    def pw_hash(password: str, salt: bytes) -> str:
        return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()

    @staticmethod
    def ip_of(headers: dict) -> str:
        return hashlib.sha256((headers.get("x-forwarded-for", "?").split(",")[0].strip() or "?").encode()).hexdigest()[:16]

    async def throttled(self, keys) -> bool:
        db = self.d.db()
        now = time.time()
        for key, cap in keys:
            d = await db.attempts.find_one({"_id": key}) or {}
            if d.get("n", 0) >= cap and now - d.get("first", 0) < 3600:
                return True
        return False

    async def strike(self, keys) -> None:
        db = self.d.db()
        now = time.time()
        for key, _ in keys:
            d = await db.attempts.find_one({"_id": key}) or {}
            if not d or now - d.get("first", 0) >= 3600:
                await db.attempts.update_one({"_id": key}, {"$set": {"n": 1, "first": now}}, upsert=True)
            else:
                await db.attempts.update_one({"_id": key}, {"$inc": {"n": 1}})

    @staticmethod
    def jbody(body: bytes) -> dict | None:
        try:
            req = json.loads(body.decode() or "{}")
            return req if isinstance(req, dict) else None
        except (ValueError, RecursionError):
            return None

    async def hand_of(self, tok) -> dict | None:
        """A pass from the Telegram button: one use, 10 minutes."""
        if not isinstance(tok, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,60}", tok):
            return None
        d = await self.d.db().webhand.find_one({"_id": tok})
        exp = d and d.get("exp")
        if exp and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return d if d and exp and exp > datetime.now(timezone.utc) else None

    async def username_for(self, d: dict) -> tuple[str, bool]:
        """(the username this person will have, whether they have to choose one)."""
        db = self.d.db()
        a = await db.webaccts.find_one({"_id": d["uid"]})
        if a:
            return a["username"], False
        u = (d.get("username") or "").lower()
        if self.USER_RE.fullmatch(u):
            other = await db.webaccts.find_one({"username": u})
            if not other or other["_id"] == d["uid"]:
                return u, False
        return "", True

    async def web_hand_info(self, body: bytes, headers: dict) -> Resp:
        req = self.jbody(body)
        if req is None:
            return err(400, "Bad request")
        if await self.throttled([(f"hand:{self.ip_of(headers)}", 60)]):
            return err(429, "Too many tries. Please wait a while.")
        d = await self.hand_of(req.get("h"))
        if not d:
            await self.strike([(f"hand:{self.ip_of(headers)}", 60)])
            return err(404, "This link has expired. Please tap the website button in Telegram again.")
        user, choose = await self.username_for(d)
        return jresp({"username": user, "choose": choose, "name": d.get("name") or "", "has": bool(await self.d.db().webaccts.find_one({"_id": d["uid"]}))})

    async def web_register(self, body: bytes, headers: dict) -> Resp:
        """Make (or reset) the website password. Only possible with a pass from the Telegram button."""
        req = self.jbody(body)
        if req is None:
            return err(400, "Bad request")
        db = self.d.db()
        ip = self.ip_of(headers)
        if await self.throttled([(f"hand:{ip}", 60)]):
            return err(429, "Too many tries. Please wait a while.")
        d = await self.hand_of(req.get("h"))
        if not d:
            await self.strike([(f"hand:{ip}", 60)])
            return err(404, "This link has expired. Please tap the website button in Telegram again.")
        uid = d["uid"]
        user, choose = await self.username_for(d)
        if choose:
            user = str(req.get("username") or "").strip().lstrip("@").lower()
            if not self.USER_RE.fullmatch(user):
                return err(400, "Your username can use letters, numbers, _ and . (3 to 32 characters).")
            other = await db.webaccts.find_one({"username": user})
            if other and other["_id"] != uid:
                return err(400, "That username is taken. Please choose another one.")
        pw = req.get("password")
        if not isinstance(pw, str) or not 8 <= len(pw) <= 100:
            return err(400, "Your password needs at least 8 characters.")
        if pw.lower() == user or pw.isdigit() and len(set(pw)) == 1:
            return err(400, "That password is too easy to guess. Please choose another one.")
        salt = secrets.token_bytes(16)
        pwh = await asyncio.to_thread(self.pw_hash, pw, salt)
        old = await db.webaccts.find_one({"_id": uid})
        gen = (old.get("gen", 0) + 1) if old else 0  # (a new password signs out every other device)
        doc = {"username": user, "salt": salt.hex(), "pw": pwh, "gen": gen, "on": True}
        if old:
            await db.webaccts.update_one({"_id": uid}, {"$set": doc})
        else:
            await db.webaccts.insert_one({"_id": uid, **doc, "created": datetime.now(timezone.utc)})
        await db.webhand.delete_one({"_id": d["_id"]})
        await db.attempts.delete_one({"_id": f"pw:{user}"})
        return jresp({"key": self.acct_key(uid, gen), "username": user})

    @staticmethod
    def device_of(ua: str) -> str:
        ua = ua or ""
        os_ = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"), ("Macintosh", "Mac"), ("CrOS", "Chromebook"), ("Linux", "Linux")) if k in ua), "unknown device")
        br = next((n for k, n in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("CriOS", "Chrome"), ("Safari/", "Safari")) if k in ua), "a browser")
        return f"{os_}, {br}"

    async def web_signin(self, body: bytes, headers: dict) -> Resp:
        """Username + password. The password alone is never enough: the bot asks them in Telegram if it's really them."""
        req = self.jbody(body)
        if req is None:
            return err(400, "Bad request")
        db = self.d.db()
        user = str(req.get("username") or "").strip().lstrip("@").lower()[:40]
        pw = req.get("password")
        ip = self.ip_of(headers)
        keys = [(f"pw:{user}", 5), (f"pw:ip:{ip}", 15), ("pw:all", 300)]
        if await self.throttled(keys):
            return err(429, "Too many wrong tries. Please wait a while and try again.")
        a = await db.webaccts.find_one({"username": user}) if self.USER_RE.fullmatch(user) else None
        salt = bytes.fromhex(a["salt"]) if a and a.get("salt") else b"\0" * 16
        h = await asyncio.to_thread(self.pw_hash, pw if isinstance(pw, str) and len(pw) <= 100 else "", salt)  # (always hashed, so a missing user takes as long as a real one)
        if not a or not a.get("pw") or not a.get("on", True) or not hmac.compare_digest(h, a["pw"]):
            await self.strike(keys)
            return err(401, "Wrong username or password.")
        uid = a["_id"]
        now = datetime.now(timezone.utc)
        if await db.weblogins.count_documents({"uid": uid, "at": {"$gt": now - timedelta(hours=1)}}) >= 3:
            return err(429, "Too many sign-in requests. Please wait a while.")
        lid, poll = secrets.token_hex(12), secrets.token_urlsafe(24)
        await db.weblogins.insert_one({"_id": lid, "poll": poll, "uid": uid, "status": "wait", "at": now, "exp": now + timedelta(minutes=2)})
        if not await self.d.web_login_prompt(uid, lid, self.device_of(headers.get("user-agent", ""))):
            await db.weblogins.delete_one({"_id": lid})
            return err(503, "I couldn't reach you in Telegram. Please open the bot, send /start, and try again.")
        await db.attempts.delete_one({"_id": f"pw:{user}"})
        return jresp({"poll": poll, "seconds": 120})

    async def web_signin_status(self, body: bytes, headers: dict) -> Resp:
        req = self.jbody(body)
        poll = req.get("poll") if req else None
        db = self.d.db()
        d = await db.weblogins.find_one({"poll": poll}) if isinstance(poll, str) and 10 <= len(poll) <= 60 else None
        exp = d and d.get("exp")
        if exp and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if not d or d["status"] == "done" or (d["status"] == "wait" and exp < datetime.now(timezone.utc)):
            return jresp({"status": "expired"})
        if d["status"] == "no":
            return jresp({"status": "no"})
        if d["status"] != "ok":
            return jresp({"status": "wait"})
        a = await db.webaccts.find_one({"_id": d["uid"]})
        await db.weblogins.update_one({"_id": d["_id"]}, {"$set": {"status": "done"}})  # (the key is handed over once)
        if not a or not a.get("on", True):
            return jresp({"status": "expired"})
        return jresp({"status": "ok", "key": self.acct_key(a["_id"], a.get("gen", 0)), "username": a["username"]})

    def pin_hash(self, pid: int, pin: str) -> str:
        return hmac.new(self.url_secret, f"pin:{pid}:{pin}".encode(), hashlib.sha256).hexdigest()

    async def web_login(self, body: bytes, headers: dict) -> Resp:
        """Log back in on a device that logged out: the person's own 4-digit PIN (they chose it themselves)."""
        try:
            req = json.loads(body.decode() or "{}")
            if not isinstance(req, dict):
                raise ValueError
        except (ValueError, RecursionError):
            return err(400, "Bad request")
        pid, pin = req.get("pid"), re.sub(r"\D", "", str(req.get("pin") or ""))
        ip = hashlib.sha256((headers.get("x-forwarded-for", "?").split(",")[0].strip() or "?").encode()).hexdigest()[:16]
        db = self.d.db()
        now = time.time()
        keys = ((f"pin:{pid}", 5), (f"pin:ip:{ip}", 15), ("pin:all", 300))
        for key, cap in keys:
            d = await db.attempts.find_one({"_id": key}) or {}
            if d.get("n", 0) >= cap and now - d.get("first", 0) < 3600:
                return err(429, "Too many wrong PINs. Please wait a while, or ask for a new code.")
        p = await self.person_of(pid) if isinstance(pid, int) and not isinstance(pid, bool) and pid < 0 and len(pin) == 4 else None
        if not p or not p.get("pin") or not hmac.compare_digest(p["pin"], self.pin_hash(p["_id"], pin)):
            for key, _ in keys:
                d = await db.attempts.find_one({"_id": key}) or {}
                if not d or now - d.get("first", 0) >= 3600:
                    await db.attempts.update_one({"_id": key}, {"$set": {"n": 1, "first": now}}, upsert=True)
                else:
                    await db.attempts.update_one({"_id": key}, {"$inc": {"n": 1}})
            return err(404, "That PIN isn't right. Please try again, or ask for a new code.")
        await db.attempts.delete_one({"_id": f"pin:{pid}"})
        return jresp({"key": self.guest_key(p["_id"], p.get("gen", 0)), "pid": p["_id"], "name": p["name"]})

    async def vis(self, tid: int, uid: int) -> dict:
        """The photos this person may see: the family's, narrowed for a web person the parents limited."""
        q = await self.d.vis_q(tid)
        p = await self.person_of(uid) if uid < 0 else None
        if p and p.get("scope") == "best":
            q["best"] = True
        elif p and p.get("scope") == "dates":
            q["date"] = {"$in": list(p.get("dates") or [])[:400]}
        return q

    async def family_of(self, uid: int) -> int | None:
        """The family this person may browse. Unconfirmed people get None. The owner gets only the family
        they picked with /connect (kept in memory by the bot, never saved), so nothing about them is stored."""
        if self.d.is_owner(uid):
            tid = self.d.preview_family(uid)
            if tid and not await self.d.db().families.find_one({"thread_id": tid}):
                return None  # that family was deleted
            return tid
        if uid < 0:  # a web-link visitor
            p = await self.person_of(uid)
            if p:
                return p["tid"]
            g = await self.d.db().guests.find_one({"_id": uid})
            if not g or not await self.d.db().families.find_one({"thread_id": g["tid"], "web": {"$exists": True}}):
                return None
            return g["tid"]
        doc = await self.d.db().users.find_one({"_id": uid})
        if not doc or doc.get("pending"):
            return None
        return doc.get("thread_id")

    async def owner_family(self, uid: int, req: dict):
        """The family the owner picked in the Admin tab (sent as `fam`). Nothing is stored about the owner:
        the pick lives only in the open app. No pick (or a bad one) means no family."""
        fam = req.get("fam")
        if isinstance(fam, int) and not isinstance(fam, bool) and abs(fam) <= 2 ** 53 \
                and await self.d.db().families.find_one({"thread_id": fam}):
            return fam
        return None

    # ---------- routing ----------

    async def handle(self, method: str, target: str, headers: dict, body: bytes) -> Resp:
        parts = urlsplit(target)
        path, query = parts.path, dict(parse_qsl(parts.query))
        head = method == "HEAD"
        if head:
            method = "GET"
        try:
            if path == "/" and method == "GET":
                return Resp(200, b"ok", "text/plain")
            if path == "/app" and method == "GET":
                return self.page()
            if method == "GET" and (m := re.fullmatch(r"/icon-(180|192|512)\.png", path)):
                return Resp(200, self.app_icon(int(m.group(1))), "image/png", {"Cache-Control": "public, max-age=86400"})
            if method == "GET" and (m := re.fullmatch(r"/w/([A-Za-z0-9_-]{10,60})/manifest\.webmanifest", path)):
                return await self.web_manifest(m.group(1))
            if method == "GET" and path == "/web/manifest.webmanifest":
                return await self.web_manifest(None)
            if method == "GET" and path in ("/web", "/web/"):  # the one web address for everyone
                head = ('<link rel="manifest" href="/web/manifest.webmanifest"><link rel="apple-touch-icon" href="/icon-180.png">'
                        '<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">'
                        '<meta name="apple-mobile-web-app-title" content="Photos"><meta name="theme-color" content="#1f6fb2">')
                return self.page({"Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}, head)
            if path.startswith("/w/") and method == "GET":  # a web link: the same app page, set up so it can be put on a home screen
                m = re.fullmatch(r"/w/([A-Za-z0-9_-]{10,60})/?", path)
                head = (f'<link rel="manifest" href="/w/{m.group(1)}/manifest.webmanifest">'
                        '<link rel="apple-touch-icon" href="/icon-180.png"><meta name="apple-mobile-web-app-capable" content="yes">'
                        '<meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Photos">'
                        '<meta name="theme-color" content="#1f6fb2">') if m else ""
                return self.page({"Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}, head)
            if path.startswith("/dl/") and method == "GET":
                return await self.image(path[4:], query, download=True)
            if path.startswith("/img/") and method == "GET":
                return await self.image(path[5:], query)
            if path.startswith("/simg/") and method == "GET":
                return await self.suggestion_image(path[6:], query)
            if path == UPLOAD_PATH and method == "POST":
                return await self.upload(query, headers, body)
            if path.startswith("/api/") and method == "POST":
                return await self.api(path, headers, body)
            return err(404, "Not found")
        except Exception:
            log.exception("Web request failed: %s %s", method, path)
            return err(500, "Something went wrong. Please try again.")

    def page(self, extra: dict | None = None, head: str = "") -> Resp:
        try:
            with open(self.html_path, "rb") as f:
                html = f.read()
        except OSError:
            return Resp(500, b"The app page is missing on the server.", "text/plain")
        if head:
            html = html.replace(b"</head>", head.encode() + b"</head>", 1)
        return Resp(200, html, "text/html; charset=utf-8", {"Cache-Control": "no-store", **(extra or {})})

    def app_icon(self, size: int) -> bytes:
        """The little camera picture for a home screen (drawn here, so there is no file to ship)."""
        hit = self.icons.get(size)
        if hit:
            return hit
        from PIL import Image, ImageDraw
        k = size / 512
        im = Image.new("RGB", (size, size), (31, 111, 178))
        d = ImageDraw.Draw(im)
        box = lambda x0, y0, x1, y1: [x0 * k, y0 * k, x1 * k, y1 * k]
        d.rounded_rectangle(box(196, 150, 316, 200), radius=14 * k, fill=(255, 255, 255))
        d.rounded_rectangle(box(120, 180, 392, 370), radius=34 * k, fill=(255, 255, 255))
        d.ellipse(box(196, 211, 316, 331), fill=(31, 111, 178))
        d.ellipse(box(214, 229, 298, 313), fill=(255, 255, 255))
        d.ellipse(box(232, 247, 280, 295), fill=(31, 111, 178))
        d.ellipse(box(340, 200, 366, 226), fill=(31, 111, 178))
        out = io.BytesIO()
        im.save(out, "PNG", optimize=True)
        self.icons[size] = out.getvalue()
        return self.icons[size]

    async def web_manifest(self, tok: str | None) -> Resp:
        db = self.d.db()
        if tok is not None and not await db.families.find_one({"web": tok}):
            return err(404, "Not found")
        base = "/web" if tok is None else f"/w/{tok}"
        data = {"name": f"{self.d.owner_name} · Photos", "short_name": "Photos", "id": base, "start_url": base, "scope": base,
                "display": "standalone", "background_color": "#ffffff", "theme_color": "#1f6fb2",
                "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                          {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}
        return Resp(200, json.dumps(data).encode(), "application/manifest+json", {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    # ---------- uploads (owner only) ----------

    def upload_denied(self, headers: dict):
        """None if the caller is the owner, else the error to send. Checked BEFORE the body is read."""
        user = validate_init_data(headers.get("x-init-data", ""), self.d.token)
        if not user:
            return err(401, "Please close this and open the app again from Telegram.")
        if not self.d.is_owner(user["id"]):
            return err(403, "Not available.")
        return None

    async def upload(self, query: dict, headers: dict, body: bytes) -> Resp:
        denied = self.upload_denied(headers)
        if denied:
            return denied
        fam_q = query.get("fam", "")
        gid = None
        if fam_q.startswith("g:"):  # a whole group (one shared copy that every family in it sees)
            gid = fam_q[2:]
            if not re.fullmatch(r"[0-9a-f]{6}", gid):
                return err(400, "Please pick a family.")
            if not await self.d.db().groups.find_one({"_id": gid}):
                return err(404, "That group no longer exists.")
            tid = None
            try:
                skip = sorted({int(x) for x in query.get("skip", "").split(",") if x.strip()})[:60]
            except ValueError:
                return err(400, "Bad request")
        else:
            skip = []
            try:
                tid = int(fam_q)
            except ValueError:
                return err(400, "Please pick a family.")
            if abs(tid) > 2 ** 53 or not await self.d.db().families.find_one({"thread_id": tid}):
                return err(404, "That family no longer exists.")
        day = self.d.parse_date(query.get("date", ""))
        if day is None:
            return err(400, "Please give a date like 2026-07-04 (not in the future), or choose that you don't know it.")
        place = " ".join(query.get("place", "").split())[:80] or None
        if place:  # a ⭐ anywhere in the caption would mark the photo as a best shot
            place = " ".join(place.replace("⭐", "").replace("\ufe0f", "").split())[:80] or None
        if place and place.casefold() in self.d.unknown_words:
            place = None
        if not body:
            return err(400, "That file is empty.")
        ct = sniff_image(body)
        if not ct:
            return err(400, "That doesn't look like a photo (JPG, PNG, HEIC, WEBP or GIF), so I skipped it.")
        name = safe_name(query.get("name", ""), ct)
        key = headers.get("x-upload-key", "")
        if not re.fullmatch(r"[0-9a-zA-Z_-]{8,40}", key):
            return err(400, "Bad request")
        sig = (gid or tid, day, place, len(body), tuple(skip))
        cur = self.uploads.get(key)
        if cur is not None and cur[0] != sig:
            return err(409, "That upload changed. Please start it again.")
        if cur is None:
            fut = asyncio.ensure_future(self.do_upload(key, tid, place, day, name, body, gid, skip))
            self.uploads[key] = (sig, fut)
        else:
            fut = cur[1]
        status, data = await asyncio.shield(fut)  # (a repeat of the same upload waits for, and gets, the same answer)
        return jresp(data, status)

    async def do_upload(self, key: str, tid, place, day: str, name: str, body: bytes, gid=None, skip=None):
        try:
            async with self.upload_gate:
                status, data = await self.d.upload_photo(tid, place, day, name, body, **({"gid": gid, "skip": skip} if gid else {}))
        except Exception:
            log.exception("Upload failed")
            status, data = 500, {"error": "Something went wrong. Please try again."}
        if status != 200 and not (isinstance(data, dict) and data.get("maybe")):
            self.uploads.pop(key, None)  # a failed one can be tried again with the same key
        else:  # done, or "maybe posted": remembered so a repeat can't post it twice
            asyncio.get_running_loop().call_later(3600, self.uploads.pop, key, None)
        return status, data

    # ---------- api ----------

    def pd(self, day) -> str:
        try:
            return self.d.pretty_date(day)
        except Exception:  # an odd date in old data must not break the whole family's screen
            return str(day or "")


    async def cards(self, docs: list, uid: int) -> list:
        gn = await self.d.group_names_for(docs)  # (photos of a whole group are labelled with the group's name)
        mids = [x["message_id"] for x in docs if isinstance(x.get("message_id"), int)]
        favs = {f["mid"] for f in await self.d.db().favs.find({"uid": uid, "mid": {"$in": mids}}).to_list()} if mids and not self.d.is_owner(uid) else set()
        out = []
        for x in docs:
            c = self.card(x, uid)
            if x.get("message_id") in favs:
                c["fav"] = True
            if gn.get(x.get("thread_id")):
                c["g"] = gn[x["thread_id"]]
            out.append(c)
        return out

    def card(self, doc: dict, uid: int) -> dict:
        return {
            "id": doc["message_id"], "k": doc.get("kind", "document"), "d": doc.get("date") or "",
            "dt": self.pd(doc.get("date") or ""), "loc": doc.get("location") or "",
            "by": doc.get("by") or "", "best": bool(doc.get("best")),
            **({"dur": int(doc.get("dur") or 0), "mb": round((doc.get("vsize") or 0) / 1048576, 1)} if doc.get("kind") == "video" else {}),
            "t": self.photo_url(doc["message_id"], "thumb", uid),
            "f": self.photo_url(doc["message_id"], "full", uid),
        }

    async def welcome(self, uid: int, tid: int) -> dict:
        """What the first screen shows, decided per person:
          * 2+ new photos from the last 14 days, and this person hasn't seen the newest yet -> "new"
          * otherwise, when the photographer picked Best shots -> "best"
          * otherwise the latest photos -> "latest"
        Opening the app with the "new" welcome is what counts as having seen it."""
        db = self.d.db()
        cutoff = datetime.now(timezone.utc) - timedelta(days=NEW_DAYS)
        vq = await self.vis(tid, uid)  # (the family's own photos + its groups' shared ones, minus files that leave it out)
        new_q = {**vq, "sug": {"$ne": True}, "added": {"$gte": cutoff}}
        n_new = await db.photos.count_documents(new_q)
        best = (await db.photos.count_documents({**vq, "best": True}) > 0) if uid < 0 else await self.d.has_best(tid)  # (a limited web person only counts what they may see)
        total = await db.photos.count_documents(vq)
        mode = "latest"
        if n_new >= NEW_MIN:
            newest = await db.photos.find(new_q).sort([("added", -1)]).limit(1).to_list()
            newest_at = naive_utc(newest[0].get("added")) if newest else None
            owner = self.d.is_owner(uid)  # (the owner's preview never reads or saves anything about them)
            user = {} if owner else (await db.users.find_one({"_id": uid}) or {})
            seen = naive_utc(user.get("seen_new"))
            if seen is None or newest_at is None or newest_at > seen:
                mode = "new"  # they haven't seen the newest photos yet
        if mode == "latest" and best:
            mode = "best"
        if mode == "new":
            q, sort = new_q, [("added", -1), ("message_id", -1)]
            title, sub = "✨ New photos", f"{n_new} new photo{'s' if n_new != 1 else ''} in the last {NEW_DAYS} days"
        elif mode == "best":
            q, sort = {**vq, "best": True}, [("message_id", -1)]
            title, sub = "⭐ Best shots", "Picked for you"
        else:
            q, sort = dict(vq), [("message_id", -1)]
            title, sub = "📸 Latest photos", ""
        docs = await db.photos.find(q).sort(sort).limit(WELCOME_ITEMS).to_list()
        if mode == "new" and docs and not self.d.is_owner(uid):
            newest_at = max((naive_utc(x.get("added")) for x in docs if x.get("added")), default=None)
            if newest_at:
                await db.users.update_one({"_id": uid}, {"$set": {"seen_new": newest_at}})
        return {"mode": mode, "title": title, "sub": sub, "total": total,
                "items": await self.cards(docs, uid)}

    async def api(self, path: str, headers: dict, body: bytes) -> Resp:
        if path == "/api/guest_join":
            return await self.guest_join(body)
        if path == "/api/web_connect":
            return await self.web_connect(body, headers)
        if path == "/api/web_login":
            return await self.web_login(body, headers)
        for route, fn in (("/api/web_hand_info", self.web_hand_info), ("/api/web_register", self.web_register),
                          ("/api/web_signin", self.web_signin), ("/api/web_signin_status", self.web_signin_status)):
            if path == route:
                return await fn(body, headers)
        user = validate_init_data(headers.get("x-init-data", ""), self.d.token)
        guest = via_web = False
        if not user and headers.get("x-web-key"):  # a parent on the website (signed in with username + password)
            a = await self.acct_from(headers["x-web-key"])
            if not a:
                return err(401, "You were signed out. Please sign in again.")
            u = await self.d.db().users.find_one({"_id": a["_id"]}) or {}
            user, via_web = {"id": a["_id"], "first_name": u.get("first_name") or ""}, True
        if not user and headers.get("x-guest-key"):
            g = await self.guest_from(headers["x-guest-key"])
            if not g:
                return err(401, "This link isn't active any more. Please ask for a new one.")
            user, guest = {"id": g["_id"], "first_name": (g["person"] or {}).get("name", "")}, True
        if not user:
            return err(401, "Please close this and open the app again from Telegram.")
        if guest and path not in self.GUEST_PATHS:
            return err(403, "Not available on the web link.")
        uid = user["id"]
        owner = self.d.is_owner(uid)
        db = self.d.db()
        if via_web and (owner or path.startswith("/api/admin/") or path.startswith("/api/helper/") or path == "/api/web_handoff"):
            return err(403, "Not available on the website.")
        try:
            req = json.loads(body.decode() or "{}")
            if not isinstance(req, dict):
                raise ValueError
        except (ValueError, RecursionError):
            return err(400, "Bad request")

        if path.startswith("/api/admin/"):  # the owner's tools
            name = path[len("/api/admin/"):]
            if not owner:
                return err(403, "Not available.")
            if name not in self.d.admin_actions:
                return err(404, "Not found")
            status, data = await self.d.admin(name, req)
            if name == "sugs" and status == 200:
                for row in data["rows"]:
                    row["cards"] = [self.sug_card(row, row["id"], i, uid) for i in range(min(row["n"], 10))]
            return jresp(data, status)

        if path in ("/api/helper/families", "/api/helper/link", "/api/helper/newfam"):  # (the photographer letting someone in on their phone)
            if owner:
                return err(403, "This is for clients.")
            status, data = await self.d.helper(uid, user, path.rsplit("/", 1)[1], req)
            return jresp(data, status)

        if owner:
            tid = await self.owner_family(uid, req)
        else:
            tid = await self.family_of(uid)
            if not tid:
                return err(403, "Please send /start to the bot and link your account first.")
        if path == "/api/a2hs":  # they've seen the "add to home screen" pop-up the photographer asked for
            if not owner:
                coll = self.d.db().webpeople if guest else self.d.db().users
                await coll.update_one({"_id": uid}, {"$unset": {"a2hs": ""}})
            return jresp({"ok": True})

        if path == "/api/web_handoff":  # "Open on the website" (from Telegram): a one-use pass that opens the website for this person
            if owner:
                return err(403, "This is for clients.")
            status, data = await self.d.web_handoff(uid, user)
            return jresp(data, status)
        if path == "/api/web_account":  # sign out the website devices, or delete the website login
            if guest or owner:
                return err(403, "Not available.")
            a = await db.webaccts.find_one({"_id": uid})
            if req.get("action") == "remove":
                await db.webaccts.delete_one({"_id": uid})
            elif a:
                await db.webaccts.update_one({"_id": uid}, {"$inc": {"gen": 1}})
            return jresp({"ok": True})
        if path == "/api/web_pin":  # a web person choosing (or removing) their own 4-digit PIN
            if not guest or uid >= 0 or not await self.person_of(uid):
                return err(403, "Not available.")
            pin = re.sub(r"\D", "", str(req.get("pin") or ""))
            if req.get("pin") in (None, ""):
                await db.webpeople.update_one({"_id": uid}, {"$unset": {"pin": ""}})
            elif len(pin) != 4 or len(set(pin)) == 1:
                return err(400, "Please choose 4 different-looking digits (not 1111).")
            else:
                await db.webpeople.update_one({"_id": uid}, {"$set": {"pin": self.pin_hash(uid, pin)}})
            return jresp({"ok": True})
        if path == "/api/child":  # the parents' own "Web access" screen
            if owner:
                return err(403, "This is for clients.")
            status, data = await self.d.child(tid, req)
            return jresp(data, status)
        if path == "/api/chat":
            if owner:
                return err(403, "This is for clients.")
            docs = (await db.comments.find({"uid": uid}).sort("at", -1).limit(50).to_list())[::-1]
            lang = await self.d.get_lang(uid)
            async def shown(d):  # (the photographer's words, in the reader's language)
                return await self.d.for_reader(d.get("text", ""), lang) if d.get("dir") == "out" else (d.get("text", ""), None)
            pairs = await asyncio.gather(*[shown(d) for d in docs])  # (all at once, not one by one)
            msgs = [{"id": d["_id"], "dir": d.get("dir", "in"), "text": t, "at": self.d.fmt_when(d.get("at")),
                     **({"orig": orig} if orig else {})} for d, (t, orig) in zip(docs, pairs)]
            return jresp({"messages": msgs})

        if path in ("/api/chat_delete", "/api/chat_clear", "/api/feedback", "/api/feedback_list", "/api/feedback_delete"):
            if owner:
                return err(403, "This is for clients.")
            if path == "/api/chat_delete":
                return jresp({"deleted": await self.d.chat_delete(uid, req.get("id"))})
            if path == "/api/chat_clear":
                return jresp({"deleted": await self.d.chat_clear(uid)})
            if path == "/api/feedback_list":
                return jresp({"rows": await self.d.feedback_list(uid)})
            if path == "/api/feedback_delete":
                return jresp({"deleted": await self.d.feedback_delete(uid, req.get("id"))})
            status, data = await self.d.feedback(uid, req.get("text"))
            return jresp(data, status) if status == 200 else err(status, data["message"])

        if path == "/api/lang":
            if owner:
                return err(403, "This is for clients.")
            return jresp({"lang": await self.d.set_lang(uid, req.get("lang"))})

        if path == "/api/langs":
            if owner:
                return err(403, "This is for clients.")
            return jresp(await self.d.langs_list())

        if path == "/api/tr":  # English app texts -> the person's language (saved after the first time)
            if owner:
                return err(403, "This is for clients.")
            lang = req.get("lang")
            strings = req.get("strings")
            if not isinstance(lang, str) or not isinstance(strings, list) or len(strings) > 120:
                return err(400, "Bad request")
            now = time.time()
            hits = [t for t in self.tr_hits.get(uid, []) if now - t < 60]
            if len(hits) >= 12:
                return err(429, "One moment please.")
            self.tr_hits[uid] = hits + [now]
            if len(self.tr_hits) > 2000:
                self.tr_hits = {k: v for k, v in self.tr_hits.items() if v and now - v[-1] < 60}
            return jresp({"tr": await self.d.ui_tr(strings, lang)})

        if path == "/api/comment":
            if owner:
                return err(403, "This is for clients.")
            status, data = await self.d.comment(uid, req.get("text"))
            return jresp(data, status) if status == 200 else err(status, data["message"])

        if not tid:  # the owner, with no family picked yet
            if path == "/api/home":
                return jresp({"admin": True, "family": "", "first": user.get("first_name") or "", "best": False,
                              "help": self.d.help_text(), "copyright": self.d.copyright, "owner": self.d.owner_name,
                              "welcome": None})
            return err(403, "Pick a family in the Admin tab first.")

        if path == "/api/home":
            lang = await self.d.get_lang(uid)
            if not owner and not guest:
                await self.d.db().users.update_one({"_id": uid}, {"$set": {"last_open": datetime.now(timezone.utc)}})
                if via_web:
                    await self.d.db().webaccts.update_one({"_id": uid}, {"$set": {"last_open": datetime.now(timezone.utc)}})
            gp = await self.person_of(uid) if guest else None
            if gp:
                await self.d.db().webpeople.update_one({"_id": uid}, {"$set": {"last_open": datetime.now(timezone.utc)}})  # (so the photographer can tell who has opened the app)
            return jresp({
                "favs": 0 if owner else await self.fav_count(uid, tid),
                "family": await self.d.family_name(tid) or "",
                "first": user.get("first_name") or "",
                "best": (await db.photos.count_documents({**await self.vis(tid, uid), "best": True}) > 0) if guest else await self.d.has_best(tid),
                "admin": owner,
                "help": (await self.d.for_reader(self.d.help_text(), lang))[0] if (not owner and lang != "en") else self.d.help_text(),
                "copyright": self.d.copyright,
                "owner": self.d.owner_name,
                "lang": lang if owner is False else "en",
                "lang_set": True if (owner or guest) else await self.d.lang_chosen(uid),
                "guest": guest,
                "dl": gp.get("dl") is not False if gp else True,
                "pin": bool(gp and gp.get("pin")),
                "site": None if (guest or owner) else ((await db.webaccts.find_one({"_id": uid}) or {}).get("username") or ""),
                "via_web": via_web,
                "a2hs": False if owner else (bool(gp and gp.get("a2hs")) if guest else bool((await self.d.db().users.find_one({"_id": uid}) or {}).get("a2hs"))),  # (the photographer asked them to add the app to their home screen)
                "child": False if (owner or guest) else bool((await db.families.find_one({"thread_id": tid}) or {}).get("child_ok")),
                "welcome": await self.welcome(uid, tid),
            })

        if path == "/api/dates":
            cur = await db.photos.aggregate([
                {"$match": {"date": {"$type": "string"}, **await self.vis(tid, uid)}},
                {"$group": {"_id": "$date", "n": {"$sum": 1}, "cover": {"$min": "$message_id"}}},
                {"$sort": {"_id": -1}},
            ])
            rows = await cur.to_list()
            return jresp({"rows": [
                {"v": r["_id"], "label": self.pd(r["_id"]), "n": r["n"],
                 "cover": self.photo_url(r["cover"], "thumb", uid)} for r in rows]})

        if path == "/api/places":
            cur = await db.photos.aggregate([
                {"$match": await self.vis(tid, uid)},
                {"$group": {"_id": {"$ifNull": ["$loc_id", "none"]}, "loc": {"$first": "$location"}, "n": {"$sum": 1},
                            "cover": {"$min": "$message_id"}}},
            ])
            rows = await cur.to_list()
            rows.sort(key=lambda r: (r["_id"] == "none", (r["loc"] or "").casefold()))
            return jresp({"rows": [
                {"v": r["_id"], "label": r["loc"] or "No location", "n": r["n"],
                 "cover": self.photo_url(r["cover"], "thumb", uid)} for r in rows]})

        if path == "/api/photos":
            mode, value = req.get("mode"), req.get("value")
            q = await self.vis(tid, uid)  # (always the caller's own family + its groups)
            sort = [("date", 1), ("message_id", 1)]
            if mode == "date":
                if not isinstance(value, str) or len(value) != 10:
                    return err(400, "Bad request")
                if isinstance(q.get("date"), dict) and value not in q["date"].get("$in", []):
                    return jresp({"items": [], "more": False})  # (a day this person isn't allowed to see)
                q["date"] = value
            elif mode == "loc":
                if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{10}|none", value):
                    return err(400, "Bad request")
                q["loc_id"] = {"$in": ["none", None]} if value == "none" else value
            elif mode == "best":
                q["best"] = True
                sort = [("date", -1), ("message_id", 1)]  # newest day first
            elif mode == "fav":  # this person's own hearts (only photos their family can still see)
                mine = [f["mid"] for f in await db.favs.find({"uid": uid}).sort("at", -1).limit(FAV_MAX).to_list()]
                q["message_id"] = {"$in": mine}
                sort = [("date", -1), ("message_id", 1)]
            elif mode == "sug":
                q["thread_id"] = tid  # (suggestions belong to one family only)
                q["sug"] = True
            elif mode == "new":
                q["sug"] = {"$ne": True}
                q["added"] = {"$gte": datetime.now(timezone.utc) - timedelta(days=NEW_DAYS)}
                sort = [("added", -1), ("message_id", -1)]
            elif mode == "all":
                sort = [("date", -1), ("message_id", 1)]  # newest day first, so recent photos come first
            else:
                return err(400, "Bad request")
            skip = req.get("skip", 0)
            if not isinstance(skip, int) or isinstance(skip, bool):
                return err(400, "Bad request")
            skip = max(0, min(skip, 100000))
            docs = await db.photos.find(q).sort(sort).skip(skip).limit(PAGE + 1).to_list()
            return jresp({"items": await self.cards(docs[:PAGE], uid), "more": len(docs) > PAGE})

        if path == "/api/fav":
            on = req.get("on")
            doc = await self.my_photo(req.get("id"), tid, uid)
            if not doc or not isinstance(on, bool):
                return err(404, "Photo not found")
            if owner:  # (the owner's preview never saves anything about them)
                return jresp({"ok": True, "fav": on, "favs": 0})
            db = self.d.db()
            key = f"{uid}:{doc['message_id']}"
            if on:
                if await db.favs.count_documents({"uid": uid}) >= FAV_MAX and not await db.favs.find_one({"_id": key}):
                    return err(400, f"That's the most favorites I can keep ({FAV_MAX}). Please un-heart a few first.")
                await db.favs.update_one({"_id": key}, {"$set": {"uid": uid, "mid": doc["message_id"], "tid": tid, "at": datetime.now(timezone.utc)}}, upsert=True)
            else:
                await db.favs.delete_one({"_id": key})
            return jresp({"ok": True, "fav": on, "favs": await self.fav_count(uid, tid)})

        if path == "/api/seen":  # a photo was opened full-screen (so the photographer can see what was looked at)
            doc = await self.my_photo(req.get("id"), tid, uid)
            if not doc or owner or doc.get("sug"):
                return jresp({"ok": True})
            await self.d.db().seen.update_one({"_id": f"{uid}:{doc['message_id']}"}, {"$set": {"uid": uid, "mid": doc["message_id"], "tid": tid, "at": datetime.now(timezone.utc)}}, upsert=True)
            return jresp({"ok": True})

        if path == "/api/send_original":
            doc = await self.my_photo(req.get("id"), tid)
            if not doc:
                return err(404, "Photo not found")
            now = time.time()
            if now - self.last_orig.get(uid, 0) < 3:
                return err(429, "One moment please, then tap again.")
            self.last_orig[uid] = now
            if len(self.last_orig) > 2000:
                self.last_orig = {k: v for k, v in self.last_orig.items() if now - v < 60}
            ok = await self.d.send_original(uid, doc)
            return jresp({"ok": True}) if ok else err(502, "I couldn't send it. Please open the chat with me and try again.")

        return err(404, "Not found")

    async def fav_count(self, uid: int, tid: int) -> int:
        """How many hearts this person has on photos their family can still see."""
        db = self.d.db()
        mine = [f["mid"] for f in await db.favs.find({"uid": uid}).limit(FAV_MAX).to_list()]
        if not mine:
            return 0
        q = await self.vis(tid, uid)
        q["message_id"] = {"$in": mine}
        return await db.photos.count_documents(q)

    async def my_photo(self, mid, tid: int, uid: int | None = None):
        if not isinstance(mid, int) or isinstance(mid, bool) or abs(mid) > 2**53:
            return None
        doc = await self.d.db().photos.find_one({"message_id": mid})
        ok = doc and doc.get("thread_id") in await self.d.visible_tids(tid) and tid not in (doc.get("skip") or [])
        if ok and uid is not None and uid < 0:  # a web person the parents limited to Best shots or some days
            p = await self.person_of(uid)
            if p and ((p.get("scope") == "best" and not doc.get("best")) or (p.get("scope") == "dates" and doc.get("date") not in (p.get("dates") or []))):
                ok = False
        return doc if ok else None

    def sug_card(self, row: dict, sid: str, i: int, uid: int) -> dict:
        kd = (row.get("kinds") or [{}] * (i + 1))[i] if i < len(row.get("kinds") or [1]) else {}
        vid = kd.get("k") == "video"
        return {"id": None, "sug": True, "k": "video" if vid else "photo", **({"dur": kd.get("dur", 0), "mb": 0} if vid else {}), "d": "", "dt": row["dt"], "loc": row["place"], "by": row["from"],
                "best": False, "t": self.sug_url(sid, i, "thumb", uid), "f": self.sug_url(sid, i, "full", uid)}

    def sug_sign(self, sid: str, i: int, size: str, uid: int, exp: int) -> str:
        return hmac.new(self.url_secret, f"s.{sid}.{i}.{size}.{uid}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]

    def sug_url(self, sid: str, i: int, size: str, uid: int) -> str:
        exp = (int(time.time()) // URL_LIFETIME + 2) * URL_LIFETIME
        return f"/simg/{sid}/{i}?s={size}&u={uid}&e={exp}&sig={self.sug_sign(sid, i, size, uid, exp)}"

    async def suggestion_image(self, rest: str, q: dict) -> Resp:
        """A photo that a family suggested and is waiting for the owner. Owner only."""
        try:
            sid, i_s = rest.split("/")
            i, uid, exp = int(i_s), int(q.get("u", "")), int(q.get("e", ""))
        except ValueError:
            return err(404, "Not found")
        size, sig = q.get("s"), q.get("sig", "")
        if size not in ("thumb", "full") or not re.fullmatch(r"[0-9a-f]{4,20}", sid) or not re.fullmatch(r"[0-9a-f]{32}", sig):
            return err(404, "Not found")
        if not hmac.compare_digest(self.sug_sign(sid, i, size, uid, exp), sig) or exp < time.time():
            return err(403, "This link expired. Close and reopen the app.")
        if not self.d.is_owner(uid):
            return err(404, "Not found")
        item = await self.d.suggestion_item(sid, i)
        if not item or not isinstance(item.get("file_id"), str):
            return err(404, "Not found")
        doc = {"message_id": f"sug:{sid}:{i}", "file_id": item["file_id"], "kind": item.get("kind", "photo"),
               "thumb": item.get("thumb")}
        hit = self.cache.get((doc["message_id"], size))
        good = True
        if hit is None:
            made = await self.made_once(doc, size)
            if made is None:
                return err(404, "Couldn't load this photo")
            hit, good = made[:2], made[2]
        return Resp(200, hit[1], hit[0], {"Cache-Control": "private, max-age=3600" if good else "no-store"})

    # ---------- images ----------

    async def image(self, mid_s: str, q: dict, download: bool = False) -> Resp:
        try:
            mid, uid, exp = int(mid_s), int(q.get("u", "")), int(q.get("e", ""))
        except ValueError:
            return err(404, "Not found")
        size = q.get("s")
        if size not in ("thumb", "full"):
            return err(404, "Not found")
        sig = q.get("sig", "")
        if not re.fullmatch(r"[0-9a-f]{32}", sig) or not hmac.compare_digest(self.sign(mid, size, uid, exp), sig) \
                or exp < time.time():
            return err(403, "This link expired. Close and reopen the app.")
        if self.d.is_owner(uid):  # the owner may see every family's photos (but a deleted family's are gone)
            doc = await self.d.db().photos.find_one({"message_id": mid})
            if doc and not await self.d.owner_can_see(doc.get("thread_id")):
                doc = None
        else:
            tid = await self.family_of(uid)  # (checked every time, so removing someone cuts them off)
            doc = await self.my_photo(mid, tid, uid) if tid else None
        if not doc or (download and (size != "full" or doc.get("kind") == "video")):
            return err(404, "Not found")
        if download and uid < 0:
            p = await self.person_of(uid)
            if p and p.get("dl") is False:
                return err(403, "Downloads are switched off for this link.")
        hit = self.cache.get((mid, size))
        good = True
        if hit is None:
            made = await self.made_once(doc, size)
            if made is None:
                return err(404, "Couldn't load this photo")
            hit, good = made[:2], made[2]
        ctype, data = hit
        if download:
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(ctype, "jpg")
            return Resp(200, data, ctype, {"Cache-Control": "private, max-age=3600",
                                            "Content-Disposition": f'attachment; filename="photo-{mid}.{ext}"'})
        # a blurry stand-in (Telegram was slow) must not be kept by the phone, so the sharp one can load next time
        return Resp(200, data, ctype, {"Cache-Control": "private, max-age=86400" if good else "no-store"})

    async def download(self, file_id: str) -> bytes | None:
        try:
            async with self.dl_gate:
                tgfile = await asyncio.wait_for(self.d.bot().get_file(file_id), 30)
                return bytes(await asyncio.wait_for(tgfile.download_as_bytearray(), 60))
        except Exception as e:  # too big for bots (20 MB), deleted, or Telegram was slow
            log.warning("Couldn't fetch a file from Telegram: %s", e)
            return None

    async def small_preview(self, doc: dict):
        """Telegram's own small preview of the photo (tiny, fast, and works for files over 20 MB too)."""
        tid = doc.get("thumb")
        raw = await self.download(tid) if tid else None
        if raw and sniff(raw) != "application/octet-stream":
            return (sniff(raw), raw)
        return None

    async def make_image(self, doc: dict, size: str):
        """-> (content type, bytes, is_sharp) or None. is_sharp is False for a blurry stand-in."""
        mid = doc["message_id"]
        if doc.get("kind") == "video":  # (a bot can't download big videos: the app shows Telegram's own preview picture)
            small = await self.small_preview(doc)
            if not small:
                return None
            self.cache.put((mid, size), small)
            return (*small, True)
        if size == "thumb":
            small = await self.small_preview(doc)
            if small:
                self.cache.put((mid, "thumb"), small)
                return (*small, True)

        async def stand_in():  # only for the big view: a blurry picture beats an empty screen
            small = await self.small_preview(doc) if size == "full" else None
            return (*small, False) if small else None

        async with self.big_gate:  # (at most 2 big files in memory at once: the host has 512 MB)
            raw = await self.download(doc["file_id"])
            if raw is None:
                return await stand_in()
            if size == "full" and doc.get("kind") == "photo" and sniff(raw) == "image/jpeg":
                res = ("image/jpeg", raw)  # a normal Telegram photo is already screen-sized
                self.cache.put((mid, size), res)
                return (*res, True)
            side, quality = (THUMB_SIDE, 72) if size == "thumb" else (FULL_SIDE, 84)
            try:
                async with self.cpu_gate:
                    data = await asyncio.to_thread(resize, raw, side, quality)
                res = ("image/jpeg", data)
            except ImportError:
                ct = sniff(raw)
                if ct == "application/octet-stream" or (size == "thumb" and len(raw) > 3_000_000):
                    return await stand_in()
                res = (ct, raw)
            except Exception as e:  # e.g. HEIC, which this server can't decode
                log.warning("Couldn't decode photo %s: %s", mid, e)
                return await stand_in()
            self.cache.put((mid, size), res)
            return (*res, True)

    async def made_once(self, doc: dict, size: str):
        """Many people (or one fast swipe) asking for the same picture share ONE download and resize."""
        key = (doc["message_id"], size)
        fut = self.inflight.get(key)
        if fut is None:
            fut = asyncio.ensure_future(self.make_image(doc, size))
            self.inflight[key] = fut
            fut.add_done_callback(lambda _f, k=key: self.inflight.pop(k, None))
        return await asyncio.shield(fut)

    # ---------- the socket layer ----------

    async def on_connection(self, reader, writer) -> None:
        if self.conns >= MAX_CONNECTIONS:
            try:  # say so, instead of hanging up silently (after reading the request, or the reply gets lost)
                await asyncio.wait_for(reader.read(4096), 0.3)
                writer.write(b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 5\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await asyncio.wait_for(writer.drain(), 2)
            except Exception:
                pass
            writer.close()
            return
        self.conns += 1
        try:
            await self.serve(reader, writer)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                ConnectionError, OSError):
            pass
        except Exception:
            log.exception("Connection failed")
        finally:
            self.conns -= 1
            try:
                writer.close()
            except Exception:
                pass

    async def serve(self, reader, writer) -> None:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 8)
        lines = raw.decode("latin-1").split("\r\n")
        try:
            method, target, _ = lines[0].split(" ", 2)
        except ValueError:
            return
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        body = b""
        if method == "POST":
            try:
                n = int(headers.get("content-length", "0"))
            except ValueError:
                n = -1
            is_upload = urlsplit(target).path == UPLOAD_PATH
            if is_upload:
                denied = self.upload_denied(headers)  # (only the owner may make us read a big body)
                if denied:
                    await self.write(writer, denied, False)
                    return
            if n < 0 or n > (MAX_UPLOAD if is_upload else MAX_JSON):
                await self.write(writer, Resp(413, b'{"error":"Too big. Photos can be up to 30 MB here; send bigger files through the chat."}'), False)
                return
            if n and is_upload:
                buf, left, deadline = bytearray(), n, time.time() + 600
                while left:
                    part = await asyncio.wait_for(reader.read(min(left, 262144)), 30)
                    if not part or time.time() > deadline:
                        raise asyncio.IncompleteReadError(bytes(buf), n)
                    buf += part
                    left -= len(part)
                body = bytes(buf)
                del buf  # (don't hold two copies of a big upload)
            elif n:
                body = await asyncio.wait_for(reader.readexactly(n), 30)
        resp = await self.handle(method, target, headers, body)
        await self.write(writer, resp, method == "HEAD")

    async def write(self, writer, r: Resp, head: bool) -> None:
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
                  409: "Conflict", 413: "Payload Too Large", 504: "Gateway Timeout", 503: "Service Unavailable", 429: "Too Many Requests",
                  500: "Internal Server Error", 502: "Bad Gateway"}.get(r.status, "OK")
        hdr = {"Content-Type": r.ctype, "Content-Length": str(len(r.body)), "Connection": "close", **SECURITY, **r.headers}
        out = f"HTTP/1.1 {r.status} {reason}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdr.items()) + "\r\n"
        writer.write(out.encode("latin-1") + (b"" if head else r.body))
        await asyncio.wait_for(writer.drain(), 60)

    async def start(self, port: int):
        server = await asyncio.start_server(self.on_connection, "0.0.0.0", port, limit=65536)
        log.info("Web server listening on port %s", port)
        return server
