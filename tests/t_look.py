import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fake
os.environ.update(BOT_TOKEN="1:x", OWNER_ID="1", GROUP_ID="-100", REVIEW_PIN="87654321", HELPER_PIN="11112222", MONGODB_URI="x", WEBAPP_URL="https://x.dev")
import bot
from datetime import datetime, timezone
class Msg:
    def __init__(s, i, photo=True): s.message_id = i; s.photo = [type("P", (), {"width": 100, "file_id": "T"})()] if photo else None; s.document = None; s.video = None
    def __getattr__(s, n): return None
class FakeBot:
    def __init__(s): s.sent = []; s.deleted = []; s.n = 700; s.edits = []; s.texts = []
    async def send_message(s, chat, text, **k): s.n += 1; s.texts.append((chat, text, k)); return Msg(s.n, False)
    async def delete_message(s, chat, mid): s.deleted.append(mid)
    async def delete_messages(s, chat, ids): s.deleted += list(ids)
    async def edit_message_caption(s, chat, mid, caption=None, **k): s.edits.append((mid, caption))
    async def edit_message_text(s, *a, **k): pass
    async def send_photo(s, chat, photo=None, **k): s.n += 1; s.sent.append((chat, "photo", k)); return Msg(s.n)
    async def send_media_group(s, chat, media, **k): out = [Msg(s.n + i + 1) for i in range(len(media))]; s.n += len(media); s.sent.append((chat, "group", len(media))); return out
