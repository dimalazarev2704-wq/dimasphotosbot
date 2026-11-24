# Run from the repo folder: python3 -I tests/t_review.py (uses a fake Telegram and a fake in-memory database; no network)
import sys, os, asyncio, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fake
os.environ.update(BOT_TOKEN="1:x", OWNER_ID="1", GROUP_ID="-100", REVIEW_PIN="87654321", HELPER_PIN="11112222", MONGODB_URI="x", WEBAPP_URL="https://x.dev")
import bot
from datetime import datetime, timezone

class Msg:
    def __init__(s, i): s.message_id = i
class FakeBot:
    def __init__(s): s.sent = []; s.deleted = []; s.n = 1000; s.edits = []
    async def send_message(s, chat, text, **k): s.n += 1; s.sent.append((chat, text)); return Msg(s.n)
    async def delete_message(s, chat, mid): s.deleted.append(mid)
    async def delete_messages(s, chat, ids): s.deleted += list(ids)
    async def edit_message_caption(s, chat, mid, caption=None, **k): s.edits.append((mid, caption))
    async def send_photo(s, chat, photo=None, **k): s.n += 1; s.sent.append((chat, "photo")); return Msg(s.n)
    async def send_media_group(s, chat, media, **k): s.sent.append((chat, "group")); return [Msg(s.n + i) for i in range(len(media))]
    async def edit_message_text(s, *a, **k): pass

def setup():
    d = fake.DB(); bot.db = d
    d.users.d += [{"_id": 10, "upline": True, "trusted": True, "thread_id": 5, "first_name": "Anna"},
                  {"_id": 11, "upline": True, "thread_id": 5, "first_name": "Bob"},   # normal photographer
                  {"_id": 12, "thread_id": 5, "first_name": "Client"}]
    d.families.d += [{"_id": "f5", "thread_id": 5, "name": "Smith"}, {"_id": "f6", "thread_id": 6, "name": "Lee"}]
    now = datetime.now(timezone.utc)
    d.suggestions.d += [
        {"_id": "aaaa1111", "status": "line", "from_id": 10, "from_name": "Anna", "thread_id": 5, "others": [], "date": "2026-06-03", "place": "Park", "items": [{"file_id": "F1", "kind": "photo"}, {"file_id": "F2", "kind": "photo"}], "created": now, "done": [], "prompt_id": 9},
        {"_id": "bbbb2222", "status": "approved", "from_id": 10, "from_name": "Anna", "thread_id": 5, "others": [], "date": "2026-05-01", "place": None, "items": [{"file_id": "F3", "kind": "photo"}], "created": now, "pub": [{"m": 501, "t": 5, "c": True}]},
        {"_id": "cccc3333", "status": "line", "from_id": 11, "from_name": "Bob", "thread_id": 5, "others": [], "date": "2026-06-04", "place": None, "items": [{"file_id": "F9", "kind": "photo"}], "created": now, "done": [], "prompt_id": 8},
    ]
    d.photos.d += [{"message_id": 501, "thread_id": 5, "file_id": "F3", "kind": "photo", "date": "2026-05-01"}]
    return d

results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name + (" " + str(extra) if extra and not cond else ""))

