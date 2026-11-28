import sys, os, asyncio, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fake
os.environ.update(BOT_TOKEN="1:x", OWNER_ID="1", GROUP_ID="-100", MONGODB_URI="x", WEBAPP_URL="https://x.dev", VK_TOKEN="t", VK_GROUP_ID="777")
import bot, vkbot
from datetime import datetime, timezone, timedelta
results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name + (" " + str(extra) if extra and not cond else ""))
class TG:
    def __init__(s): s.texts = []
    async def send_message(s, chat, text, **k): s.texts.append((chat, text))
    async def get_file(s, fid): raise RuntimeError("not used")
sent, calls = [], []
async def fake_call(method, **p):
    calls.append((method, p))
    if method == "messages.send": sent.append(p); return 1
    if method == "photos.getMessagesUploadServer": return {"upload_url": "u"}
    if method == "photos.saveMessagesPhoto": return [{"id": int(p["photo"][1:]), "owner_id": -777, "access_key": "k"}]
    if method == "docs.getMessagesUploadServer": return {"upload_url": "u"}
    if method == "docs.save": return {"doc": {"id": 9, "owner_id": 5, "access_key": "z"}}
    if method == "users.get": return [{"first_name": "Мария"}]
    return 1
async def fake_upload(url, field, name, data): return {"photo": "x" + data.decode()[1:], "server": 1, "hash": "h"} if field == "photo" else {"file": "f"}
async def fake_dl(fid): return None if fid == "BAD" else ("p" + fid[1:]).encode()
vkbot.call = fake_call; vkbot.upload = fake_upload; vkbot.tg_download = fake_dl
async def main():
    d = fake.DB(); bot.db = d; tg = TG(); bot._APP = type("A", (), {"bot": tg})(); vkbot.B = bot
    now = datetime.now(timezone.utc)
    d.families.d += [{"_id": "a", "thread_id": 5, "name": "Smith", "key": bot.normalize("Smith"), "code": "123456", "web": "tok"},
                    {"_id": "b", "thread_id": 6, "name": "Lee", "key": bot.normalize("Lee"), "code": "654321"}]
    for i in range(11):
        d.photos.d.append({"message_id": 100 + i, "thread_id": 5, "file_id": f"P{i}", "kind": "photo", "date": "2026-06-0%d" % (1 + i // 6), "location": "Парк", "loc_id": "park", "best": i == 0})
    d.photos.d += [{"message_id": 200, "thread_id": 6, "file_id": "P9", "kind": "photo", "date": "2026-06-03", "loc_id": "x"},
                   {"message_id": 300, "thread_id": 5, "file_id": "V", "kind": "video", "date": "2026-06-02", "loc_id": "park", "location": "Парк"}]
    def msg(uid, text="", payload=None, ref=None):
        return {"from_id": uid, "peer_id": uid, "text": text, **({"payload": json.dumps(payload)} if payload else {}), **({"ref": ref} if ref else {})}
    # --- linking by code
    sent.clear(); await vkbot.on_message(msg(50, "hi"))
    check("asks for the first name", "Как вас зовут" in sent[-1]["message"])
    await vkbot.on_message(msg(50, "Мария")); await vkbot.on_message(msg(50, "Smith")); await vkbot.on_message(msg(50, "000000"))
    check("wrong code refused, not linked", not d.vkusers.d and "не подошли" in sent[-1]["message"], sent[-1]["message"])
    await vkbot.on_message(msg(50, "hi")); await vkbot.on_message(msg(50, "Мария")); await vkbot.on_message(msg(50, "Smith")); await vkbot.on_message(msg(50, "123456"))
    check("right code links to the family", d.vkusers.d and d.vkusers.d[0]["thread_id"] == 5 and "Вы в семье" in sent[-1]["message"], d.vkusers.d)
    kb = json.loads(sent[-1]["keyboard"]); labels = [b["action"]["label"] for r in kb["buttons"] for b in r]
    check("menu has the buttons (best, website)", "⭐ Лучшие" in labels and "🌐 Сайт" in labels and "📅 По датам" in labels, labels)
    # --- galleries
    sent.clear(); await vkbot.on_message(msg(50, "", {"c": "all"}))
    first = sent[0]
    check("first batch: 8 slots (7 photos + the video note)", len(first["attachment"].split(",")) == 7 and "1–8 из 12" in first["message"], first)
    check("another family's photo never shown", all("photo-777_200" not in x.get("attachment", "") for x in sent))
    check("video listed as site-only, not uploaded", "🎬" in first["message"] and not any(c[1].get("photo") == "xV" for c in calls))
    ikb = json.loads(first["keyboard"]); btns = [b for r in ikb["buttons"] for b in r]
    check("fav buttons + more + originals, at most 10 buttons", len(btns) <= 10 and any(json.loads(b["action"]["payload"]).get("c") == "more" for b in btns) and any(json.loads(b["action"]["payload"]).get("c") == "orig" for b in btns), len(btns))
    more = [json.loads(b["action"]["payload"]) for b in btns if json.loads(b["action"]["payload"]).get("c") == "more"][0]
    sent.clear(); await vkbot.on_event({"user_id": 50, "peer_id": 50, "event_id": "e", "payload": more})
    check("next batch shows the rest, then 'that's all'", "9–12 из 12" in sent[0]["message"] and "Это всё" in sent[-1]["message"] or "9–11" in sent[0]["message"], [x["message"][:30] for x in sent])
    n_up = len([c for c in calls if c[0] == "photos.saveMessagesPhoto"])
    sent.clear(); await vkbot.on_message(msg(50, "", {"c": "all"}))
    check("photos are uploaded only once (cache)", len([c for c in calls if c[0] == "photos.saveMessagesPhoto"]) == n_up)
    # dates / places
    sent.clear(); await vkbot.on_message(msg(50, "", {"c": "dates"}))
    pk = [json.loads(b["action"]["payload"]) for r in json.loads(sent[-1]["keyboard"])["buttons"] for b in r]
    check("dates listed", {"c": "go", "s": "d:2026-06-01"} in pk and {"c": "go", "s": "d:2026-06-02"} in pk, pk)
    sent.clear(); await vkbot.on_event({"user_id": 50, "peer_id": 50, "event_id": "e", "payload": {"c": "go", "s": "d:2026-06-02"}})
    check("a date shows that day's photos", "из 6" in sent[0]["message"] or "из 7" in sent[0]["message"], sent[0]["message"][:60])
    sent.clear(); await vkbot.on_message(msg(50, "", {"c": "locs"})); check("places listed", sent and "Выберите место" in sent[-1]["message"])
    sent.clear(); await vkbot.on_message(msg(50, "", {"c": "best"})); check("best shots", "1–1 из 1" in sent[0]["message"], sent[0]["message"][:40])
    # favourites
    calls.clear(); await vkbot.on_event({"user_id": 50, "peer_id": 50, "event_id": "e", "payload": {"c": "fav", "m": 101}})
    check("favourite added (+ pop-up answer)", d.vkfavs.d and any(c[0] == "messages.sendMessageEventAnswer" and "Добавлено" in c[1]["event_data"] for c in calls), calls[-1])
    await vkbot.on_event({"user_id": 50, "peer_id": 50, "event_id": "e", "payload": {"c": "fav", "m": 200}})
    check("someone else's family photo can't be favourited", len(d.vkfavs.d) == 1)
    sent.clear(); await vkbot.on_message(msg(50, "", {"c": "favs"})); check("favourites view", "1–1 из 1" in sent[0]["message"])
    await vkbot.on_event({"user_id": 50, "peer_id": 50, "event_id": "e", "payload": {"c": "fav", "m": 101}}); check("tap again removes it", not d.vkfavs.d)
    # originals
    sent.clear(); await vkbot.on_event({"user_id": 50, "peer_id": 50, "event_id": "e", "payload": {"c": "orig", "ms": [101, 102, 200]}})
    check("originals sent as files (own family only)", sent and sent[-1]["attachment"].count("doc") == 2, sent[-1:] )
    # --- invite link
    d.invites.d.append({"_id": "tok123456789", "tid": 6, "name": "Анна", "exp": now + timedelta(hours=5), "used": None})
    sent.clear(); await vkbot.on_message(msg(51, "x", None, "inv_tok123456789"))
    check("invite link logs in with typed name", [u for u in d.vkusers.d if u["_id"] == 51][0]["thread_id"] == 6 and "Анна" in sent[-1]["message"])
    check("owner told", any(t[0] == 1 and "joined" in t[1] for t in tg.texts))
    sent.clear(); await vkbot.on_message(msg(52, "x", None, "inv_tok123456789"))
    check("link works only once", not any(u["_id"] == 52 for u in d.vkusers.d) and "не работает" in sent[-1]["message"])
    # --- only private chats
    n = len(sent); await vkbot.on_message({"from_id": 60, "peer_id": 2000000001, "text": "hi"}); check("group chats ignored", len(sent) == n)
    # --- notifications
    sent.clear(); k = await vkbot.notify_families([5]); check("new-photo note reaches VK people of the family only", k == 1 and sent[0]["peer_id"] == 50, sent)
    # --- lockout
    for i in range(5):
        await vkbot.on_message(msg(70, "hi")); await vkbot.on_message(msg(70, "A")); await vkbot.on_message(msg(70, "Smith")); await vkbot.on_message(msg(70, "111111"))
    sent.clear(); await vkbot.on_message(msg(70, "hi"))
    check("too many wrong codes locks them out", "Слишком много" in sent[-1]["message"] or "Слишком много" in "".join(x["message"] for x in sent), sent)
asyncio.run(main())
bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed"); sys.exit(1 if bad else 0)
