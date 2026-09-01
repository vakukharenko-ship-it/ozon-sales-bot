import asyncio
import datetime
import json
import os
import time
import re
import requests
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    filters, ConversationHandler
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
WAITING_DATE_SINGLE = 1          # для выбора конкретной даты
WAITING_DATE_PERIOD_START = 2   # для периода: ожидание начальной даты
WAITING_DATE_PERIOD_END = 3     # для периода: ожидание конечной даты
WAITING_ADD_MANAGER = 4
WAITING_REMOVE_MANAGER = 5
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

def is_valid_date(date_str):
    """Проверяет формат ГГГГ-ММ-ДД и существование даты."""
    try:
        datetime.datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except ValueError:
        return False

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

# ---------- ПОЛУЧЕНИЕ ДАННЫХ ИЗ OZON ----------
def fetch_postings(date_from, date_to):
    """Загружает все отгрузки FBO за указанный период."""
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
    """
    Агрегирует отгрузки по дням.
    Если заданы date_from/date_to, фильтрует отгрузки по дате создания.
    Возвращает словарь {дата: метрики}.
    """
    aggregated = {}
    for posting in postings:
        created_at = posting.get("created_at", "")
        if not created_at:
            continue
        date_str = created_at[:10]
        # Если задан фильтр по датам – проверяем
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
        # остальные статусы остаются только в "заказано"

    return aggregated

def get_metrics_for_date(date_str):
    """
    Возвращает метрики за конкретный день (суммарно за этот день).
    """
    # Загружаем данные за месяц, чтобы охватить один день
    today = datetime.date.today()
    first_day = today.replace(day=1)
    date_from = first_day.strftime("%Y-%m-%d")
    date_to = today.strftime("%Y-%m-%d")
    postings = fetch_postings(date_from, date_to)
    agg = aggregate_postings(postings, date_from=date_str, date_to=date_str)
    return agg.get(date_str, {})

def get_metrics_for_period(date_from, date_to):
    """
    Возвращает суммарные метрики за период (с date_from по date_to включительно).
    """
    postings = fetch_postings(date_from, date_to)
    agg = aggregate_postings(postings, date_from=date_from, date_to=date_to)
    # Суммируем по всем дням
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
    """Возвращает три словаря: сегодня, вчера, текущий месяц."""
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

    # Суммируем за месяц
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

# ---------- ФОРМАТИРОВАНИЕ СООБЩЕНИЙ ----------
def format_metrics(metrics, title):
    """Форматирует словарь метрик в читаемое сообщение."""
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

# ---------- ОБРАБОТЧИК ГЛАВНОГО МЕНЮ (ОБЩИЙ) ----------
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

    # Если пришло что-то другое – игнорируем или напоминаем
    await update.message.reply_text("Используйте кнопки меню.")

# ---------- ОБРАБОТЧИКИ ПОДМЕНЮ "ОТЧЁТ" ----------
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
        await update.message.reply_text(
            "Введите дату в формате ГГГГ-ММ-ДД (например, 2026-09-01):"
        )
        return WAITING_DATE_SINGLE

    if text == "📊 Выбрать период":
        await update.message.reply_text(
            "Введите начальную дату в формате ГГГГ-ММ-ДД:"
        )
        return WAITING_DATE_PERIOD_START

    if text == "🔙 Назад":
        # Возврат в главное меню
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

    # Если неизвестная кнопка – игнорируем
    await update.message.reply_text("Неизвестная команда. Используйте кнопки.")

# ---------- ДИАЛОГ ВЫБОРА ДАТЫ ----------
async def single_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.strip()
    if not is_valid_date(date_str):
        await update.message.reply_text(
            "❌ Неверный формат. Введите дату в формате ГГГГ-ММ-ДД (например, 2026-09-01):"
        )
        return WAITING_DATE_SINGLE

    metrics = get_metrics_for_date(date_str)
    msg = format_metrics(metrics, f"Отчёт за {date_str}")
    await update.message.reply_text(msg, parse_mode="Markdown")
    # Возвращаемся в подменю отчётов
    await update.message.reply_text(
        "Выберите действие:",
        reply_markup=reports_keyboard()
    )
    return ConversationHandler.END

