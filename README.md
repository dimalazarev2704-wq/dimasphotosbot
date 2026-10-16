# Telegram Family Photo Bot

You post photos in a private Telegram group, one topic
per family. Clients open the bot, enter their first name, last name and their family's
6-digit code, and can then browse **only their family's photos** by date or location.

The photos stay on Telegram. MongoDB stores the index (which file belongs to which family,
its date and location) and which Telegram account is linked to which family.

## 1. Create the bot
1. In Telegram, open **@BotFather**, send `/newbot`, and copy the token.
2. Optional: `/setprivacy` → your bot → **Disable**, so it can see group posts.

## 2. Set up the group
1. Create a group and turn on **Topics**.
2. Add the bot and make it an **admin** with the **Delete messages** right.
3. Send `/id` in the group to get its ID (looks like `-1001234567890`).

## 3. MongoDB
Create a free MongoDB Atlas cluster, add a database user, allow your host's IP
(or `0.0.0.0/0` while testing), and copy the connection string.

## 4. Run locally
```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # fill in BOT_TOKEN, MONGODB_URI, GROUP_ID, ADMIN_PASSWORD
python bot.py
```

## 5. Adding a family
1. Create a topic named after the family's last name.
2. Inside it send `/register Garcia`.
3. The bot replies **"Family made"** with a 6-digit code. Give the family their last name
   and that code. Two families can share a last name, because each has its own code.

## 6. Adding photos
Post them in the family's topic either way:
- **Normal photos** (the gallery way): easiest, but Telegram shrinks them, so clients get the shrunken version.
- **As Files** (attach → File): Telegram never compresses these, so clients get the full original quality.

The bot accepts both. In "All photos" it sends photos and files as separate albums.

Optional caption for the date and place:
```
Central Park
2026-07-04 | Central Park
2026-07-04
```
- No date → the day it was posted. No location → grouped under "No location".
- For albums, one caption on any file applies to the whole album.
- Telegram strips GPS data, so locations come from the caption.

**From the bot (without being in the group):** pick the family, then the bot **asks you for the
place and the date** (both required, no skipping). Type the place, then a date like `2026-07-04`
(or `today` / `yesterday`). If you don't know the date, type `unknown`: those photos show as **No date** at the bottom of the client's date list, and still appear under their place and in All photos. After that, send the photos or files: every one in that batch gets that
place and date. A photo with its own caption keeps its own caption instead. For a different place or
date, send `/done` and start a new batch.

- **You (owner):** `/admin` → **📤 Add photos** → pick the family → place → date → send photos → **Done**.
  No password.
- **Anyone else:** `/addphotos`, the password (asked **every time**, your message with it is deleted),
  then `LastName 123456`, then place, date, photos, `/done`. The bot does not save anything that
  marks your account as an admin.

## 7. What clients do
1. Open the bot and send `/start`.
2. Enter first name, last name, then the 6-digit code.
3. Use the buttons: **By date**, **By location** or **All photos**. `/gallery` (or `/menu`) shows them again.
4. After the photos arrive, tap **🧹 Hide these photos** to remove them from the chat and keep it clean.
   They stay in the gallery, so the client can open them again any time.

### Suggesting photos
Clients can add their own photos to the gallery, and you approve each one first:
1. Client taps **➕ Suggest photos**, types the place and date (`unknown` is allowed for either),
   names any other families in the photos by last name (or `no`), sends the photos and taps
   **Send for approval**.
2. The bot makes a **💡 [Family] suggestions** topic in the group (the first time) and posts the photos
   there with the details and buttons. You also get a private heads-up.
3. Only you can tap the buttons: **Approve for all families listed**, **Approve for their own family only**,
   or **Reject**. Approving copies the photos into the family topic(s), saves place, date and the
   client's first name, deletes them from the suggestions topic and tells the client. Rejecting deletes
   them and tells the client kindly.
4. Approved photos show in the normal date/location galleries and in a **💡 Family suggestions** gallery,
   each captioned with place, date and "Added by [name]".

If two families share the last name a client types, the bot doesn't guess; it passes the name on to you.
The bot needs the **Manage topics** and **Delete messages** rights for this.

Clients can't unlink themselves. Wrong guesses are limited: 5 wrong tries lock that
Telegram account for an hour (saved in MongoDB, so a restart doesn't reset it).

### Family members adding relatives

Clients get **👪 Add a family member** in their menu. Nobody types a Telegram ID:

1. **Choose a person**: they tap "👤 Choose a person" and pick the relative from their Telegram contacts (or send
   the contact card). Typing a name or `@username` doesn't work (Telegram doesn't let bots look people up), so the
   bot asks them to use the picker.
2. They type the relative's **first name**, then their **last name**.
3. The bot shows a **summary** (the person's photo, @username, Telegram name, the first and last name typed, and
   their Telegram ID) and asks "Is this right?". Nothing is added until they tap **✅ Yes**. **✏️ Start over** and
   Cancel are there too.

