import os
import logging
import random
import sqlite3
import datetime
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # На Railway dotenv не обязателен, ENV и так есть
    pass

# =========================
# CONFIG (ENV)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "mathtrain.db").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Add BOT_TOKEN to environment variables (Railway Variables or .env).")

# если путь вида /data/mathtrain.db — гарантируем директорию
db_parent = Path(DB_PATH).expanduser().resolve().parent
db_parent.mkdir(parents=True, exist_ok=True)

# =========================
# LOGGING
# =========================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("mathbot")

# =========================
# DATABASE HELPERS
# =========================
def db_conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = db_conn()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        level TEXT DEFAULT 'новичок',
        xp INTEGER DEFAULT 0,
        total_correct INTEGER DEFAULT 0,
        total_wrong INTEGER DEFAULT 0,
        last_session TEXT,
        streak INTEGER DEFAULT 0,
        theme TEXT DEFAULT 'default'
    )''')

    # делаем (user_id, date) уникальными, чтобы можно было апдейтить дневную статистику
    c.execute('''CREATE TABLE IF NOT EXISTS stats (
        user_id INTEGER,
        date TEXT,
        correct INTEGER DEFAULT 0,
        wrong INTEGER DEFAULT 0,
        avg_time REAL DEFAULT 0.0,
        PRIMARY KEY (user_id, date),
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    )''')

    conn.commit()
    conn.close()

def add_user(user_id: int, username: str):
    conn = db_conn()
    c = conn.cursor()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    c.execute('''INSERT OR IGNORE INTO users
        (user_id, username, level, xp, total_correct, total_wrong, last_session, streak, theme)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (user_id, username, 'новичок', 0, 0, 0, now, 0, 'default')
    )
    conn.commit()
    conn.close()

def get_user_data(user_id: int):
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT user_id, username, level, xp, total_correct, total_wrong, last_session, streak, theme FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {
        'user_id': row[0],
        'username': row[1],
        'level': row[2],
        'xp': row[3],
        'total_correct': row[4],
        'total_wrong': row[5],
        'last_session': row[6],
        'streak': row[7],
        'theme': row[8]
    }

def update_user_data(user_id: int, data: dict):
    conn = db_conn()
    c = conn.cursor()
    c.execute('''UPDATE users SET 
        level = ?, xp = ?, total_correct = ?, total_wrong = ?, 
        last_session = ?, streak = ?, theme = ?
        WHERE user_id = ?''', (
        data['level'], data['xp'], data['total_correct'], data['total_wrong'],
        data['last_session'], data['streak'], data['theme'], user_id
    ))
    conn.commit()
    conn.close()

def upsert_daily_stat(user_id: int, correct_add: int, wrong_add: int, elapsed: float):
    # дневная статистика (простая): суммируем correct/wrong, avg_time — грубое среднее по попыткам
    today = datetime.date.today().isoformat()

    conn = db_conn()
    c = conn.cursor()

    # достаём текущие значения
    c.execute("SELECT correct, wrong, avg_time FROM stats WHERE user_id = ? AND date = ?", (user_id, today))
    row = c.fetchone()

    if row:
        correct, wrong, avg_time = row
        attempts_before = correct + wrong
        attempts_after = attempts_before + 1

        # пересчитываем среднее время
        new_avg = (avg_time * attempts_before + elapsed) / attempts_after

        c.execute('''UPDATE stats
                     SET correct = ?, wrong = ?, avg_time = ?
                     WHERE user_id = ? AND date = ?''',
                  (correct + correct_add, wrong + wrong_add, new_avg, user_id, today))
    else:
        # первая попытка дня
        new_avg = elapsed
        c.execute('''INSERT INTO stats (user_id, date, correct, wrong, avg_time)
                     VALUES (?, ?, ?, ?, ?)''', (user_id, today, correct_add, wrong_add, new_avg))

    conn.commit()
    conn.close()

