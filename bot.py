import asyncio
import datetime
import json
import os
import time
import re
import calendar
import requests  # <-- БЫЛО ПРОПУЩЕНО
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    filters, ConversationHandler, CallbackQueryHandler
)

# ==================== КОНФИГУРАЦИЯ ====================
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

OZON_POSTING_FBO_URL = "https://api-seller.ozon.ru/v2/posting/fbo/list"
MANAGERS_FILE = "managers.json"
LOG_FILE = "/app/data/ozon_log.txt"

# Состояния для диалогов
WAITING_DATE_SINGLE = 1
WAITING_PERIOD_TYPE = 2
WAITING_PERIOD_START = 3
WAITING_PERIOD_END = 4
WAITING_ADD_MANAGER = 5
WAITING_REMOVE_MANAGER = 6
WAITING_MONTH_SELECT = 7
WAITING_QUARTER_SELECT = 8
WAITING_YEAR_SELECT = 9
# =====================================================

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
def write_log(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"[{timestamp}] {message}"
    print(full_msg, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(full_msg + "\n")
    except:
        pass

def load_managers():
    if os.path.exists(MANAGERS_FILE):
        with open(MANAGERS_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except:
                return []
    return []

def save_managers(managers):
    with open(MANAGERS_FILE, "w", encoding="utf-8") as f:
        json.dump(managers, f, ensure_ascii=False, indent=2)

def is_manager(chat_id):
    return chat_id in load_managers()

def add_manager(chat_id):
    managers = load_managers()
    if chat_id not in managers:
        managers.append(chat_id)
        save_managers(managers)
        return True
    return False

def remove_manager(chat_id):
    managers = load_managers()
    if chat_id in managers:
        managers.remove(chat_id)
        save_managers(managers)
        return True
    return False

# ---------- КАЛЕНДАРЬ ----------
def create_calendar(year, month, callback_prefix):
    month_names = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                   "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    keyboard = []
    header = f"{month_names[month-1]} {year}"
    keyboard.append([InlineKeyboardButton(header, callback_data="ignore")])
    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    row = [InlineKeyboardButton(day, callback_data="ignore") for day in week_days]
    keyboard.append(row)

    first_day, num_days = calendar.monthrange(year, month)
    row = []
    for _ in range(first_day):
        row.append(InlineKeyboardButton(" ", callback_data="ignore"))
    for day in range(1, num_days + 1):
        row.append(InlineKeyboardButton(str(day), callback_data=f"{callback_prefix}{year}-{month:02d}-{day:02d}"))
        if len(row) == 7:
            keyboard.append(row)
            row = []
    if row:
        while len(row) < 7:
            row.append(InlineKeyboardButton(" ", callback_data="ignore"))
        keyboard.append(row)

    nav_row = [
        InlineKeyboardButton("◀️", callback_data=f"{callback_prefix}prev_month_{year}_{month}"),
        InlineKeyboardButton(" ", callback_data="ignore"),
        InlineKeyboardButton("▶️", callback_data=f"{callback_prefix}next_month_{year}_{month}")
    ]
    keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"{callback_prefix}cancel")])
    return InlineKeyboardMarkup(keyboard)

# ---------- ПОЛУЧЕНИЕ ДАННЫХ ИЗ OZON ----------
def fetch_postings(date_from, date_to):
    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "date_from": date_from,
        "date_to": date_to,
        "status": "",
        "limit": 1000,
        "offset": 0,
    }
    all_postings = []
    while True:
        try:
            response = requests.post(OZON_POSTING_FBO_URL, headers=headers, json=payload, timeout=15)
            if response.status_code == 429:
                time.sleep(10)
                continue
            response.raise_for_status()
            data = response.json()
            postings = data.get("result", [])
            if not postings:
                break
            all_postings.extend(postings)
            if len(postings) < payload["limit"]:
                break
            payload["offset"] += payload["limit"]
        except Exception as e:
            write_log(f"❌ Ошибка получения отгрузок: {e}")
            break
    write_log(f"📦 Загружено отгрузок: {len(all_postings)} за период {date_from} – {date_to}")
    return all_postings

