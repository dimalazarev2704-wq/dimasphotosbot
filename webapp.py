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
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

log = logging.getLogger("photobot.web")

INIT_MAX_AGE = 24 * 3600
URL_LIFETIME = 6 * 3600
PAGE = 90  # photos per page in the grid
WELCOME_ITEMS = 12  # photos on the welcome screen
NEW_DAYS = 14  # how long photos count as "new"
NEW_MIN = 2  # how many new photos it takes to show the "New photos" welcome
THUMB_SIDE, FULL_SIDE = 360, 1600
MAX_JSON = 16 * 1024
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
    if data[:3] == b"GIF":
        return "image/gif"
    return "application/octet-stream"


MAX_PIXELS = 30_000_000  # for PNG and the like, which must be decoded at full size
MAX_JPEG_PIXELS = 150_000_000  # JPEGs are decoded at a reduced size, so even a big camera original is cheap


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
        self.dl_gate = asyncio.Semaphore(4)
        self.cpu_gate = asyncio.Semaphore(1)  # (one CPU: more at once only slows the bot down)
        self.inflight: dict = {}
        self.last_orig: dict[int, float] = {}
        self.conns = 0

    # ---------- plumbing ----------

    def sign(self, mid: int, size: str, uid: int, exp: int) -> str:
        msg = f"{mid}.{size}.{uid}.{exp}".encode()
        return hmac.new(self.url_secret, msg, hashlib.sha256).hexdigest()[:32]

    def photo_url(self, mid: int, size: str, uid: int) -> str:
        # the expiry is rounded, so the same link comes back all day and the phone can keep the picture
        exp = (int(time.time()) // URL_LIFETIME + 2) * URL_LIFETIME
        return f"/img/{mid}?s={size}&u={uid}&e={exp}&sig={self.sign(mid, size, uid, exp)}"

    async def family_of(self, uid: int) -> int | None:
        """The family this person may browse. Unconfirmed people get None. The owner gets only the family
        they picked with /connect (kept in memory by the bot, never saved), so nothing about them is stored."""
        if self.d.is_owner(uid):
            tid = self.d.preview_family(uid)
            if tid and not await self.d.db().families.find_one({"thread_id": tid}):
                return None  # that family was deleted
            return tid
        doc = await self.d.db().users.find_one({"_id": uid})
        if not doc or doc.get("pending"):
            return None
        return doc.get("thread_id")

    async def auth(self, headers: dict):
        """-> (user, thread_id, None) or (None, None, error Resp)."""
        user = validate_init_data(headers.get("x-init-data", ""), self.d.token)
        if not user:
            return None, None, err(401, "Please close this and open the app again from Telegram.")
        tid = await self.family_of(user["id"])
        if self.d.is_owner(user["id"]) and not tid:
            return None, None, err(403, "This app shows a family's photos. In the chat, send /connect and pick a family, "
                                        "then open the app again. (Your own admin view isn't in the app yet.)")
        if not tid:
            return None, None, err(403, "Please send /start to the bot and link your account first.")
        return user, tid, None

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
            if path.startswith("/img/") and method == "GET":
                return await self.image(path[5:], query)
            if path.startswith("/api/") and method == "POST":
                return await self.api(path, headers, body)
            return err(404, "Not found")
        except Exception:
            log.exception("Web request failed: %s %s", method, path)
            return err(500, "Something went wrong. Please try again.")

    def page(self) -> Resp:
        try:
            with open(self.html_path, "rb") as f:
                html = f.read()
        except OSError:
            return Resp(500, b"The app page is missing on the server.", "text/plain")
        return Resp(200, html, "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    # ---------- api ----------

    def pd(self, day) -> str:
        try:
            return self.d.pretty_date(day)
        except Exception:  # an odd date in old data must not break the whole family's screen
            return str(day or "")


    def card(self, doc: dict, uid: int) -> dict:
        return {
            "id": doc["message_id"], "k": doc.get("kind", "document"), "d": doc["date"],
            "dt": self.pd(doc["date"]), "loc": doc.get("location") or "",
            "by": doc.get("by") or "", "best": bool(doc.get("best")),
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
        new_q = {"thread_id": tid, "sug": {"$ne": True}, "added": {"$gte": cutoff}}
        n_new = await db.photos.count_documents(new_q)
        best = await self.d.has_best(tid)
        total = await db.photos.count_documents({"thread_id": tid})
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
            q, sort = {"thread_id": tid, "best": True}, [("message_id", -1)]
            title, sub = "⭐ Best shots", "Picked for you"
        else:
            q, sort = {"thread_id": tid}, [("message_id", -1)]
            title, sub = "📸 Latest photos", ""
        docs = await db.photos.find(q).sort(sort).limit(WELCOME_ITEMS).to_list()
        if mode == "new" and docs and not self.d.is_owner(uid):
            newest_at = max((naive_utc(x.get("added")) for x in docs if x.get("added")), default=None)
            if newest_at:
                await db.users.update_one({"_id": uid}, {"$set": {"seen_new": newest_at}})
        return {"mode": mode, "title": title, "sub": sub, "total": total,
                "items": [self.card(x, uid) for x in docs]}

    async def api(self, path: str, headers: dict, body: bytes) -> Resp:
        user, tid, bad = await self.auth(headers)
        if bad:
            return bad
        uid = user["id"]
        db = self.d.db()
        try:
            req = json.loads(body.decode() or "{}")
            if not isinstance(req, dict):
                raise ValueError
        except (ValueError, RecursionError):
            return err(400, "Bad request")

        if path == "/api/home":
            return jresp({
                "family": await self.d.family_name(tid) or "",
                "first": user.get("first_name") or "",
                "best": await self.d.has_best(tid),
                "help": self.d.help_text(),
                "copyright": self.d.copyright,
                "owner": self.d.owner_name,
                "welcome": await self.welcome(uid, tid),
            })

        if path == "/api/dates":
            cur = await db.photos.aggregate([
                {"$match": {"thread_id": tid, "date": {"$type": "string"}}},
                {"$group": {"_id": "$date", "n": {"$sum": 1}, "cover": {"$min": "$message_id"}}},
                {"$sort": {"_id": -1}},
            ])
            rows = await cur.to_list()
            return jresp({"rows": [
                {"v": r["_id"], "label": self.pd(r["_id"]), "n": r["n"],
                 "cover": self.photo_url(r["cover"], "thumb", uid)} for r in rows]})

        if path == "/api/places":
            cur = await db.photos.aggregate([
                {"$match": {"thread_id": tid}},
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
            q = {"thread_id": tid}  # (always the caller's own family)
            sort = [("date", 1), ("message_id", 1)]
            if mode == "date":
                if not isinstance(value, str) or len(value) != 10:
                    return err(400, "Bad request")
                q["date"] = value
            elif mode == "loc":
                if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{10}|none", value):
                    return err(400, "Bad request")
                q["loc_id"] = {"$in": ["none", None]} if value == "none" else value
            elif mode == "best":
                q["best"] = True
                sort = [("date", -1), ("message_id", 1)]  # newest day first
            elif mode == "sug":
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
            return jresp({"items": [self.card(x, uid) for x in docs[:PAGE]], "more": len(docs) > PAGE})

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

    async def my_photo(self, mid, tid: int):
        if not isinstance(mid, int) or isinstance(mid, bool) or abs(mid) > 2**53:
            return None
        doc = await self.d.db().photos.find_one({"message_id": mid})
        return doc if doc and doc.get("thread_id") == tid else None

    # ---------- images ----------

    async def image(self, mid_s: str, q: dict) -> Resp:
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
        tid = await self.family_of(uid)  # (checked every time, so removing someone cuts them off)
        doc = await self.my_photo(mid, tid) if tid else None
        if not doc:
            return err(404, "Not found")
        hit = self.cache.get((mid, size))
        good = True
        if hit is None:
            made = await self.made_once(doc, size)
            if made is None:
                return err(404, "Couldn't load this photo")
            hit, good = made[:2], made[2]
        ctype, data = hit
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
        if size == "thumb":
            small = await self.small_preview(doc)
            if small:
                self.cache.put((mid, "thumb"), small)
                return (*small, True)

        async def stand_in():  # only for the big view: a blurry picture beats an empty screen
            small = await self.small_preview(doc) if size == "full" else None
            return (*small, False) if small else None

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
            if n < 0 or n > MAX_JSON:
                await self.write(writer, Resp(413, b'{"error":"Too big"}'), False)
                return
            if n:
                body = await asyncio.wait_for(reader.readexactly(n), 30)
        resp = await self.handle(method, target, headers, body)
        await self.write(writer, resp, method == "HEAD")

    async def write(self, writer, r: Resp, head: bool) -> None:
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
                  413: "Payload Too Large", 503: "Service Unavailable", 429: "Too Many Requests",
                  500: "Internal Server Error", 502: "Bad Gateway"}.get(r.status, "OK")
        hdr = {"Content-Type": r.ctype, "Content-Length": str(len(r.body)), "Connection": "close", **SECURITY, **r.headers}
        out = f"HTTP/1.1 {r.status} {reason}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdr.items()) + "\r\n"
        writer.write(out.encode("latin-1") + (b"" if head else r.body))
        await asyncio.wait_for(writer.drain(), 60)

    async def start(self, port: int):
        server = await asyncio.start_server(self.on_connection, "0.0.0.0", port, limit=65536)
        log.info("Web server listening on port %s", port)
        return server
