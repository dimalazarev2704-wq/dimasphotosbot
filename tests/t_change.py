import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fake
os.environ.update(BOT_TOKEN="1:x", OWNER_ID="1", GROUP_ID="-100", MONGODB_URI="x", WEBAPP_URL="https://x.dev")
import bot
from datetime import datetime, timezone, timedelta
results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name + (" " + str(extra) if extra and not cond else ""))
class FB:
    def __init__(s): s.texts = []
    async def send_message(s, chat, text, **k): s.texts.append((chat, text, k))
    async def delete_message(s, *a): pass
    async def delete_messages(s, *a): pass
    async def get_chat_member(s, c, u): raise bot.TelegramError("no")
async def noop(*a, **k): return None
bot.ensure_keyboard = noop
async def main():
    d = fake.DB(); bot.db = d; b = FB()
    now = datetime.now(timezone.utc)
    d.families.d += [{"_id": "a", "thread_id": 5, "name": "Smith"}, {"_id": "b", "thread_id": 6, "name": "Lee"}]
    d.users.d += [{"_id": 20, "thread_id": 5, "first_name": "Mom", "linked_at": now - timedelta(minutes=3)},
                  {"_id": 21, "thread_id": 5, "first_name": "Old", "linked_at": now - timedelta(minutes=30)},
                  {"_id": 22, "thread_id": 5, "first_name": "Dad", "linked_at": (now - timedelta(minutes=2)).replace(tzinfo=None)}]
    d.favs.d += [{"_id": "20:1", "uid": 20}]
    state = {"deleted": 0}
    class M:
        chat_id = -100; message_thread_id = 5; is_topic_message = True; sender_chat = None
        async def delete(s): state["deleted"] += 1
        async def reply_text(s, t, **k): b.texts.append((-100, t, k))
    class U: id = 1
    class Upd: effective_message = M(); effective_user = U()
    class Ctx:
        bot = b
        def __init__(s, args=()): s.args = list(args)
    await bot.change_family_cmd(Upd(), Ctx(["family"]))
    check("command message deleted", state["deleted"] == 1)
    last = b.texts[-1]
    names = [r[0].text for r in last[2]["reply_markup"].inline_keyboard]
    check("two recent people offered, old one not", names[:2] == ["Mom", "Dad"] and "Old" not in names, names)
    b.texts.clear(); await bot.change_family_cmd(Upd(), Ctx(["Old"]))
    check("person past 10 min refused", "No one joined" in b.texts[-1][1], b.texts[-1][1])
    b.texts.clear(); await bot.change_family_cmd(Upd(), Ctx(["mom"]))
    kb = b.texts[-1][2]["reply_markup"].inline_keyboard
    check("one match goes straight to family list (not the same family)", [r[0].text for r in kb] == ["Lee", "✖ Cancel"], kb)
    cbs = [r[0].callback_data for r in kb]
    class Q:
        def __init__(s, data, uid=1): s.data = data; s.from_user = type("F", (), {"id": uid})(); s.message = M(); s.edits = []; s.alerts = []
        async def answer(s, t=None, show_alert=False): s.alerts.append(t)
        async def edit_message_text(s, t, **k): s.edits.append(t)
    class U2:
        def __init__(s, q): s.callback_query = q
    q = Q(cbs[0], uid=99); await bot.on_change_family_button(U2(q), Ctx())
    check("non-admin can't move", d.users.d[0]["thread_id"] == 5, q.alerts)
    q = Q(cbs[0]); await bot.on_change_family_button(U2(q), Ctx())
    check("moved to the new family", d.users.d[0]["thread_id"] == 6 and "moved" in q.edits[-1], q.edits)
    check("hearts of the old family removed", not d.favs.d)
    check("person told", any(t[0] == 20 and "Lee" in t[1] for t in b.texts), b.texts[-1])
    q = Q("cf:t:21:6"); await bot.on_change_family_button(U2(q), Ctx())
    check("old link can't be moved by button either", d.users.d[1]["thread_id"] == 5 and "first 10 minutes" in q.edits[-1], q.edits)
    q = Q("cf:x"); await bot.on_change_family_button(U2(q), Ctx()); check("cancel", "Cancelled" in q.edits[-1])
async def priv():
    d = fake.DB(); bot.db = d; b = FB(); bot.OWNER_ID = 1
    now = datetime.now(timezone.utc)
    d.families.d += [{"_id": "a", "thread_id": 5, "name": "Smith"}]
    d.users.d += [{"_id": 20, "thread_id": 5, "first_name": "Mom", "linked_at": now - timedelta(minutes=3)},
                  {"_id": 21, "thread_id": 5, "first_name": "Old", "linked_at": now - timedelta(minutes=30)}]
    d.favs.d += [{"_id": "20:1", "uid": 20}]
    st = {"del": 0}
    class M:
        chat_id = 0
        async def delete(s): st["del"] += 1
    class U:
        def __init__(s, i): s.id = i
    class Upd:
        def __init__(s, i): s.effective_message = M(); s.effective_message.chat_id = i; s.effective_user = U(i)
    class Ctx:
        bot = b
        def __init__(s): s.user_data = {}; s.args = []
    c = Ctx(); await bot.change_family_private(Upd(20), c)
    check("own command deleted + unlinked", st["del"] == 1 and not any(u["_id"] == 20 for u in d.users.d) and not d.favs.d)
    check("starts the name questions again", c.user_data.get("link") == {"stage": "first"} and "first name" in b.texts[-1][1], b.texts[-1])
    check("owner told", any(t[0] == 1 and "Mom" in t[1] for t in b.texts))
    c = Ctx(); await bot.change_family_private(Upd(21), c)
    check("after 10 minutes: refused, stays linked", any(u["_id"] == 21 for u in d.users.d) and "first 10 minutes" in b.texts[-1][1] and "link" not in c.user_data, b.texts[-1])
    c = Ctx(); await bot.change_family_private(Upd(77), c)
    check("not linked: told to /start", "/start" in b.texts[-1][1])
asyncio.run(main()); asyncio.run(priv())
bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed"); sys.exit(1 if bad else 0)
