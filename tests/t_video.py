# Run from the repo folder: python3 -I tests/t_video.py  (fake Telegram + fake database)
import sys, os, asyncio, re, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fake
os.environ.update(BOT_TOKEN="1:x", OWNER_ID="1", GROUP_ID="-100", MONGODB_URI="x")
import bot, webapp
results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name + (" " + str(extra)[:200] if extra and not cond else ""))
class FA: bot = None; bot_data = {}
async def main():
    d = fake.DB(); bot.db = d
    d.users.d += [{"_id": 20, "thread_id": 5, "first_name": "Mom"}, {"_id": 21, "thread_id": 6, "first_name": "Other"}]
    d.families.d += [{"_id": "f5", "thread_id": 5, "name": "Smith"}, {"_id": "f6", "thread_id": 6, "name": "Lee"}]
    d.photos.d += [{"message_id": 301, "thread_id": 5, "file_id": "V1", "kind": "video", "vsize": 1000, "dur": 5, "date": "2026-06-03"},
                   {"message_id": 302, "thread_id": 5, "file_id": "V2", "kind": "video", "vsize": 30 * 1048576, "dur": 9, "date": "2026-06-03"},
                   {"message_id": 303, "thread_id": 5, "file_id": "P1", "kind": "photo", "date": "2026-06-03"}]
    w = webapp.WebApp(bot.WebDeps(FA()))
    data = bytes(range(256)) * 4
    async def fake_dl(fid): return data if fid == "V1" else None
    w.download = fake_dl
    c = w.card(d.photos.d[0], 20); check("small video card has a play link", c.get("v", "").startswith("/vid/301?u=20&e="), c)
    c2 = w.card(d.photos.d[1], 20); check("30 MB video has no play link", "v" not in c2, c2)
    check("photo has no play link", "v" not in w.card(d.photos.d[2], 20))
    url = c["v"]
    r = await w.handle("GET", url, {}, b""); check("whole video", r.status == 200 and r.body == data and r.headers["Accept-Ranges"] == "bytes", r.status)
    r = await w.handle("GET", url, {"range": "bytes=0-99"}, b""); check("first 100 bytes", r.status == 206 and r.body == data[:100] and r.headers["Content-Range"] == "bytes 0-99/1024", (r.status, r.headers))
    r = await w.handle("GET", url, {"range": "bytes=1000-"}, b""); check("open-ended range", r.status == 206 and r.body == data[1000:], r.status)
    r = await w.handle("GET", url, {"range": "bytes=-24"}, b""); check("last 24 bytes", r.status == 206 and r.body == data[-24:], r.status)
    r = await w.handle("GET", url, {"range": "bytes=5000-6000"}, b""); check("range past the end refused", r.status == 416, r.status)
    r = await w.handle("GET", url, {"range": "garbage"}, b""); check("junk range gives whole file", r.status == 200)
    bad = re.sub(r"sig=[0-9a-f]{32}", "sig=" + "0" * 32, url); r = await w.handle("GET", bad, {}, b""); check("tampered link refused", r.status == 403, r.status)
    other = url.replace("u=20", "u=21"); r = await w.handle("GET", other, {}, b""); check("another person's link refused", r.status == 403, r.status)
    # a person of another family with a correctly signed link still can't see it
    exp = int(re.search(r"e=(\d+)", url).group(1)); sig = w.sign(301, "vid", 21, exp)
    r = await w.handle("GET", f"/vid/301?u=21&e={exp}&sig={sig}", {}, b""); check("other family can't watch it", r.status == 404, r.status)
    # big video and photos refused even with a valid signature
    for mid in (302, 303):
        sig = w.sign(mid, "vid", 20, exp); r = await w.handle("GET", f"/vid/{mid}?u=20&e={exp}&sig={sig}", {}, b""); check(f"{mid} (big video / photo) refused", r.status == 404, r.status)
    sig = w.sign(301, "vid", 20, 1); r = await w.handle("GET", f"/vid/301?u=20&e=1&sig={sig}", {}, b""); check("expired link refused", r.status == 403, r.status)
    # the image link signature can't be reused for video
    sigimg = w.sign(301, "full", 20, exp); r = await w.handle("GET", f"/vid/301?u=20&e={exp}&sig={sigimg}", {}, b""); check("photo link can't be reused for video", r.status == 403, r.status)
asyncio.run(main())
bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed"); sys.exit(1 if bad else 0)