# =========================
# LEVELS / XP
# =========================
LEVELS = {
    'новичок': {'min_a': 10, 'max_a': 99, 'min_b': 2, 'max_b': 9, 'op': '×'},
    'любитель': {'min_a': 10, 'max_a': 99, 'min_b': 10, 'max_b': 99, 'op': '×'},
    'мастер': {'min_a': 100, 'max_a': 999, 'min_b': 10, 'max_b': 99, 'op': '×'},
    'эксперт': {'min_a': 1000, 'max_a': 9999, 'min_b': 100, 'max_b': 999, 'op': '×'},
    'гений': {'min_a': 1000, 'max_a': 9999, 'min_b': 1000, 'max_b': 9999, 'op': '×'}
}

LEVEL_NAMES = {
    'новичок': 'Легкий: Двухзначное на однозначное',
    'любитель': 'Средний: Двухзначное на двухзначное',
    'мастер': 'Высокий: Трехзначное на двухзначное',
    'эксперт': 'Эксперт: Четырехзначное на трехзначное',
    'гений': 'Гений: Четырехзначное на четырехзначное'
}

XP_PER_LEVEL = {
    'новичок': 5,
    'любитель': 10,
    'мастер': 20,
    'эксперт': 30,
    'гений': 50
}

LEVEL_THRESHOLDS = {
    'новичок': 50,
    'любитель': 150,
    'мастер': 300,
    'эксперт': 600
}

NEXT_LEVEL = {
    'новичок': 'любитель',
    'любитель': 'мастер',
    'мастер': 'эксперт',
    'эксперт': 'гений'
}

# =========================
# THEMES (style)
# =========================
THEMES = {
    'default': {'bg': '🌿', 'correct': '🎉', 'wrong': '💥', 'level_up': '🚀'},
    'космос': {'bg': '🌌', 'correct': '🌠', 'wrong': '🪐', 'level_up': '🛸'},
    'море':   {'bg': '🌊', 'correct': '🐬', 'wrong': '🐙', 'level_up': '⚓'},
    'лес':    {'bg': '🌲', 'correct': '🐿️', 'wrong': '🐻', 'level_up': '🌲'},
    'off':    {'bg': '',   'correct': '',   'wrong': '',   'level_up': ''}
}

THEME_PHRASES = {
    'default': {'correct': 'Правильно!', 'wrong': 'Не совсем так.'},
    'космос':  {'correct': 'Космически точно!', 'wrong': 'Немного мимо… попробуем ещё раз?'},
    'море':    {'correct': 'Отлично! В точку!', 'wrong': 'Чуть-чуть не туда. Подумай ещё.'},
    'лес':     {'correct': 'Верно! Чётко!', 'wrong': 'Почти. Давай ещё раз.'},
    'off':     {'correct': 'Правильно.', 'wrong': 'Неверно.'}
}

def get_theme_emoji(theme: str, key: str) -> str:
    return THEMES.get(theme, THEMES['default']).get(key, '')

def get_theme_phrase(theme: str, key: str) -> str:
    return THEME_PHRASES.get(theme, THEME_PHRASES['default']).get(key, '')

# =========================
# CORE LOGIC
# =========================
def generate_problem(level_key: str):
    config = LEVELS[level_key]
    a = random.randint(config['min_a'], config['max_a'])
    b = random.randint(config['min_b'], config['max_b'])
    problem = f"{a} {config['op']} {b}"
    answer = a * b
    return problem, answer, a, b

def format_last_session(last_session) -> str:
    if not last_session:
        return "—"
    return str(last_session)[:10]

def build_main_menu_text(user_data: dict, username: str) -> str:
    return (
        f"👋 Привет, {username}!\n"
        f"Я — тренер по устному счёту 🧠\n\n"
        f"🎯 *Уровень:* {user_data['level'].capitalize()} | XP: {user_data['xp']}\n"
        f"✅ Правильных: {user_data['total_correct']}\n"
        f"❌ Ошибок: {user_data['total_wrong']}\n"
        f"🔥 Серия: {user_data['streak']}\n\n"
        f"Команды: /hint /answer /theory /stats /theme /stop"
    )

