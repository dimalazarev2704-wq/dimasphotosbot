import sys, os, asyncio, re, json, hmac, hashlib, time
from urllib.parse import urlencode, urlsplit
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fake
os.environ.update(BOT_TOKEN="1:x", OWNER_ID="1", GROUP_ID="-100", REVIEW_PIN="87654321", HELPER_PIN="11112222", MONGODB_URI="x", WEBAPP_URL="https://x.dev")
import bot, webapp
from datetime import datetime, timezone
TOKEN = "1:x"
def init(uid):
    pairs = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": "T"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)
results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name + (" " + str(extra)[:200] if extra and not cond else ""))
class FB:
    async def delete_message(s, *a): pass
    async def delete_messages(s, *a): pass
class FakeApp:
    bot = FB(); bot_data = {}
async def main():
    d = fake.DB(); bot.db = d
    d.users.d += [{"_id": 10, "upline": True, "trusted": True, "thread_id": 5, "first_name": "Anna"}, {"_id": 12, "thread_id": 5, "first_name": "Client"}]
    d.families.d += [{"_id": "f5", "thread_id": 5, "name": "Smith"}]
    d.suggestions.d += [{"_id": "aaaa1111", "status": "line", "from_id": 10, "from_name": "Anna", "thread_id": 5, "others": [], "date": "2026-06-03", "place": "Park",
        "items": [{"file_id": "F1", "kind": "photo"}, {"file_id": "F2", "kind": "photo"}], "created": datetime.now(timezone.utc), "done": [], "prompt_id": 9}]
    w = webapp.WebApp(bot.WebDeps(FakeApp()))
    async def call(uid, path, body):
        r = await w.handle("POST", path, {"x-init-data": init(uid), "content-type": "application/json"}, json.dumps(body).encode())
        return r.status, (json.loads(r.body) if r.ctype.startswith("application/json") else r.body)
    s, r = await call(10, "/api/review/list", {}); check("list closed before the code", s == 401, (s, r))
    s, r = await call(12, "/api/review/pin", {"pin": "87654321"}); check("a client can't open it", s == 403, (s, r))
    s, r = await call(1, "/api/review/pin", {"pin": "87654321"}); check("owner account gets a clear refusal", s == 403, (s, r))
    s, r = await call(10, "/api/review/pin", {"pin": "123"}); check("wrong code refused", s == 403, (s, r))
    s, r = await call(10, "/api/review/pin", {"pin": "87654321"}); check("right code ok", s == 200, (s, r))
    s, r = await call(10, "/api/review/list", {}); check("list ok", s == 200 and len(r["rows"]) == 1, (s, r))
    cards = r["rows"][0]["cards"]; check("cards for every file", len(cards) == 2 and all(c["t"].startswith("/simg/aaaa1111/") for c in cards), cards)
    # image access: signature + review session
    t = cards[0]["t"]
    async def get(url, who=None):
        resp = await w.handle("GET", url, {}, b""); return resp.status
    # stub the actual download so we only test authorization
    async def fake_made(doc, size): return (b"JPEGDATA", "image/jpeg", True)
    w.made_once = fake_made
    check("image loads while open", await get(t) == 200, await get(t))
    bad = re.sub(r"sig=[0-9a-f]{32}", "sig=" + "0" * 32, t); check("tampered link refused", await get(bad) == 403)
    other = re.sub(r"u=10", "u=12", t); check("link for another user refused", await get(other) in (403, 404))
    s, r = await call(10, "/api/review/signout", {}); check("sign out ok", s == 200)
    check("image refused after sign out", await get(t) == 404, await get(t))
    s, r = await call(10, "/api/review/list", {}); check("list closed after sign out", s == 401)
    # guest keys can't reach it
    r = await w.handle("POST", "/api/review/list", {"x-guest-key": "1.abc"}, b"{}"); check("web guests can't reach it", r.status in (401, 403), r.status)
asyncio.run(main())
bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed"); sys.exit(1 if bad else 0)