# ---------- ДИАЛОГ ВЫБОРА ПЕРИОДА ----------
async def period_start_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.strip()
    if not is_valid_date(date_str):
        await update.message.reply_text(
            "❌ Неверный формат. Введите начальную дату в формате ГГГГ-ММ-ДД:"
        )
        return WAITING_DATE_PERIOD_START

    context.user_data['period_start'] = date_str
    await update.message.reply_text(
        "Введите конечную дату в формате ГГГГ-ММ-ДД:"
    )
    return WAITING_DATE_PERIOD_END

async def period_end_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    end_date_str = update.message.text.strip()
    if not is_valid_date(end_date_str):
        await update.message.reply_text(
            "❌ Неверный формат. Введите конечную дату в формате ГГГГ-ММ-ДД:"
        )
        return WAITING_DATE_PERIOD_END

    start_date = context.user_data.get('period_start')
    if start_date > end_date_str:
        await update.message.reply_text(
            "❌ Начальная дата не может быть позже конечной. Попробуйте снова."
        )
        # Очищаем и начинаем заново
        context.user_data.pop('period_start', None)
        await update.message.reply_text(
            "Введите начальную дату в формате ГГГГ-ММ-ДД:"
        )
        return WAITING_DATE_PERIOD_START

    metrics = get_metrics_for_period(start_date, end_date_str)
    msg = format_metrics(metrics, f"Отчёт за период {start_date} – {end_date_str}")
    await update.message.reply_text(msg, parse_mode="Markdown")
    await update.message.reply_text(
        "Выберите действие:",
        reply_markup=reports_keyboard()
    )
    # Очищаем данные пользователя
    context.user_data.pop('period_start', None)
    return ConversationHandler.END

# ---------- ОБРАБОТЧИКИ ПОДМЕНЮ "АДМИНИСТРИРОВАНИЕ" ----------
async def handle_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ Только для администратора.")
        return

    text = update.message.text
    if text == "➕ Добавить менеджера":
        await update.message.reply_text(
            "Введите ID пользователя (только цифры):"
        )
        return WAITING_ADD_MANAGER

    if text == "➖ Удалить менеджера":
        await update.message.reply_text(
            "Введите ID пользователя (только цифры):"
        )
        return WAITING_REMOVE_MANAGER

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

    # Если неизвестная кнопка
    await update.message.reply_text("Неизвестная команда. Используйте кнопки.")

# ---------- ДИАЛОГ ДОБАВЛЕНИЯ/УДАЛЕНИЯ МЕНЕДЖЕРА ----------
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

# ---------- ОТМЕНА ДИАЛОГОВ ----------
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

    # Команда /start
    application.add_handler(CommandHandler("start", start))

    # Обработчик главного меню (кнопки "Отчёт", "Администрирование")
    application.add_handler(MessageHandler(
        filters.Text(["📊 Отчёт", "⚙️ Администрирование"]),
        handle_main_menu
    ))

    # Обработчик подменю "Отчёт"
    application.add_handler(MessageHandler(
        filters.Text(["📅 Текущие показатели", "📆 Выбрать дату", "📊 Выбрать период", "🔙 Назад"]),
        handle_reports_menu
    ))

    # Обработчик подменю "Администрирование"
    application.add_handler(MessageHandler(
        filters.Text(["➕ Добавить менеджера", "➖ Удалить менеджера", "📋 Список менеджеров", "🔙 Назад"]),
        handle_admin_menu
    ))

    # ConversationHandler для диалога выбора даты
    conv_date = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📆 Выбрать дату"), handle_reports_menu)],
        states={
            WAITING_DATE_SINGLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, single_date_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # ConversationHandler для диалога выбора периода
    conv_period = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📊 Выбрать период"), handle_reports_menu)],
        states={
            WAITING_DATE_PERIOD_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, period_start_input)],
            WAITING_DATE_PERIOD_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, period_end_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # ConversationHandler для добавления менеджера
    conv_add_manager = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("➕ Добавить менеджера"), handle_admin_menu)],
        states={
            WAITING_ADD_MANAGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_manager_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # ConversationHandler для удаления менеджера
    conv_remove_manager = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("➖ Удалить менеджера"), handle_admin_menu)],
        states={
            WAITING_REMOVE_MANAGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_manager_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_date)
    application.add_handler(conv_period)
    application.add_handler(conv_add_manager)
    application.add_handler(conv_remove_manager)

    # Планировщик автоматических отчётов
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