def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(LEVEL_NAMES['новичок'], callback_data='level_новичок')],
        [InlineKeyboardButton(LEVEL_NAMES['любитель'], callback_data='level_любитель')],
        [InlineKeyboardButton(LEVEL_NAMES['мастер'], callback_data='level_мастер')],
        [InlineKeyboardButton(LEVEL_NAMES['эксперт'], callback_data='level_эксперт')],
        [InlineKeyboardButton(LEVEL_NAMES['гений'], callback_data='level_гений')],
        [InlineKeyboardButton("📊 Моя статистика", callback_data='stats')],
        [InlineKeyboardButton("🎨 Выбрать тему", callback_data='theme')],
        [InlineKeyboardButton("📚 Теория", callback_data='theory')],
        [InlineKeyboardButton("🔚 Выйти из тренировки", callback_data='stop')]
    ]
    return InlineKeyboardMarkup(keyboard)

async def safe_edit_or_send(query, text: str, *, reply_markup=None, parse_mode=None):
    """
    Telegram иногда не даёт редактировать сообщения (старое/не то/и т.д.)
    Тогда отправляем новым.
    """
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f"edit_message_text failed, fallback to send_message: {e}")
        await query.message.chat.send_message(text=text, reply_markup=reply_markup, parse_mode=parse_mode)

# =========================
# COMMANDS / HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    add_user(user_id, username)
    user_data = get_user_data(user_id)

    # сброс текущего примера
    context.user_data.pop('current_problem', None)

    # убрать reply-клаву, если осталась от других ботов
    if update.message:
        await update.message.reply_text("Обновляю интерфейс…", reply_markup=ReplyKeyboardRemove())

    msg = build_main_menu_text(user_data, username)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=msg,
        reply_markup=build_main_menu_keyboard(),
        parse_mode="Markdown"
    )

async def send_problem(chat_id: int, bot, user_id: int, level_key: str, context):
    problem, answer, a, b = generate_problem(level_key)
    context.user_data['current_problem'] = {
        'problem': problem,
        'answer': answer,
        'a': a,
        'b': b,
        'start_time': datetime.datetime.now(),
        'level': level_key
    }
    await bot.send_message(chat_id, f"🔢 *{problem} = ?*", parse_mode="Markdown")