def aggregate_postings(postings, date_from=None, date_to=None):
    aggregated = {}
    for posting in postings:
        created_at = posting.get("created_at", "")
        if not created_at:
            continue
        date_str = created_at[:10]
        if date_from and date_str < date_from:
            continue
        if date_to and date_str > date_to:
            continue

        products = posting.get("products", [])
        total_units = 0
        total_sum = 0.0
        for product in products:
            qty = int(product.get("quantity", 0))
            price_str = product.get("price", "0")
            try:
                price = float(price_str)
            except:
                price = 0.0
            total_units += qty
            total_sum += price * qty

        if date_str not in aggregated:
            aggregated[date_str] = {
                "ordered_units": 0,
                "ordered_sum": 0.0,
                "delivered_units": 0,
                "delivered_sum": 0.0,
                "canceled_units": 0,
                "canceled_sum": 0.0,
            }

        aggregated[date_str]["ordered_units"] += total_units
        aggregated[date_str]["ordered_sum"] += total_sum

        status = posting.get("status", "")
        if status in ("cancelled", "canceled"):
            aggregated[date_str]["canceled_units"] += total_units
            aggregated[date_str]["canceled_sum"] += total_sum
        elif status in ("delivered", "completed"):
            aggregated[date_str]["delivered_units"] += total_units
            aggregated[date_str]["delivered_sum"] += total_sum

    return aggregated

def get_metrics_for_date(date_str):
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=183)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    postings = fetch_postings(start, end)
    agg = aggregate_postings(postings, date_from=date_str, date_to=date_str)
    return agg.get(date_str, {})

def get_metrics_for_period(date_from, date_to):
    postings = fetch_postings(date_from, date_to)
    agg = aggregate_postings(postings, date_from=date_from, date_to=date_to)
    total = {
        "ordered_units": 0,
        "ordered_sum": 0.0,
        "delivered_units": 0,
        "delivered_sum": 0.0,
        "canceled_units": 0,
        "canceled_sum": 0.0,
    }
    for vals in agg.values():
        for key in total:
            total[key] += vals.get(key, 0)
    return total

def get_current_metrics():
    today = datetime.date.today()
    first_day = today.replace(day=1)
    date_from = first_day.strftime("%Y-%m-%d")
    date_to = today.strftime("%Y-%m-%d")
    postings = fetch_postings(date_from, date_to)
    agg = aggregate_postings(postings, date_from=date_from, date_to=date_to)

    today_str = today.strftime("%Y-%m-%d")
    yesterday_str = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    today_data = agg.get(today_str, {})
    yesterday_data = agg.get(yesterday_str, {})

    month_data = {
        "ordered_units": 0,
        "ordered_sum": 0.0,
        "delivered_units": 0,
        "delivered_sum": 0.0,
        "canceled_units": 0,
        "canceled_sum": 0.0,
    }
    for vals in agg.values():
        for key in month_data:
            month_data[key] += vals.get(key, 0)

    return today_data, yesterday_data, month_data

# ---------- ФОРМАТИРОВАНИЕ ----------
def format_metrics(metrics, title):
    if not metrics or all(v == 0 for v in metrics.values()):
        return f"📊 *{title}*\n\n❌ Нет данных за указанный период."
    return (
        f"📊 *{title}*\n\n"
        f"🛒 *Заказано*\n  На сумму: {metrics.get('ordered_sum', 0):,.2f} ₽\n"
        f"  Штук: {metrics.get('ordered_units', 0)}\n\n"
        f"📦 *Доставлено*\n  На сумму: {metrics.get('delivered_sum', 0):,.2f} ₽\n"
        f"  Штук: {metrics.get('delivered_units', 0)}\n\n"
        f"❌ *Отмены*\n  На сумму: {metrics.get('canceled_sum', 0):,.2f} ₽\n"
        f"  Штук: {metrics.get('canceled_units', 0)}"
    )

