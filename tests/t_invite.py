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
    username = "testbot"
    def __init__(s): s.texts = []
    async def send_message(s, chat, text, **k): s.texts.append((chat, text, k))
async def noop(*a, **k): return None
bot.ensure_keyboard = noop
async def main():
    d = fake.DB(); bot.db = d; b = FB(); bot.OWNER_ID = 1
    d.families.d += [{"_id": "a", "thread_id": 5, "name": "Smith"}, {"_id": "b", "thread_id": 6, "name": "Lee"}]
    out = []
    class M:
        chat_id = -100; message_thread_id = 5; is_topic_message = True; sender_chat = None
        async def reply_text(s, t, **k): out.append((t, k))
    class U:
        def __init__(s, i, n="Mia"): s.id = i; s.first_name = n; s.username = None
    class Upd:
        def __init__(s, i=1, n="Mia"): s.effective_message = M(); s.effective_user = U(i, n)
    class Ctx:
        bot = b
        def __init__(s, args=()): s.args = list(args); s.user_data = {}
    await bot.invite_cmd(Upd(1), Ctx(["Mom"]))
    text, k = out[-1]
    tok = d.invites.d[0]["_id"]
    check("link made for the right family", f"https://t.me/testbot?start=inv_{tok}" in text and d.invites.d[0]["tid"] == 5 and d.invites.d[0]["name"] == "Mom", text)
    # someone taps it
    out.clear(); await bot.claim_invite(Upd(30, "Maria"), Ctx(), tok)
    u = [x for x in d.users.d if x["_id"] == 30]
    check("linked to that family with the typed name", u and u[0]["thread_id"] == 5 and u[0]["first_name"] == "Mom", d.users.d)
    check("welcome shown", "You're in, Mom" in out[-1][0] and "Smith" in out[-1][0])
    check("owner told", any(t[0] == 1 and "Mom" in t[1] for t in b.texts))
    # second person with the same link
    out.clear(); await bot.claim_invite(Upd(31, "Eve"), Ctx(), tok)
    check("works only once", not any(x["_id"] == 31 for x in d.users.d) and "isn't working" in out[-1][0], out)
    # same person taps again: just opens
    out.clear(); await bot.claim_invite(Upd(30), Ctx(), tok)
    check("same person again just opens the photos", "already in" in out[-1][0])
    # expired
    d.invites.d.append({"_id": "oldtoken123456", "tid": 5, "name": "", "exp": datetime.now(timezone.utc) - timedelta(minutes=1), "used": None})
    out.clear(); await bot.claim_invite(Upd(32), Ctx(), "oldtoken123456")
    check("expired link refused", not any(x["_id"] == 32 for x in d.users.d) and "isn't working" in out[-1][0])
    # nonsense token
    out.clear(); await bot.claim_invite(Upd(33), Ctx(), "x' $ne")
    check("bad token refused", not any(x["_id"] == 33 for x in d.users.d) and out)
    # already in another family: not consumed
    await bot.invite_cmd(Upd(1), Ctx([])); tok2 = d.invites.d[-1]["_id"]
    out.clear(); await bot.claim_invite(Upd(30), Ctx(), tok2) # 30 is already in 5 -> same family path
    d.users.d.append({"_id": 40, "thread_id": 6, "first_name": "Lee1"})
    out.clear(); await bot.claim_invite(Upd(40), Ctx(), tok2)
    check("someone in another family isn't moved, link kept", [x for x in d.users.d if x["_id"] == 40][0]["thread_id"] == 6 and not [i for i in d.invites.d if i["_id"] == tok2][0]["used"] and "another family" in out[-1][0], out)
    out.clear(); await bot.claim_invite(Upd(41, "Zed"), Ctx(), tok2)
    check("no name given: Telegram first name used", [x for x in d.users.d if x["_id"] == 41][0]["first_name"] == "Zed")
    # cancel
    await bot.invite_cmd(Upd(1), Ctx(["Bob"])); tok3 = d.invites.d[-1]["_id"]
    class Q:
        def __init__(s, data, uid=1): s.data = data; s.from_user = type("F", (), {"id": uid})(); s.message = M(); s.edits = []
        async def answer(s, *a, **k): pass
        async def edit_message_text(s, t, **k): s.edits.append(t)
    class U2:
        def __init__(s, q): s.callback_query = q
    q = Q("iv:x:" + tok3); await bot.on_invite_button(U2(q), Ctx())
    check("cancel removes the link", not [i for i in d.invites.d if i["_id"] == tok3] and "cancelled" in q.edits[-1])
    out.clear(); await bot.claim_invite(Upd(50), Ctx(), tok3)
    check("cancelled link doesn't work", not any(x["_id"] == 50 for x in d.users.d))
asyncio.run(main())
bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed"); sys.exit(1 if bad else 0)