async def main():
    d = setup(); b = FakeBot()
    api = bot.review_api
    # who can use it
    s, r = await api(b, 12, {}, "pin", {"pin": "87654321"}); check("client refused", s == 403, (s, r))
    s, r = await api(b, 11, {}, "pin", {"pin": "87654321"}); check("normal photographer refused", s == 403, (s, r))
    s, r = await api(b, 1, {}, "pin", {"pin": "87654321"}); check("owner refused (not trusted role)", s == 403, (s, r))
    # not open yet
    s, r = await api(b, 10, {}, "list", {}); check("list needs the code first", s == 401, (s, r))
    # wrong code
    s, r = await api(b, 10, {}, "pin", {"pin": "11112222"}); check("helper code does NOT work", s == 403, (s, r))
    s, r = await api(b, 10, {}, "pin", {"pin": "00000000"}); check("wrong code refused", s == 403, (s, r))
    s, r = await api(b, 10, {}, "list", {}); check("still closed after wrong codes", s == 401, (s, r))
    # right code
    s, r = await api(b, 10, {}, "pin", {"pin": "87654321"}); check("right code opens", s == 200, (s, r))
    s, r = await api(b, 10, {}, "list", {})
    check("list ok", s == 200, (s, r))
    ids = [x["id"] for x in r["rows"]]
    check("sees own waiting + approved only", ids == ["aaaa1111", "bbbb2222"], ids)
    check("states", [x["state"] for x in r["rows"]] == ["wait", "done"], [x["state"] for x in r["rows"]])
    # other photographer's batch not touchable
    s, r = await api(b, 10, {}, "act", {"id": "cccc3333", "action": "del"}); check("cannot touch someone else's batch", s == 404, (s, r))
    # images only while open
    it = await bot.WebDeps.review_item(bot.WebDeps(None), 10, "aaaa1111", 0); check("image item while open", it and it["file_id"] == "F1", it)
    it = await bot.WebDeps.review_item(bot.WebDeps(None), 10, "cccc3333", 0); check("image of someone else's batch refused", it is None, it)
    it = await bot.WebDeps.review_item(bot.WebDeps(None), 11, "aaaa1111", 0); check("image refused for another user", it is None, it)
    # delete approved
    s, r = await api(b, 10, {}, "act", {"id": "bbbb2222", "action": "del"}); check("delete approved ok", s == 200, (s, r))
    check("approved message deleted in group", 501 in b.deleted, b.deleted)
    check("approved photo gone from index", not any(p["message_id"] == 501 for p in d.photos.d))
    s, r = await api(b, 10, {}, "list", {}); check("approved batch no longer listed", [x["id"] for x in r["rows"]] == ["aaaa1111"], r)
    s, r = await api(b, 10, {}, "act", {"id": "bbbb2222", "action": "go"}); check("cannot publish a deleted batch", s in (400, 404), (s, r))
    # bad input
    s, r = await api(b, 10, {}, "act", {"id": "../x", "action": "del"}); check("bad id rejected", s == 400, (s, r))
    s, r = await api(b, 10, {}, "act", {"id": "aaaa1111", "action": "zzz"}); check("bad action rejected", s == 400, (s, r))
    # let go (publish) of the waiting batch
    published = {}
    async def fake_line_publish(bot_, sid, edit):
        published["sid"] = sid; await d.suggestions.update_one({"_id": sid}, {"$set": {"status": "approved"}})
    bot.line_publish = fake_line_publish
    s, r = await api(b, 10, {}, "act", {"id": "aaaa1111", "action": "go"}); check("let go publishes", s == 200 and published.get("sid") == "aaaa1111", (s, r))
    s, r = await api(b, 10, {}, "act", {"id": "aaaa1111", "action": "go"}); check("second let go refused", s == 400, (s, r))
    # a publish that fails
    d.suggestions.d.append({"_id": "dddd4444", "status": "line", "from_id": 10, "from_name": "Anna", "thread_id": 5, "others": [], "date": "2026-06-05", "items": [], "created": datetime.now(timezone.utc)})
    async def failing(bot_, sid, edit): pass
    bot.line_publish = failing
    s, r = await api(b, 10, {}, "act", {"id": "dddd4444", "action": "go"}); check("failed publish reports error", s == 502, (s, r))
    # reject waiting
    rejected = {}
    async def fake_reject(bot_, sid, edit): rejected["sid"] = sid; await d.suggestions.update_one({"_id": sid}, {"$set": {"status": "rejected"}})
    bot.line_reject = fake_reject
    s, r = await api(b, 10, {}, "act", {"id": "dddd4444", "action": "del"}); check("delete waiting rejects it", s == 200 and rejected.get("sid") == "dddd4444", (s, r))
    # sign out
    s, r = await api(b, 10, {}, "signout", {}); check("sign out ok", s == 200, (s, r))
    s, r = await api(b, 10, {}, "list", {}); check("closed after sign out", s == 401, (s, r))
    s, r = await api(b, 10, {}, "act", {"id": "aaaa1111", "action": "del"}); check("act closed after sign out", s == 401, (s, r))
    it = await bot.WebDeps.review_item(bot.WebDeps(None), 10, "aaaa1111", 0); check("images closed after sign out", it is None, it)
    # timeout
    await api(b, 10, {}, "pin", {"pin": "87654321"})
    d.revsess.d[0]["until"] = time.time() - 1
    s, r = await api(b, 10, {}, "list", {}); check("closes by itself after the time", s == 401, (s, r))
    # trust removed mid-session
    await api(b, 10, {}, "pin", {"pin": "87654321"})
    d.users.d[0]["trusted"] = False
    s, r = await api(b, 10, {}, "list", {}); check("closes when no longer trusted", s == 403, (s, r))
    d.users.d[0]["trusted"] = True
    # lockout after 5 wrong codes
    b2 = FakeBot()
    for _ in range(5): s, r = await api(b2, 10, {}, "pin", {"pin": "00000001"})
    s, r = await api(b2, 10, {}, "pin", {"pin": "87654321"}); check("locked after 5 wrong codes (even right code)", s == 429, (s, r))
    # window text + buttons
    d.attempts.d.clear()
    text, kb = await bot.review_window(10, "p")
    flat = [bt for row in kb.inline_keyboard for bt in row]
    check("window has I-have-Dima button", any("with me" in bt.text and bt.web_app for bt in flat), [bt.text for bt in flat])
    check("window has menu button", any(bt.callback_data == "menu" for bt in flat))
    check("window text", "In process" in text, text)
    text2, _ = await bot.review_window(10, "a"); check("approved view works", "Published" in text2, text2)
    # menu row
    row = await bot.role_row(10); check("trusted gets Add to + In process", [x.callback_data for x in row] == ["ad:go", "rv:p"], [x.callback_data for x in row])
    row = await bot.role_row(11); check("normal photographer gets only Add to", [x.callback_data for x in row] == ["ad:go"], [x.callback_data for x in row])
    # notify + look
    n = 0
asyncio.run(main())
bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed"); sys.exit(1 if bad else 0)
