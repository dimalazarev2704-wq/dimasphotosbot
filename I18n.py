"""Russian texts for client-facing bot messages and buttons.

`ru(text)` returns the Russian version when it is in the table (exact match, or one of the patterns for texts that
contain a name or a number). Anything else comes back unchanged (bot.py may then ask Groq to translate it).
Admin-only texts are not here on purpose: the photographer reads English."""
import re

EXACT = {
    # buttons
    "📱 Open the photo app": "📱 Открыть приложение с фото",
    "📅 By date": "📅 По дате",
    "📍 By location": "📍 По месту",
    "🖼 All photos": "🖼 Все фото",
    "➕ Suggest photos": "➕ Предложить фото",
    "💡 Family suggestions": "💡 Предложения семьи",
    "👪 Add a family member": "👪 Добавить родственника",
    "📝 Send feedback": "📝 Отправить отзыв",
    "⭐ Best shots": "⭐ Лучшие кадры",
    "🏠 Menu": "🏠 Меню",
    "🧹 Hide these photos": "🧹 Скрыть эти фото",
    "Cancel": "Отмена",
    "⬅️ Back": "⬅️ Назад",
    "🗑 Delete my chat": "🗑 Удалить мой чат",
    "Yes, delete the chat": "Да, удалить чат",
    "🗂 My feedback": "🗂 Мои отзывы",
    "✅ Yes, tell them": "✅ Да, сообщить им",
    "No thanks": "Нет, спасибо",
    "🏠 Open the gallery": "🏠 Открыть галерею",
    "✅ Send for approval": "✅ Отправить на одобрение",
    "👤 Choose a person": "👤 Выбрать человека",
    "✏️ Start over": "✏️ Начать заново",
    "✅ Yes, that's right": "✅ Да, верно",
    "✏️ No, let me retype it": "✏️ Нет, введу заново",
    "🆕 I'm new, I don't have a code": "🆕 Я новый, у меня нет кода",
    "📷 My photos": "📷 Мои фото",
    "❓ Help": "❓ Помощь",
    "Show more photos": "Показать ещё фото",
    # messages
    "Hi! 👋 Let's get you to your photos. What's your first name?": "Привет! 👋 Давайте найдём ваши фото. Как вас зовут?",
    "Welcome back! What would you like to see?": "С возвращением! Что хотите посмотреть?",
    "What would you like to see?": "Что хотите посмотреть?",
    "Send /start to link your account first.": "Сначала отправьте /start, чтобы подключить аккаунт.",
    "Send /start to link your account.": "Отправьте /start, чтобы подключить аккаунт.",
    "Please send just your first name.": "Пожалуйста, отправьте только своё имя.",
    "Please send your family's last name.": "Пожалуйста, отправьте фамилию вашей семьи.",
    "Please tap ✅ Yes or ✏️ No above.": "Пожалуйста, нажмите ✅ Да или ✏️ Нет выше.",
    "You're already in a family. Send /gallery to see the photos.": "Вы уже в семье. Отправьте /gallery, чтобы увидеть фото.",
    "Hi! 👋 I can show you your family's photos. Tap what you'd like to see (or tap ❓ Help):":
        "Привет! 👋 Я покажу фото вашей семьи. Нажмите, что хотите посмотреть (или ❓ Помощь):",
    "No photos here yet.": "Здесь пока нет фото.",
    "No photos yet. Check back soon!": "Фото пока нет. Загляните позже!",
    "No best shots have been picked for your family yet. Check back soon!": "Для вашей семьи пока не выбраны лучшие кадры. Загляните позже!",
    "I'm still sending your photos. One moment! ⏳": "Я ещё отправляю ваши фото. Минутку! ⏳",
    "Something went wrong while sending. Please try again.": "При отправке что-то пошло не так. Попробуйте ещё раз.",
    "Pick a date:": "Выберите дату:",
    "Pick a location:": "Выберите место:",
    "You haven't sent any feedback yet.": "Вы пока не отправляли отзывов.",
    "That feedback timed out. Tap 📝 Send feedback to start again.": "Время вышло. Нажмите 📝 Отправить отзыв, чтобы начать заново.",
    "👇 Tap “Choose a person”": "👇 Нажмите «Выбрать человека»",
    "Cancelled.": "Отменено.",
    "Step 2: type their FIRST name.": "Шаг 2: введите их ИМЯ.",
    "Step 3: type their LAST name.": "Шаг 3: введите их ФАМИЛИЮ.",
    "Please type just their FIRST name (letters only).": "Пожалуйста, введите только имя (только буквы).",
    "Please type their LAST name (letters only).": "Пожалуйста, введите фамилию (только буквы).",
    "Please tap ✅ Yes or ✏️ Start over above.": "Пожалуйста, нажмите ✅ Да или ✏️ Начать заново выше.",
    "Send the photos, then tap Send for approval.": "Отправьте фото, затем нажмите «Отправить на одобрение».",
    "📥 Got it. Send more, or tap Send for approval.": "📥 Принято. Отправьте ещё или нажмите «Отправить на одобрение».",
    "I only take photos and image files. That one was skipped.": "Я принимаю только фото и файлы-изображения. Этот файл пропущен.",
    "📍 Where were they taken? Type the place, or type unknown if you don't know.":
        "📍 Где это снято? Напишите место или «unknown», если не знаете.",
    "👪 Are other families in these photos too? Type their last names separated by commas (for example: Novak, Smith), or type no.":
        "👪 На этих фото есть другие семьи? Напишите их фамилии через запятую (например: Novak, Smith) или напишите «no».",
    "Not yet. Please answer my question first, then send the photos again.": "Ещё рано. Сначала ответьте на мой вопрос, потом отправьте фото снова.",
    "That suggestion timed out. Tap ➕ Suggest photos to start again.": "Время вышло. Нажмите ➕ Предложить фото, чтобы начать заново.",
    "That timed out. Tap 👪 Add a family member to start again.": "Время вышло. Нажмите 👪 Добавить родственника, чтобы начать заново.",
    "I can only take photos when you tap ➕ Suggest photos first (it may have been reset). Open the menu with /gallery.":
        "Я принимаю фото только после нажатия ➕ Предложить фото (возможно, сессия сбросилась). Откройте меню: /gallery.",
    "Sorry, I can't set up new families yet. Please ask the photographer.": "Простите, я пока не могу создавать новые семьи. Обратитесь к фотографу.",
    "Sorry, I couldn't send your request right now. Please try again later.": "Простите, сейчас не удалось отправить запрос. Попробуйте позже.",
    "OK! What's your family's last name?": "Хорошо! Какая у вашей семьи фамилия?",
    "Send /start to begin.": "Отправьте /start, чтобы начать.",
    "👇 I put a “📷 My photos” button at the bottom of this chat, so you can always find your photos. “❓ Help” explains everything.":
        "👇 Внизу чата я добавил кнопку «📷 Мои фото», чтобы вы всегда могли найти свои фото. «❓ Помощь» всё объясняет.",
    "Only the photographer can do this.": "Это может сделать только фотограф.",
    "🕐 That was too long ago. Tap the button again.": "🕐 Слишком давно. Нажмите кнопку ещё раз.",
    "🗑 Your chat was deleted.": "🗑 Ваш чат удалён.",
    "There was no chat to delete.": "Нет чата для удаления.",
    "ℹ️ How this works": "ℹ️ Как это работает",
}