# ---------- КЛАВИАТУРЫ ----------
def main_admin_keyboard():
    buttons = [
        [KeyboardButton("📊 Отчёт")],
        [KeyboardButton("⚙️ Администрирование")]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def main_user_keyboard():
    buttons = [[KeyboardButton("📊 Отчёт")]]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def reports_keyboard():
    buttons = [
        [KeyboardButton("📅 Текущие показатели")],
        [KeyboardButton("📆 Выбрать дату")],
        [KeyboardButton("📊 Выбрать период")],
        [KeyboardButton("🔙 Назад")]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def admin_keyboard():
    buttons = [
        [KeyboardButton("➕ Добавить менеджера"), KeyboardButton("➖ Удалить менеджера")],
        [KeyboardButton("📋 Список менеджеров")],
        [KeyboardButton("🔙 Назад")]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

# ---------- ОБРАБОТЧИКИ КОМАНД ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id == ADMIN_CHAT_ID:
        await update.message.reply_text(
            "👋 Добро пожаловать, администратор!",
            reply_markup=main_admin_keyboard()
        )
    elif is_manager(chat_id):
        await update.message.reply_text(
            "👋 Здравствуйте, менеджер!",
            reply_markup=main_user_keyboard()
        )
    else:
        await update.message.reply_text(
            "Доступ запрещён.",
            reply_markup=ReplyKeyboardRemove()
        )

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if text == "📊 Отчёт":
        await update.message.reply_text(
            "Выберите тип отчёта:",
            reply_markup=reports_keyboard()
        )
        return

    if text == "⚙️ Администрирование":
        if chat_id != ADMIN_CHAT_ID:
            await update.message.reply_text("⛔ Только для администратора.")
            return
        await update.message.reply_text(
            "Управление менеджерами:",
            reply_markup=admin_keyboard()
        )
        return

    await update.message.reply_text("Используйте кнопки меню.")

async def handle_reports_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "📅 Текущие показатели":
        today_m, yesterday_m, month_m = get_current_metrics()
        await update.message.reply_text(
            format_metrics(today_m, "Сегодня"),
            parse_mode="Markdown"
        )
        await update.message.reply_text(
            format_metrics(yesterday_m, "Вчера"),
            parse_mode="Markdown"
        )
        await update.message.reply_text(
            format_metrics(month_m, "Текущий месяц"),
            parse_mode="Markdown"
        )
        return

    if text == "📆 Выбрать дату":
        now = datetime.date.today()
        year, month = now.year, now.month
        keyboard = create_calendar(year, month, "date_")
        await update.message.reply_text(
            "Выберите дату:",
            reply_markup=keyboard
        )
        return WAITING_DATE_SINGLE

    if text == "📊 Выбрать период":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗓️ По месяцам", callback_data="period_month")],
            [InlineKeyboardButton("📅 По кварталам", callback_data="period_quarter")],
            [InlineKeyboardButton("📆 По годам", callback_data="period_year")],
            [InlineKeyboardButton("📊 Произвольный период", callback_data="period_custom")],
            [InlineKeyboardButton("❌ Отмена", callback_data="period_cancel")]
        ])
        await update.message.reply_text(
            "Выберите тип периода:",
            reply_markup=keyboard
        )
        return WAITING_PERIOD_TYPE

    if text == "🔙 Назад":
        chat_id = update.effective_chat.id
        if chat_id == ADMIN_CHAT_ID:
            await update.message.reply_text(
                "Главное меню",
                reply_markup=main_admin_keyboard()
            )
        else:
            await update.message.reply_text(
                "Главное меню",
                reply_markup=main_user_keyboard()
            )
        return

    await update.message.reply_text("Неизвестная команда.")

async def handle_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ Только для администратора.")
        return

    text = update.message.text

    if text == "📋 Список менеджеров":
        managers = load_managers()
        if not managers:
            await update.message.reply_text("Список менеджеров пуст.")
        else:
            await update.message.reply_text(
                "📋 Список ID менеджеров:\n" + "\n".join(str(m) for m in managers)
            )
        return

    if text == "🔙 Назад":
        await update.message.reply_text(
            "Главное меню",
            reply_markup=main_admin_keyboard()
        )
        return

    await update.message.reply_text("Неизвестная команда.")

# ---------- ОБРАБОТЧИКИ INLINE CALLBACK ----------
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # ----- Календарь (выбор даты) -----
    if data.startswith("date_"):
        if data == "date_cancel":
            await query.edit_message_text("Выбор даты отменён.")
            await query.message.reply_text(
                "Выберите действие:",
                reply_markup=reports_keyboard()
            )
            return ConversationHandler.END

        parts = data.split("_")
        if len(parts) == 4 and parts[1] in ("prev_month", "next_month"):
            action = parts[1]
            year = int(parts[2])
            month = int(parts[3])
            if action == "prev_month":
                if month == 1:
                    month = 12
                    year -= 1
                else:
                    month -= 1
            elif action == "next_month":
                if month == 12:
                    month = 1
                    year += 1
                else:
                    month += 1
            keyboard = create_calendar(year, month, "date_")
            await query.edit_message_reply_markup(reply_markup=keyboard)
            return WAITING_DATE_SINGLE

        # Выбор конкретной даты: date_2025-05-01
        date_str = data[5:]
        if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            metrics = get_metrics_for_date(date_str)
            msg = format_metrics(metrics, f"Отчёт за {date_str}")
            await query.edit_message_text(msg, parse_mode="Markdown")
            await query.message.reply_text(
                "Выберите действие:",
                reply_markup=reports_keyboard()
            )
            return ConversationHandler.END
        else:
            await query.edit_message_text("Ошибка формата даты.")
            return WAITING_DATE_SINGLE

    # ----- Выбор периода -----
    if data == "period_month":
        months = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
        buttons = [[InlineKeyboardButton(name, callback_data=f"month_{i}")] for i, name in enumerate(months, 1)]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="period_cancel")])
        await query.edit_message_text("Выберите месяц:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_MONTH_SELECT

    if data == "period_quarter":
        quarters = ["1 квартал (янв-мар)", "2 квартал (апр-июн)", "3 квартал (июл-сен)", "4 квартал (окт-дек)"]
        buttons = [[InlineKeyboardButton(name, callback_data=f"quarter_{i}")] for i, name in enumerate(quarters, 1)]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="period_cancel")])
        await query.edit_message_text("Выберите квартал:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_QUARTER_SELECT

    if data == "period_year":
        current_year = datetime.date.today().year
        years = list(range(current_year - 9, current_year + 1))
        buttons = [[InlineKeyboardButton(str(y), callback_data=f"year_{y}")] for y in years]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="period_cancel")])
        await query.edit_message_text("Выберите год:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_YEAR_SELECT

    if data == "period_custom":
        now = datetime.date.today()
        year, month = now.year, now.month
        keyboard = create_calendar(year, month, "start_")
        await query.edit_message_text("Выберите начальную дату:", reply_markup=keyboard)
        return WAITING_PERIOD_START

    if data == "period_cancel":
        await query.edit_message_text("Выбор периода отменён.")
        await query.message.reply_text(
            "Выберите действие:",
            reply_markup=reports_keyboard()
        )
        return ConversationHandler.END

    # ----- Месяц, квартал, год -----
    if data.startswith("month_"):
        month_num = int(data.split("_")[1])
        year = datetime.date.today().year
        first_day = datetime.date(year, month_num, 1)
        if month_num == 12:
            last_day = datetime.date(year, 12, 31)
        else:
            last_day = datetime.date(year, month_num+1, 1) - datetime.timedelta(days=1)
        date_from = first_day.strftime("%Y-%m-%d")
        date_to = last_day.strftime("%Y-%m-%d")
        metrics = get_metrics_for_period(date_from, date_to)
        msg = format_metrics(metrics, f"Месяц {first_day.strftime('%B %Y')}")
        await query.edit_message_text(msg, parse_mode="Markdown")
        await query.message.reply_text(
            "Выберите действие:",
            reply_markup=reports_keyboard()
        )
        return ConversationHandler.END

    if data.startswith("quarter_"):
        q = int(data.split("_")[1])
        year = datetime.date.today().year
        start_month = (q-1)*3 + 1
        end_month = q*3
        first_day = datetime.date(year, start_month, 1)
        if end_month == 12:
            last_day = datetime.date(year, 12, 31)
        else:
            last_day = datetime.date(year, end_month+1, 1) - datetime.timedelta(days=1)
        date_from = first_day.strftime("%Y-%m-%d")
        date_to = last_day.strftime("%Y-%m-%d")
        metrics = get_metrics_for_period(date_from, date_to)
        msg = format_metrics(metrics, f"{q} квартал {year}")
        await query.edit_message_text(msg, parse_mode="Markdown")
        await query.message.reply_text(
            "Выберите действие:",
            reply_markup=reports_keyboard()
        )
        return ConversationHandler.END

    if data.startswith("year_"):
        year = int(data.split("_")[1])
        first_day = datetime.date(year, 1, 1)
        last_day = datetime.date(year, 12, 31)
        date_from = first_day.strftime("%Y-%m-%d")
        date_to = last_day.strftime("%Y-%m-%d")
        metrics = get_metrics_for_period(date_from, date_to)
        msg = format_metrics(metrics, f"Год {year}")
        await query.edit_message_text(msg, parse_mode="Markdown")
        await query.message.reply_text(
            "Выберите действие:",
            reply_markup=reports_keyboard()
        )
        return ConversationHandler.END

    # ----- Календарь для произвольного периода (начало) -----
    if data.startswith("start_"):
        if data == "start_cancel":
            await query.edit_message_text("Выбор периода отменён.")
            await query.message.reply_text(
                "Выберите действие:",
                reply_markup=reports_keyboard()
            )
            return ConversationHandler.END
        parts = data.split("_")
        if len(parts) == 4 and parts[1] in ("prev_month", "next_month"):
            action = parts[1]
            year = int(parts[2])
            month = int(parts[3])
            if action == "prev_month":
                if month == 1:
                    month = 12
                    year -= 1
                else:
                    month -= 1
            elif action == "next_month":
                if month == 12:
                    month = 1
                    year += 1
                else:
                    month += 1
            keyboard = create_calendar(year, month, "start_")
            await query.edit_message_reply_markup(reply_markup=keyboard)
            return WAITING_PERIOD_START
        date_str = data[6:]
        if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            context.user_data['period_start_date'] = date_str
            now = datetime.date.today()
            year, month = now.year, now.month
            keyboard = create_calendar(year, month, "end_")
            await query.edit_message_text(
                f"Начало: {date_str}\nТеперь выберите конечную дату:",
                reply_markup=keyboard
            )
            return WAITING_PERIOD_END
        else:
            await query.edit_message_text("Ошибка формата даты.")
            return WAITING_PERIOD_START

    # ----- Календарь для произвольного периода (конец) -----
    if data.startswith("end_"):
        if data == "end_cancel":
            await query.edit_message_text("Выбор периода отменён.")
            await query.message.reply_text(
                "Выберите действие:",
                reply_markup=reports_keyboard()
            )
            return ConversationHandler.END
        parts = data.split("_")
        if len(parts) == 4 and parts[1] in ("prev_month", "next_month"):
            action = parts[1]
            year = int(parts[2])
            month = int(parts[3])
            if action == "prev_month":
                if month == 1:
                    month = 12
                    year -= 1
                else:
                    month -= 1
            elif action == "next_month":
                if month == 12:
                    month = 1
                    year += 1
                else:
                    month += 1
            keyboard = create_calendar(year, month, "end_")
            await query.edit_message_reply_markup(reply_markup=keyboard)
            return WAITING_PERIOD_END
        end_date_str = data[4:]
        if re.match(r"\d{4}-\d{2}-\d{2}", end_date_str):
            start_date = context.user_data.get('period_start_date')
            if not start_date:
                await query.edit_message_text("Ошибка: не найдена начальная дата. Попробуйте снова.")
                return ConversationHandler.END
            if start_date > end_date_str:
                await query.edit_message_text("❌ Начальная дата не может быть позже конечной. Попробуйте сначала.")
                now = datetime.date.today()
                year, month = now.year, now.month
                keyboard = create_calendar(year, month, "start_")
                await query.message.reply_text(
                    "Выберите начальную дату заново:",
                    reply_markup=keyboard
                )
                return WAITING_PERIOD_START
            metrics = get_metrics_for_period(start_date, end_date_str)
            msg = format_metrics(metrics, f"Период {start_date} – {end_date_str}")
            await query.edit_message_text(msg, parse_mode="Markdown")
            await query.message.reply_text(
                "Выберите действие:",
                reply_markup=reports_keyboard()
            )
            context.user_data.pop('period_start_date', None)
            return ConversationHandler.END
        else:
            await query.edit_message_text("Ошибка формата даты.")
            return WAITING_PERIOD_END

    await query.edit_message_text("Неизвестная команда.")
    return ConversationHandler.END

# ---------- ДИАЛОГИ ДЛЯ АДМИНИСТРИРОВАНИЯ ----------
async def add_manager_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введите ID пользователя (только цифры):"
    )
    return WAITING_ADD_MANAGER

