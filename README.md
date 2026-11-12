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

### New families setting themselves up

If you give someone the bot and they don't have a code (for example at an event), they send `/start`, type their
first name and last name, and tap **🆕 I'm new, I don't have a code**. The bot asks "Is the last name spelled
right?" (**Yes** / **No, retype**). On Yes, the bot posts the request in one topic in the group called
**➕ Add families**. The topic is made the first time and reused after that (if you delete it, it's made again).
The post shows their name, last name and Telegram ID, with three choices:

- **🆕 Make a new [Last name] family and add [name]**: makes the family's topic and code and adds them. They're
  welcomed with their photos link.
- **➕ Add [name] to the existing [Last name] family**: shown when a family with that name already exists (it also
  warns you), in case it's a relative.
- **❌ Reject**: they're told politely.

Only you can answer. Each person can have one request waiting (an unanswered one expires after a day so they can ask again), at most 30 can wait at once, and each person can send only a few per hour. If something fails halfway, the request stays and tapping the button again retries without making a second family. The bot gives the same
replies whether or not a last name exists, so nobody can find out which families exist. `/register` refuses the
Add families topic.

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
   Upload **bot.py, webapp.py and app.html** together (the photo app needs all three).
3. Add the variables from `.env.example` (`BOT_TOKEN`, `MONGODB_URI`, `GROUP_ID`, `OWNER_ID`, `ADMIN_PASSWORD`, ...).
4. In UptimeRobot add an **HTTP(s)** monitor for your Render URL at the shortest interval
   (5 minutes on free). Free instances sleep after about 15 minutes without traffic, and the
   bot answers `200 ok` on the port Render provides.

No disk is needed. Check Render's current free-tier limits before relying on it.

## 11. The photo app (Telegram Mini App)
A nicer way to look at photos, inside Telegram. Clients open it with the **📱 Open the photo app**
button in the bot's menu, or the blue **📷 Photos** button next to the message box.

**What it has**
- **Welcome screen**, decided per person:
  - 2 or more photos added in the last 14 days, and the person hasn't seen the newest yet → **✨ New photos**.
  - After they have seen those once, and you have picked Best shots → **⭐ Best shots**.
  - Older than 14 days → Best shots. No Best shots yet (and they've already seen the new photos) → the latest photos.
  - Each family member has their own record, so one person opening the app doesn't change it for the others.
  - Photos approved from the Suggestions box don't count as "new".
- Tabs at the bottom: **Home, Dates, Places, All, Best** (only when you have picked some), **Suggested** (the Suggestions box).
- Tap a photo for full screen: swipe for the next one, pinch or double-tap to zoom.
  **📥 Send me the photo in chat** sends the full-quality file to the person's chat. The copyright notice shows in the viewer.
- Bigger/smaller text buttons and a Help page.
- Each person only ever sees **their own family's** photos. Everything is checked on the server using
  Telegram's signed login data, and the photo links are signed and expire.
- The owner account sees a "this app is for families" message. An admin tab for you comes later.

**How it works.** The web server runs inside the bot (same Render service, same `$PORT`), so there is
nothing extra to host. Photos stay on Telegram: the app asks the bot to fetch each picture and passes it on,
so the bot token never reaches the phone. When a photo is added, the bot also saves the id of Telegram's own
small preview, which makes the grids fast (and works for files over 20 MB). Older photos don't have that, so
their first view is a bit slower while a small copy is made.

**Setup.** Nothing to do in BotFather by default: the bot sets the Open button itself when it starts.
Want to control that button yourself? Set `AUTO_MENU_BUTTON=no` in Render. The bot then leaves it alone; in
BotFather choose your bot → Bot Settings → Menu Button → Configure menu button, send `https://your-address/app`
and a title (for example "Photos"). The 📱 button inside the bot's menu works either way. It needs the
service's public https address. On Render that is `RENDER_EXTERNAL_URL`, which Render fills in; elsewhere set
`WEBAPP_URL=https://your-address`. If neither is set the app is simply off.

**The owner's Admin tab.** When *you* open the app you get a 🛠 Admin tab (nobody else does; every request is
checked against your Telegram login). Nothing about you is ever saved.
- **Stats and Families:** totals, and every family with its code, people and photo counts. Tap a family to see its
  members, to **browse its photos exactly as it sees them** (a banner reminds you which family you're viewing), or
  to add photos.
- **Photo tools** (inside the full-screen viewer, only for you): ⭐ add or remove from Best shots, ✏️ change the place
  or date, 🗑 remove (asks first, deletes from the group and the list).
- **📤 Add photos:** pick the family, the place and the date (you must choose a date or tick "I don't know the date":
  the app never guesses), choose photos, and they upload one at a time. They are posted into the family's topic **as
  Files, so the full quality is kept**, and added to the list like any chat upload. Up to 30 MB each (bots can send
  at most 50 MB, and the free Render plan has little memory); bigger files still go through the chat. A repeated tap
  can't post a photo twice. Note: as with the chat upload, files keep their original data (including GPS, if the
  photo has it); Telegram only strips that from normal photos, not from files.
- **💡 Suggestions inbox:** each suggestion with its photos, and Approve / Reject (same logic as the buttons in the
  group, and the group's message is updated too).
- **📣 Announcements:** pick who, write it, review, then **send now** (it goes out in the background and the bot
  messages you when it's done) or **schedule** it (in the bot's time zone, shown on screen). Scheduled ones can be
  cancelled.
- **🕵️ Login attempts:** who tried and failed, with a family picker and an Add button, plus clear-lockout and dismiss.
- Your chat `/connect` pick is not used by the app; you choose the family inside the Admin tab.

**Limits**
- On the free Render plan the first open after a sleep takes a few seconds.
- A web page can't stop saving or screenshots; the copyright line is shown, nothing more.
- HEIC photos can't be shown in the grid by the server; the Telegram preview is used when it exists.
- Bot downloads are limited to 20 MB, so for bigger files the app shows Telegram's smaller preview.
  "Send me the photo in chat" still delivers the full original.

## Limits to know about
- The bot can't see files posted **before** it joined the group, or topics it hasn't been told about.
- Deleting a file by hand in the group doesn't update the list; use `/remove` instead.
- Telegram only lets a bot delete messages older than 48 hours if the bot is an admin.
- `PROTECT_CONTENT=true` blocks forwarding and saving from the chat, but not screenshots.

## Admin tab (owner only)

Open the Mini App as the owner and you get an **Admin** tab: families list, browse/remove/hide any family's photos, suggestions inbox, stats, announcements (now or scheduled), and login attempts.

### 📤 Add photos from the app
Admin → **Add photos** (also on each family's page). Pick the family, a place (optional), and a date (or tick "I don't know the date"), then choose photos. They upload one at a time as files, so full quality is kept (max 30 MB each; bigger ones go through the chat). They are posted into the family's topic and indexed. Failed ones can be retried without creating duplicates.

### Family tools in the app
Admin → **Add a family** (makes the topic and code), and on each family's page: members with **Unlink**, **Add a person by Telegram ID**, **Make a new code**, **Clear best shots**, and **Delete this family** (keep the topic and files, or delete the topic with all its files). These do the same as the chat's /admin panel.

### Chatting with clients
Clients tap **💬 Message or comment to Dima** (chat menu, **💬 Reply to Dima** under every announcement or message, or **💬 Chat with Dima** on the app's Home screen). Each message is saved and posted in a **💬 Messages** topic in the group (made automatically the first time). You can answer in three ways:
- **In the group:** reply to their message in the Messages topic. Your answer goes straight to them.
- **In the app:** Admin → **Messages** shows every conversation with an unread count. Open one, read it, and write back.
- **To anyone, any time:** **✉️ Write to someone** in the Messages list, or **✉️ Message** on a member's card.

For 3 days after you write to someone, whatever they type in the bot chat comes to you, so they can just answer you. Clients can send 10 messages an hour (up to 1000 characters each). Conversations older than 180 days are cleared automatically.

### Feedback and deleting
- **Feedback:** clients tap **📝 Send feedback** (chat menu, or the app's Home screen). It is posted in a **⭐ Feedback** topic in the group (made automatically) and listed in the app's Admin tab → **Feedback**, and in `/admin` → **Feedback** in the bot. Reply to it in the topic, or tap **Reply** to start a chat with that person. Clients can send 5 an hour, up to 1500 characters.
- **Deleting (clients):** their own chat messages, their whole chat (**🗑 Delete this chat**, in the app or the bot), and their own feedback (**🗂 My feedback** in the bot, **🗑 Delete** in the app). They can't delete your messages one by one, but clearing the chat removes everything.
- **Deleting (you):** single messages (🗑 on a bubble in the app, or a button in the bot), a whole chat, or a piece of feedback. Telegram copies in the group and in the client's chat are removed too, where Telegram allows it (within 48 hours).
- **Chats inside the bot:** `/admin` → **💬 Messages** lists conversations (with unread counts), opens one, lets you **✍️ Reply**, delete a message, or delete the chat.
- Unlinking a person or deleting a family also deletes their chat and feedback.

## Any language (translation)

The first time someone opens the bot (`/start`) or the app, they are asked for their language: a **Recommended** list (Русский, Українська, English) and a **search** for about 75 others (type the English name or the name in its own letters, for example `hebrew` or `español`). They can change it any time with **🌐 Language** (bot menu, or the app's Home tab). People who picked a language before are asked once on their next visit. After that:

- The bot's menus, buttons and the app are shown in their language (the Admin tab stays English). Russian uses a hand-written translation; every other language is translated with Groq the first time someone picks it, then saved, so it is instant for the next people. Right-to-left languages (Arabic, Hebrew, Persian, Urdu, Yiddish) flip the layout.
- Their messages and feedback reach you in English (the original is shown below it).
- Your messages and announcements reach them in their language, with the original underneath.

**Setup (free):** make an account at console.groq.com, create an API key, and add it on Render as the environment
variable `GROQ_API_KEY`. Optional: `GROQ_MODEL` (default `llama-3.3-70b-versatile`; change it if Groq retires that name).
Without a key everything still works; messages just aren't translated.

**Files:** upload the new `i18n.py` (lowercase) next to `bot.py`, together with the updated `bot.py`, `webapp.py`, `app.html`.

## Users list

**/admin → 👥 Users** (and the app's Admin tab → Users) lists everyone who started the bot, newest first, with where they stand:
in a family, added by a relative but not confirmed yet, asked for a new family, tried to log in with wrong details, or just started the bot.
People who pressed Start before this update appear once they are in a family or try to log in; everyone else shows up the next time they press Start.

## Groups and choosing who gets an announcement

**Admin → 🏷 Groups** (app) or **/admin → 🏷 Groups** (bot): make named groups of families (for example "Camp"). A family can be in several groups.
Tick the families inside a group, or open a family's page and tick its groups. Deleting a family removes it from every group.

**Announcements** (and "Write to someone") can go to: everyone, a group, several families you tick, one family, or one person.
The preview shows who will get it before you send. For a scheduled announcement to a group, the group's families are looked up when it is sent, so families you add later are included.

**Filters:** the Families list, 👥 Users and 💬 Messages have group buttons (All / each group) in both the bot and the app.

Groups need no new setup. Limits: 30 groups, group names up to 30 characters, up to 100 hand-picked families per announcement.

## Videos

You can add videos the same way as photos, from the bot chat or by posting in a family's topic in the group:

- **Bot chat:** /admin → Add photos → pick the family → place and date → send photos **and videos** (a video sent as a File is kept as a file).
- **Group topic:** post the video with a caption like `2026-07-04 | Central Park`, same as a photo. Replying `/best` or `/remove` to a video works too.

Videos can be any size (the bot never downloads them, it passes them on by Telegram's file id). Clients see them in the bot with the photos, and in the app as a thumbnail with a ▶ badge and the length.
In the app, tapping ▶ (or the button) sends the full video to the client's chat. The app doesn't play videos itself. A video can't be added from the app's upload screen (photos only), but clients **can** suggest videos: in ➕ Suggest photos they send photos and videos together (a video sent as a File stays a File). In the group's suggestions topic and in the app's Suggested inbox you see each video's thumbnail and length, with the usual Approve / Reject. When you approve, the videos join the family's gallery (and the other families you approve for) with “Added by …”, exactly like suggested photos.
GIFs are ignored.

## Photos and videos for a whole group

For a photo or video of everyone in a group (for example the whole camp), add it **once** to the group instead of to every family:

- **Bot chat:** /admin → **📤 Add photos** → tap the **👥 group** button above the families (or open the group under **Groups** → **📤 Add photos/videos for the whole group**) → place → date → send photos **and videos**. For the place you can type `group` to use “Camp · whole group”.
- **App:** Admin tab → **Add photos** → choose **👥 Camp (the whole group)** in the picker (photos only; videos go through the bot chat).

There is only **one shared copy**. Every family in the group sees it, including families you add to the group later, and a family you take out of the group stops seeing it right away. Families outside the group never see it. It shows up with the family's own photos in the bot and the app (dates, places, All, Best shots, New photos), labelled **👥 Camp**. The copy is kept in the Telegram group's **General** topic (the bot never indexes that topic from what you post there by hand).

**Leaving families out:** when you start a group upload you get a checklist of the group's families (all ticked). Untick any family that should not see these files, then tap **➡️ Continue** (the app has the same list under the picker). Left-out families never see the files, even though they are in the group. To change it for one file later, open the photo in the app as the owner → ✏️ Edit → **Who sees it**.

When you finish, the bot offers to tell everyone in the group (not the left-out families). To remove group files: open the group in the app and delete a photo as usual, or in the bot use Groups → the group → **🗑 Delete the group's shared files**. Deleting a group also deletes its shared files.

## Upload line and Photographers line

Everything people suggest (➕ Suggest photos) goes to you, not to the family's own Suggestions topic:

- **📤 Upload line** is one shared topic in the group for everybody's suggestions.
- **📤 Photographers line** is a separate topic for your top photographers. You pick them in /admin → 📸 Photographers
  (or the Admin tab → Photographers, or Families → Members → "📸 Photographer: ON/off").

In either topic:

- `/go` (reply to the batch): shows the info and asks "Is all this right?". Yes publishes it to the family (and any other
  families listed). No asks the date, place and families one by one; each question has a Skip button.
- `/hold`: moves the batch to a 📋 Photos on review topic (reply `/go` there later).
- `/del` as a reply to a photo drops that photo. Replying to a photo with a new (edited) photo or file replaces it.
- Buttons under the batch do the same: ✅ Go, ⏸ Hold, ❌ Reject.

(The old way, one Suggestions topic per family with Approve / Reject buttons, is still in the code behind
`LEGACY_SUGGESTIONS`. Batches that were already waiting there can still be approved.)

## Favorites (♥) and "not opened yet"

- In the app's photo viewer, clients tap **♡ Favorite** to heart a photo. Once someone has a heart, a **♥ Favorites** tab appears
  with their own list (nobody else sees it). On a family's page in /admin (and the app's Admin tab) you see the family's total
  number of hearts. Your own preview never saves hearts.
- The app records when each person last opened it. A family's page shows "Last opened the app: 2 days ago · saw the newest
  photos: yes/NO", and each member's last open.
- After you send a "new photos" message to a family, if nobody in the family has opened the app 3 days later, each confirmed person
  gets **one** gentle reminder with an Open button (never more than one per "new photos" message).

## Not opened yet
- When someone opens a photo full-screen, the app records it (`db.seen`). Owner views are never recorded.
- Admin → "👀 Not opened yet" (bot /admin and the app) lists families where nobody opened the app since their newest photos arrived, with how many new photos each person saw, and a "🔔 Remind" button (the same gentle message with an Open button; one manual reminder per family per 6 hours).


## Letting someone in on their own phone
Set `HELPER_PIN` (4–8 digits, your own secret) in Render. At the login's code step a "🤝 I have <name> with me" button opens the app, asks for that code, then lists the families (names only); tap one and that phone is linked. Wrong codes lock out after a few tries.