results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name + (" " + str(extra) if extra and not cond else ""))
async def main():
    d = fake.DB(); bot.db = d; b = FakeBot()
    d.users.d += [{"_id": 10, "upline": True, "trusted": True, "thread_id": 5, "first_name": "Anna"}]
    d.families.d += [{"_id": "f5", "thread_id": 5, "name": "Smith", "code": "123456"}]
    d.suggestions.d += [{"_id": "aaaa1111", "status": "line", "from_id": 10, "from_name": "Anna", "thread_id": 5, "others": [], "date": "2026-06-03", "place": "Park",
        "items": [{"file_id": "F1", "kind": "photo"}, {"file_id": "F2", "kind": "photo"}, {"file_id": "F3", "kind": "photo"}], "created": datetime.now(timezone.utc), "done": [], "prompt_id": 9, "board_ids": [1, 2, 3], "topic": 77}]
    # 1) trusted publish by itself
    await bot.auto_publish(b, "aaaa1111")
    doc = d.suggestions.d[0]
    check("published (approved)", doc["status"] == "approved", doc["status"])
    check("pub recorded for each file", len(doc.get("pub", [])) == 3, doc.get("pub"))
    check("only first message carries the caption flag", [p["c"] for p in doc["pub"]] == [True, False, False], doc.get("pub"))
    check("auto flag set", doc.get("auto") is True)
    owner_msgs = [t for t in b.texts if t[0] == 1]
    check("owner got the ⭐ note", any("published 3 files" in t[1] and "/look" in t[1] for t in owner_msgs), [t[1] for t in owner_msgs])
    check("note id stored", doc.get("note_mid"), doc)
    check("photos indexed", len(d.photos.d) == 3)
    # 2) /look as a reply to the note
    class U: id = 1
    class Reply: message_id = doc["note_mid"]
    replies = []
    class M:
        chat_id = 1; reply_to_message = Reply()
        async def reply_text(s, text, **k): replies.append((text, k))
    class Upd: effective_message = M(); effective_user = U()
    class Ctx: bot = b; user_data = {}
    await bot.look_cmd(Upd(), Ctx())
    check("look sent the photos (one album)", any(s[1] == "group" and s[2] == 3 for s in b.sent), b.sent)
    check("look showed the control message with 5 buttons", replies and sum(len(r) for r in replies[-1][1]["reply_markup"].inline_keyboard) == 5, replies)
    # look from a stranger
    class U2: id = 99
    class Upd2: effective_message = M(); effective_user = U2()
    n0 = len(b.sent); await bot.look_cmd(Upd2(), Ctx()); check("stranger gets nothing", len(b.sent) == n0)
    # no reply -> latest
    class M3(M): reply_to_message = None
    class Upd3: effective_message = M3(); effective_user = U()
    n0 = len(b.sent); await bot.look_cmd(Upd3(), Ctx()); check("plain /look shows latest trusted batch", len(b.sent) > n0)
    # 3) change date through the text step
    ctx = Ctx(); ctx.user_data = {"lk": {"sid": "aaaa1111", "what": "d", "until": 9e12}}
    out = []
    class MT:
        text = "2026-05-20"
        async def reply_text(s, text, **k): out.append(text)
    class UpdT: message = MT(); effective_user = U()
    await bot.handle_look_text(UpdT(), ctx, ctx.user_data["lk"])
    check("date changed in gallery", all(p["date"] == "2026-05-20" for p in d.photos.d), [p["date"] for p in d.photos.d])
    check("caption edited once (first message)", len(b.edits) == 1 and "May 20, 2026" in b.edits[0][1] and "Park" in b.edits[0][1], b.edits)
    check("lk flow cleared", "lk" not in ctx.user_data)
    MT.text = "next tuesday"; ctx.user_data["lk"] = {"sid": "aaaa1111", "what": "d", "until": 9e12}; out.clear()
    await bot.handle_look_text(UpdT(), ctx, ctx.user_data["lk"]); check("bad date asks again", "couldn't read" in out[0] and "lk" in ctx.user_data, out)
    # place
    MT.text = "Beach"; ctx.user_data["lk"] = {"sid": "aaaa1111", "what": "p", "until": 9e12}; b.edits.clear()
    await bot.handle_look_text(UpdT(), ctx, ctx.user_data["lk"])
    check("place changed", all(p["location"] == "Beach" and p["loc_id"] == bot.loc_id("Beach") for p in d.photos.d))
    MT.text = "none"; ctx.user_data["lk"] = {"sid": "aaaa1111", "what": "p", "until": 9e12}
    await bot.handle_look_text(UpdT(), ctx, ctx.user_data["lk"])
    check("place cleared", all(p["location"] is None and p["loc_id"] == "none" for p in d.photos.d))
    # 4) delete one photo from "Delete some"
    class Q:
        data = "lk:r:aaaa1111:%d" % d.photos.d[0]["message_id"]
        from_user = U()
        message = type("MM", (), {"chat_id": 1})()
        async def answer(s, *a, **k): pass
        async def edit_message_caption(s, *a, **k): pass
    class UpdQ: callback_query = Q()
    first = d.photos.d[0]["message_id"]
    await bot.on_look_button(UpdQ(), Ctx())
    check("one photo deleted", first in b.deleted and not any(p["message_id"] == first for p in d.photos.d), b.deleted)
    check("others kept", len(d.photos.d) == 2)
    # a stranger pressing a button
    alerts = []
    class Q2(Q):
        from_user = U2()
        async def answer(s, *a, **k): alerts.append(a)
    class UpdQ2: callback_query = Q2()
    n = len(b.deleted); await bot.on_look_button(UpdQ2(), Ctx()); check("stranger cannot delete", len(b.deleted) == n and alerts)
    # 5) delete all
    class Q3(Q):
        data = "lk:y:aaaa1111"
        async def edit_message_text(s, *a, **k): pass
    class UpdQ3: callback_query = Q3()
    await bot.on_look_button(UpdQ3(), Ctx())
    check("delete all empties the index", len(d.photos.d) == 0, d.photos.d)
    n0 = len(b.sent); replies.clear(); await bot.look_cmd(Upd(), Ctx())
    check("look after delete says gone", replies and "gone" in replies[-1][0], replies)
asyncio.run(main())
bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed"); sys.exit(1 if bad else 0)