def theory_text() -> str:
    return (
        "📚 Теория устного умножения\n\n"
        "1) Разложение на десятки и единицы:\n"
        "   47×8 = (40+7)×8 = 40×8 + 7×8\n\n"
        "2) Числа 10–19 как (10+x):\n"
        "   14×7 = (10+4)×7 = 70 + 28\n\n"
        "3) Округление и компенсация:\n"
        "   49×6 = 50×6 − 6\n\n"
        "Команды в тренировке:\n"
        "/hint — подсказка\n"
        "/answer — ответ + кнопка нового примера"
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    user_id = user.id
    username = user.username or user.first_name

    # гарантируем пользователя
    add_user(user_id, username)
    user_data = get_user_data(user_id)

    data = query.data

    if data.startswith("level_"):
        level_key = data.split("_", 1)[1]
        user_data['level'] = level_key
        user_data['last_session'] = datetime.datetime.now().isoformat(timespec="seconds")
        update_user_data(user_id, user_data)

        await safe_edit_or_send(query, f"✅ Уровень установлен: *{LEVEL_NAMES[level_key]}*", parse_mode="Markdown")
        await send_problem(query.message.chat.id, context.bot, user_id, level_key, context)

    elif data == "stats":
        stats_msg = (
            f"📊 *Статистика {username}*\n"
            f"Уровень: {user_data['level'].capitalize()}\n"
            f"XP: {user_data['xp']}\n"
            f"Правильных: {user_data['total_correct']}\n"
            f"Ошибок: {user_data['total_wrong']}\n"
            f"Серия: {user_data['streak']} ✅\n"
            f"Последняя тренировка: {format_last_session(user_data['last_session'])}"
        )
        await safe_edit_or_send(query, stats_msg, parse_mode="Markdown")

    elif data == "theme":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌿 По умолчанию", callback_data="theme_default")],
            [InlineKeyboardButton("🌌 Космос", callback_data="theme_космос")],
            [InlineKeyboardButton("🌊 Море", callback_data="theme_море")],
            [InlineKeyboardButton("🌲 Лес", callback_data="theme_лес")],
            [InlineKeyboardButton("🚫 Без оформления", callback_data="theme_off")],
            [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
        ])
        await safe_edit_or_send(query, "🎨 Выбери тему / антураж:", reply_markup=kb)

    elif data.startswith("theme_"):
        theme = data.split("_", 1)[1]
        user_data['theme'] = theme
        update_user_data(user_id, user_data)

        label = "Без оформления" if theme == "off" else theme
        emoji = get_theme_emoji(theme, "bg")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Начать тренировку", callback_data="start_training")],
            [InlineKeyboardButton("🔙 В меню", callback_data="menu")]
        ])
        await safe_edit_or_send(query, f"{emoji} Тема изменена на '{label}'!", reply_markup=kb)

    elif data == "theory":
        await safe_edit_or_send(query, theory_text())

    elif data == "start_training":
        context.user_data.pop("current_problem", None)
        await send_problem(query.message.chat.id, context.bot, user_id, user_data['level'], context)

    elif data == "next_example":
        await send_problem(query.message.chat.id, context.bot, user_id, user_data['level'], context)

    elif data == "menu":
        msg = build_main_menu_text(user_data, username)
        await safe_edit_or_send(query, msg, reply_markup=build_main_menu_keyboard(), parse_mode="Markdown")

    elif data == "stop":
        context.user_data.pop("current_problem", None)
        await safe_edit_or_send(query, "🔚 Тренировка завершена. Возвращайся снова!")
        await query.message.reply_text("Чтобы начать снова — нажми /start", reply_markup=ReplyKeyboardRemove())

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'current_problem' not in context.user_data:
        await update.message.reply_text("💡 Начни тренировку с помощью /start")
        return

    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    add_user(user_id, username)
    user_data = get_user_data(user_id)

    current = context.user_data['current_problem']

    try:
        user_answer = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Введи число!")
        return
    except Exception as e:
        logger.error(f"Ошибка парсинга ответа от {user_id}: {e}")
        await update.message.reply_text("❌ Ошибка. Попробуй снова.")
        return

    elapsed = (datetime.datetime.now() - current['start_time']).total_seconds()
    is_correct = (user_answer == current['answer'])

    user_data['last_session'] = datetime.datetime.now().isoformat(timespec="seconds")

    if is_correct:
        user_data['total_correct'] += 1
        user_data['streak'] += 1

        current_level = user_data['level']
        xp_gain = XP_PER_LEVEL[current_level]

        xp_before = user_data['xp']
        xp_after = xp_before + xp_gain
        user_data['xp'] = xp_after

        # level up только на пересечении порога
        next_level = None
        if current_level in LEVEL_THRESHOLDS:
            threshold = LEVEL_THRESHOLDS[current_level]
            if xp_before < threshold <= xp_after:
                next_level = NEXT_LEVEL[current_level]

        if next_level:
            user_data['level'] = next_level
            lvl_emoji = get_theme_emoji(user_data['theme'], 'level_up') or "🚀"
            await update.message.reply_text(f"{lvl_emoji} Поздравляю! Новый уровень: *{next_level.capitalize()}*", parse_mode="Markdown")

        emoji = get_theme_emoji(user_data['theme'], 'correct')
        phrase = get_theme_phrase(user_data['theme'], 'correct')
        text = f"{emoji} {phrase} +{xp_gain} XP. ⏱ {elapsed:.1f} сек." if emoji else f"{phrase} +{xp_gain} XP. Время: {elapsed:.1f} сек."
        await update.message.reply_text(text)

        update_user_data(user_id, user_data)
        upsert_daily_stat(user_id, correct_add=1, wrong_add=0, elapsed=elapsed)

        # новый пример на актуальном уровне
        await send_problem(update.message.chat_id, context.bot, user_id, user_data['level'], context)

    else:
        user_data['total_wrong'] += 1
        user_data['streak'] = 0

        emoji = get_theme_emoji(user_data['theme'], 'wrong')
        phrase = get_theme_phrase(user_data['theme'], 'wrong')
        prefix = f"{emoji} " if emoji else ""

        # ВАЖНО: не показываем правильный ответ после ошибки
        await update.message.reply_text(
            f"{prefix}{phrase}\n"
            "Подумай ещё раз и попробуй снова.\n\n"
            "Если нужна помощь:\n"
            "• /hint — подсказка\n"
            "• /answer — показать ответ"
        )

        update_user_data(user_id, user_data)
        upsert_daily_stat(user_id, correct_add=0, wrong_add=1, elapsed=elapsed)