# (pattern, template using the groups): for texts that contain a name or a number
PATTERNS = [
    (r"Your request is waiting for (.+?)\. I'll message you here as soon as your family is set up\. 🙂",
     r"Ваш запрос ожидает \1. Я напишу вам здесь, как только ваша семья будет настроена. 🙂"),
    (r"Nice to meet you, (.+?)! What's your family's last name\?", r"Приятно познакомиться, \1! Какая фамилия у вашей семьи?"),
    (r"Thanks! Now the 6-digit code you were given\.\n\nNew here and don't have a code\?",
     "Спасибо! Теперь 6-значный код, который вам дали.\n\nВы здесь впервые и у вас нет кода?"),
    (r"You're in, (.+?)! 🎉 Here are the (.+?) family's photos\.", r"Вы в семье, \1! 🎉 Вот фото семьи \2."),
    (r"💬 Message or comment to (.+)", r"💬 Написать сообщение для \1"),
    (r"📝 Type your feedback about the photos or the app\. It goes to (.+?)\.", r"📝 Напишите отзыв о фото или о приложении. Он придёт \1."),
    (r"💬 Type your message or comment for (.+?)\. It goes straight to them\.", r"💬 Напишите сообщение для \1. Оно уйдёт напрямую."),
    (r"✅ Sent to (.+?)\. Thank you!", r"✅ Отправлено: \1. Спасибо!"),
    (r"✅ Thank you! Your feedback was sent to (.+?)\.", r"✅ Спасибо! Ваш отзыв отправлен: \1."),
    (r"✅ Sent (\d+) photo\(s\) to the photographer for approval\. I'll message you when they're added\.",
     r"✅ Отправлено фото: \1. Они ушли фотографу на одобрение. Я напишу вам, когда их добавят."),
    (r"That message timed out\. Tap 💬 Message or comment to (.+?) to start again\.", r"Время вышло. Нажмите «💬 Написать сообщение для \1», чтобы начать заново."),
    (r"That's the most I can take in one go \((\d+)\)\. Tap Send for approval\.", r"Больше за раз принять не могу (\1). Нажмите «Отправить на одобрение»."),
    (r"📅 Open (.+)", r"📅 Открыть: \1"),
    (r"Sorry, (.+?) has a lot of requests waiting\. Please try again later\.", r"Простите, у \1 много запросов в ожидании. Попробуйте позже."),
    (r"✅ Sent to (.+?)! As soon as your family is set up, I'll message you here with your photos\. You can close this chat\.",
     r"✅ Отправлено: \1! Как только ваша семья будет настроена, я пришлю вам фото здесь. Этот чат можно закрыть."),
    (r"Your request is already waiting for (.+?)\. I'll message you here when it's done\. 🙂",
     r"Ваш запрос уже ожидает \1. Я напишу вам здесь, когда всё будет готово. 🙂"),
]
_COMPILED = [(re.compile(p, re.S), t) for p, t in PATTERNS]


def ru(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    hit = EXACT.get(text)
    if hit:
        return hit
    for rx, tpl in _COMPILED:
        m = rx.fullmatch(text)
        if m:
            return m.expand(tpl)
    return text