async def add_manager_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ Только для администратора.")
        return ConversationHandler.END

    try:
        user_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(
            "❌ Ошибка: ID должен быть числом. Попробуйте снова."
        )
        return WAITING_ADD_MANAGER

    if add_manager(user_id):
        await update.message.reply_text(f"✅ Менеджер с ID {user_id} добавлен.")
    else:
        await update.message.reply_text(f"⚠️ Менеджер с ID {user_id} уже существует.")
    await update.message.reply_text(
        "Управление менеджерами:",
        reply_markup=admin_keyboard()
    )
    return ConversationHandler.END

async def remove_manager_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введите ID пользователя (только цифры):"
    )
    return WAITING_REMOVE_MANAGER

async def remove_manager_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ Только для администратора.")
        return ConversationHandler.END

    try:
        user_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(
            "❌ Ошибка: ID должен быть числом. Попробуйте снова."
        )
        return WAITING_REMOVE_MANAGER

    if remove_manager(user_id):
        await update.message.reply_text(f"✅ Менеджер с ID {user_id} удалён.")
    else:
        await update.message.reply_text(f"❌ Менеджер с ID {user_id} не найден.")
    await update.message.reply_text(
        "Управление менеджерами:",
        reply_markup=admin_keyboard()
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id == ADMIN_CHAT_ID:
        keyboard = main_admin_keyboard()
    else:
        keyboard = main_user_keyboard()
    await update.message.reply_text(
        "Действие отменено.",
        reply_markup=keyboard
    )
    return ConversationHandler.END

# ---------- ЗАПУСК ----------
def main():
    if not all([OZON_CLIENT_ID, OZON_API_KEY, TELEGRAM_BOT_TOKEN]) or ADMIN_CHAT_ID == 0:
        write_log("❌ ОШИБКА: Не все переменные окружения установлены!")
        return

    write_log("🚀 Запуск бота...")

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .build()
    )

    application.add_handler(CommandHandler("start", start))

    application.add_handler(MessageHandler(
        filters.Text(["📊 Отчёт", "⚙️ Администрирование"]),
        handle_main_menu
    ))
    application.add_handler(MessageHandler(
        filters.Text(["📅 Текущие показатели", "🔙 Назад"]),
        handle_reports_menu
    ))
    application.add_handler(MessageHandler(
        filters.Text(["📋 Список менеджеров", "🔙 Назад"]),
        handle_admin_menu
    ))

    # Конфигурация ConversationHandler с per_message=True для избежания предупреждений
    conv_date = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📆 Выбрать дату"), handle_reports_menu)],
        states={
            WAITING_DATE_SINGLE: [CallbackQueryHandler(handle_callback_query)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=True,
    )

    conv_period = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📊 Выбрать период"), handle_reports_menu)],
        states={
            WAITING_PERIOD_TYPE: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PERIOD_START: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PERIOD_END: [CallbackQueryHandler(handle_callback_query)],
            WAITING_MONTH_SELECT: [CallbackQueryHandler(handle_callback_query)],
            WAITING_QUARTER_SELECT: [CallbackQueryHandler(handle_callback_query)],
            WAITING_YEAR_SELECT: [CallbackQueryHandler(handle_callback_query)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=True,
    )

    conv_add_manager = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("➕ Добавить менеджера"), add_manager_start)],
        states={
            WAITING_ADD_MANAGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_manager_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    conv_remove_manager = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("➖ Удалить менеджера"), remove_manager_start)],
        states={
            WAITING_REMOVE_MANAGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_manager_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_date)
    application.add_handler(conv_period)
    application.add_handler(conv_add_manager)
    application.add_handler(conv_remove_manager)

    application.add_handler(CallbackQueryHandler(handle_callback_query))

    async def scheduled_report(context):
        moscow_tz = datetime.timezone(datetime.timedelta(hours=3))
        now = datetime.datetime.now(moscow_tz)
        if not (9 <= now.hour <= 23):
            return

        managers = load_managers()
        if not managers:
            return

        today_m, yesterday_m, month_m = get_current_metrics()
        msg_today = format_metrics(today_m, "Сегодня")
        msg_yesterday = format_metrics(yesterday_m, "Вчера")
        msg_month = format_metrics(month_m, "Текущий месяц")

        for chat_id in managers:
            try:
                await context.bot.send_message(chat_id=chat_id, text=msg_today, parse_mode="Markdown")
                await context.bot.send_message(chat_id=chat_id, text=msg_yesterday, parse_mode="Markdown")
                await context.bot.send_message(chat_id=chat_id, text=msg_month, parse_mode="Markdown")
            except Exception as e:
                write_log(f"❌ Ошибка отправки для {chat_id}: {e}")

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(scheduled_report, interval=60*60, first=0)
        write_log("✅ Планировщик отчётов запущен (каждый час с 9:00 до 23:00 МСК).")
    else:
        write_log("⚠️ JobQueue недоступен! Автоматические отчёты не будут работать.")

    write_log("🚀 Бот готов.")
    application.run_polling(allowed_updates=Update.ALL_TYPES, timeout=30)

if __name__ == "__main__":
    main()