async def hint_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'current_problem' not in context.user_data:
        await update.message.reply_text("Сначала начни тренировку: /start")
        return

    current = context.user_data['current_problem']
    a, b = current['a'], current['b']
    tens = a // 10
    ones = a % 10

    if 10 <= a <= 19 and tens == 1:
        decomposition = f"{a} = 10 + {ones}\n(10 + {ones})×{b} = 10×{b} + {ones}×{b}"
    else:
        decomposition = f"{a} = {tens}×10 + {ones}\n({tens}×10 + {ones})×{b} = {tens}×10×{b} + {ones}×{b}"

    await update.message.reply_text(
        "💡 Подсказка к текущему примеру:\n"
        f"{current['problem']} = ?\n\n"
        f"{decomposition}\n\n"
        "Досчитай и введи ответ числом 🙂"
    )

async def answer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'current_problem' not in context.user_data:
        await update.message.reply_text("Сначала начни тренировку: /start")
        return

    current = context.user_data['current_problem']
    ans = current['answer']

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➡️ Новый пример", callback_data="next_example")]
    ])

    await update.message.reply_text(
        f"✅ Ответ:\n{current['problem']} = {ans}\n\nГотов к следующему?",
        reply_markup=kb
    )

async def theory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(theory_text())

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    add_user(user_id, username)
    user_data = get_user_data(user_id)

    stats_msg = (
        f"📊 *Статистика {username}*\n"
        f"Уровень: {user_data['level'].capitalize()}\n"
        f"XP: {user_data['xp']}\n"
        f"Правильных: {user_data['total_correct']}\n"
        f"Ошибок: {user_data['total_wrong']}\n"
        f"Серия: {user_data['streak']} ✅\n"
        f"Последняя тренировка: {format_last_session(user_data['last_session'])}"
    )
    await update.message.reply_text(stats_msg, parse_mode="Markdown")

async def theme_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌿 По умолчанию", callback_data="theme_default")],
        [InlineKeyboardButton("🌌 Космос", callback_data="theme_космос")],
        [InlineKeyboardButton("🌊 Море", callback_data="theme_море")],
        [InlineKeyboardButton("🌲 Лес", callback_data="theme_лес")],
        [InlineKeyboardButton("🚫 Без оформления", callback_data="theme_off")]
    ])
    await update.message.reply_text("🎨 Выбери тему / антураж:", reply_markup=kb)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("current_problem", None)
    await update.message.reply_text("🔚 Тренировка завершена. Возвращайся снова!", reply_markup=ReplyKeyboardRemove())

# =========================
# MAIN
# =========================
def main():
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("theme", theme_command))
    application.add_handler(CommandHandler("theory", theory_command))
    application.add_handler(CommandHandler("hint", hint_command))
    application.add_handler(CommandHandler("answer", answer_command))
    application.add_handler(CommandHandler("stop", stop_command))

    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer))

    print("🤖 Bot started.")
    application.run_polling()

if __name__ == "__main__":
    main()