- The relative is added to **the same family only**, but sees nothing yet. When they open the bot they must type
  their first and last name exactly as the family member wrote it (capitals and spacing don't matter). Wrong
  tries are limited like logins. Until they confirm they get no photos, announcements or "photos ready" messages.
- You get a message with the name and **added by [family member + family name]**, and a **Remove** button. In
  Family → members, each person added this way shows "added by …" and "hasn't confirmed their name yet" if they
  haven't.
- The limit is 12 people per family (`FAMILY_MAX_MEMBERS` to change it). Someone already in another family (or the photographer) can't
  be taken, and the client is never told which family. Clients still can't remove anyone; only you can.

### Help, pinned buttons, Best shots, "photos are ready"

- Families get two permanent buttons at the bottom of the chat: **📷 My photos** and **❓ Help** (also `/help`). Any
  random message gets a friendly reply pointing to them.
- **⭐ Best shots**: in the group, reply to a photo with `/best` (again to remove), or put a ⭐ in the caption when
  uploading. Once any exist, families see a **⭐ Best shots** gallery. On a family's admin page you can
  **Clear best shots**.
- After you tap **Done** (or `/done`) on an upload, the bot asks "Tell the family their photos are ready?" and says how
  many people will get it. Yes sends them a message with an Open button to those photos; No sends nothing.

## 8. The owner's admin panel (`/admin`)
Set `OWNER_ID` to your own Telegram ID (send `/id` to the bot in a **private chat**, the number
it replies with is your ID). Then send `/admin` to the bot. Only that account sees the panel,
and the bot saves nothing about you in the database.

| Button | What it does |
|---|---|
| 📋 Families | all families; tap one for its page (code, photo count, people linked) |
| ➕ Add family | you type the last name, the bot **creates the topic in the group** and gives you the code |
| 📤 Add photos | pick a family, type the place and date, then send photos. No password for you |
| 🗑 Remove photos | pick a family, then a date; asks "Are you sure?", then deletes those photos from the group and the list |
| 🕵️ Login attempts | everyone who recently opened the bot or got their details wrong: name, family they typed, Telegram ID, why it failed and whether they're locked out. One tap adds them to the family, **Clear the lockout** lets them retry, **Dismiss** removes them. You also get a private message when someone is locked out |
| ➕ Add person | from the main panel: type a Telegram ID and first name, then tap the family. They get the "already added you" message |
| (family page) ➕ Add person by ID | type a Telegram ID (and first name) to link someone yourself. They can get their ID by sending `/id` to the bot. When they open the bot they see "[OWNER_NAME] has already added you to the … family" (they get it right away if they've opened the bot before) |
| 📣 Announce | pick everyone or one family, type the message, check the preview, then send. It goes to each linked client as a private message from the bot (`/announce` does the same) |
| 🕒 Scheduled | on the preview tap **🕒 Schedule it** and type when, like `tomorrow 9am` or `2026-10-20 18:30` (your time zone). Open **🕒 Scheduled** in the panel to see or cancel them. Saved in the database, so a restart doesn't lose them; the bot checks every 30 seconds and tells you when one went out. If the bot is asleep at that time (free hosting), it goes out as soon as it wakes, so keep UptimeRobot running |
| 📊 Stats | families, people linked and photos |
| (family page) 👥 Members | see who linked and unlink people |
| (family page) 🔄 New code | makes a new code; people already linked stay linked |
| (family page) ❌ Delete family | asks first; choose to keep the topic and files in the group, or delete the topic and all its files too |

Creating or deleting topics needs the bot to be a group admin with the **Manage topics** right
(and **Delete messages** for removing photos or topics).

**Browsing as a family (owner only):** send `/connect` in the private chat, pick a family, and `/gallery`
shows exactly what its clients see (or tap **👁 Browse as this family** on the family's page). `/unlink`
in the private chat disconnects you. This is kept in memory only, so nothing is saved about you and a
restart disconnects you. Clients can't use `/connect` or `/unlink`.

`/addphotos` in a private chat also skips the password for the owner. Everyone else still needs
`ADMIN_PASSWORD` every time.

## 9. Group admin commands
All of these only work for admins of the group (and for the owner).

| Command | Where | What it does |
|---|---|---|
| `/register LastName` | in a topic | makes the family, replies with its code |
| `/code` | in a topic | shows the family's code again |
| `/members` | in a topic | lists people linked to that family |
| `/unlink FirstName` | in a topic | unlinks a person (use `/unlink <id>` if two share a first name) |
| `/remove` | reply to a photo | asks "Are you sure?", then deletes that exact photo from the group and the list |
| `/families` | anywhere in the group | all families with codes and file counts |

## 10. Free hosting: Render + UptimeRobot
1. Push this project to a private GitHub repo (never commit `.env`).
2. In Render: **New → Web Service**, connect the repo, choose the **Free** instance.
   Build: `pip install -r requirements.txt`. Start: `python bot.py`.
3. Add the variables from `.env.example` (`BOT_TOKEN`, `MONGODB_URI`, `GROUP_ID`, `OWNER_ID`, `ADMIN_PASSWORD`, ...).
4. In UptimeRobot add an **HTTP(s)** monitor for your Render URL at the shortest interval
   (5 minutes on free). Free instances sleep after about 15 minutes without traffic, and the
   bot answers `200 ok` on the port Render provides.

No disk is needed. Check Render's current free-tier limits before relying on it.

## Limits to know about
- The bot can't see files posted **before** it joined the group, or topics it hasn't been told about.
- Deleting a file by hand in the group doesn't update the list; use `/remove` instead.
- Telegram only lets a bot delete messages older than 48 hours if the bot is an admin.
- `PROTECT_CONTENT=true` blocks forwarding and saving from the chat, but not screenshots.
