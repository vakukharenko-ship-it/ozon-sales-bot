import asyncio
import datetime
import json
import os
import requests
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler

# ---------------------- БЕЗОПАСНОЕ ПОЛУЧЕНИЕ КЛЮЧЕЙ ----------------------
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
# -------------------------------------------------------------------------

OZON_ANALYTICS_URL = "https://api-seller.ozon.ru/v1/analytics/data"
MANAGERS_FILE = "managers.json"

WAITING_FOR_ADD_ID = 1
WAITING_FOR_REMOVE_ID = 2

# ---------- РАБОТА СО СПИСКОМ МЕНЕДЖЕРОВ ----------
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
# ------------------------------------------------

def get_ozon_analytics(date_from, date_to):
    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": [
            "ordered_units",
            "ordered_sum",
            "delivered_units",
            "delivered_sum",
            "canceled_units",
            "canceled_sum",
        ],
        "dimension": ["day"],
        "filters": [],
        "sort": [],
        "limit": 1000,
    }
    try:
        response = requests.post(OZON_ANALYTICS_URL, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and "result" in data:
            return data
        else:
            return None
    except:
        return None

def format_sales_message(period_name, analytics_data):
    if not isinstance(analytics_data, dict) or "result" not in analytics_data:
        return f"❌ Нет данных за {period_name}."
    result = analytics_data["result"]
    if not isinstance(result, list):
        return f"❌ Нет данных за {period_name}."
    total_ordered_units = 0
    total_ordered_sum = 0
    total_delivered_units = 0
    total_delivered_sum = 0
    total_canceled_units = 0
    total_canceled_sum = 0
    for row in result:
        if not isinstance(row, dict):
            continue
        total_ordered_units += row.get("ordered_units", 0)
        total_ordered_sum += row.get("ordered_sum", 0)
        total_delivered_units += row.get("delivered_units", 0)
        total_delivered_sum += row.get("delivered_sum", 0)
        total_canceled_units += row.get("canceled_units", 0)
        total_canceled_sum += row.get("canceled_sum", 0)
    message = (
        f"📊 *{period_name}*\n\n"
        f"🛒 *Заказано*\n  На сумму: {total_ordered_sum:,.2f} ₽\n  Штук: {total_ordered_units}\n\n"
        f"📦 *Доставлено*\n  На сумму: {total_delivered_sum:,.2f} ₽\n  Штук: {total_delivered_units}\n\n"
        f"❌ *Отмены*\n  На сумму: {total_canceled_sum:,.2f} ₽\n  Штук: {total_canceled_units}"
    )
    return message

async def send_scheduled_report(context):
    moscow_tz = datetime.timezone(datetime.timedelta(hours=3))
    now = datetime.datetime.now(moscow_tz)
    if not (9 <= now.hour <= 23):
        return
    managers = load_managers()
    if not managers:
        return
    today = datetime.date.today()
    today_str = today.strftime("%Y-%m-%d")
    yesterday = today - datetime.timedelta(days=1)
    yesterday_str = yesterday.strftime("%Y-%m-%d")
    first_day_of_month = today.replace(day=1)
    month_start_str = first_day_of_month.strftime("%Y-%m-%d")
    data_today = get_ozon_analytics(today_str, today_str)
    data_yesterday = get_ozon_analytics(yesterday_str, yesterday_str)
    data_month = get_ozon_analytics(month_start_str, today_str)
    msg_today = format_sales_message("Сегодня", data_today)
    msg_yesterday = format_sales_message("Вчера", data_yesterday)
    msg_month = format_sales_message("Текущий месяц", data_month)
    for chat_id in managers:
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg_today, parse_mode="Markdown")
            await context.bot.send_message(chat_id=chat_id, text=msg_yesterday, parse_mode="Markdown")
            await context.bot.send_message(chat_id=chat_id, text=msg_month, parse_mode="Markdown")
        except Exception as e:
            print(f"Ошибка отправки для {chat_id}: {e}")

# ---------- КЛАВИАТУРЫ ----------
def get_admin_keyboard():
    buttons = [
        [KeyboardButton("📊 Отчёт"), KeyboardButton("👥 Менеджеры (кол-во)")],
        [KeyboardButton("➕ Добавить менеджера"), KeyboardButton("➖ Удалить менеджера")],
        [KeyboardButton("📋 Список менеджеров")]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def get_user_keyboard():
    buttons = [[KeyboardButton("📊 Отчёт")]]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

# ---------- ОБРАБОТЧИКИ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id == ADMIN_CHAT_ID:
        await update.message.reply_text("👋 Администратор!", reply_markup=get_admin_keyboard())
    elif is_manager(chat_id):
        await update.message.reply_text("👋 Менеджер!", reply_markup=get_user_keyboard())
    else:
        await update.message.reply_text("Доступ запрещён.", reply_markup=ReplyKeyboardMarkup([], resize_keyboard=True))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if text == "📊 Отчёт":
        if not is_manager(chat_id):
            await update.message.reply_text("⛔ Нет доступа.")
            return
        today = datetime.date.today()
        today_str = today.strftime("%Y-%m-%d")
        yesterday = today - datetime.timedelta(days=1)
        yesterday_str = yesterday.strftime("%Y-%m-%d")
        first_day_of_month = today.replace(day=1)
        month_start_str = first_day_of_month.strftime("%Y-%m-%d")
        data_today = get_ozon_analytics(today_str, today_str)
        data_yesterday = get_ozon_analytics(yesterday_str, yesterday_str)
        data_month = get_ozon_analytics(month_start_str, today_str)
        msg_today = format_sales_message("Сегодня", data_today)
        msg_yesterday = format_sales_message("Вчера", data_yesterday)
        msg_month = format_sales_message("Текущий месяц", data_month)
        await update.message.reply_text(msg_today, parse_mode="Markdown")
        await update.message.reply_text(msg_yesterday, parse_mode="Markdown")
        await update.message.reply_text(msg_month, parse_mode="Markdown")
        return

    if text == "👥 Менеджеры (кол-во)":
        if chat_id != ADMIN_CHAT_ID:
            await update.message.reply_text("⛔ Только для админа.")
            return
        managers = load_managers()
        await update.message.reply_text(f"📊 Менеджеров: {len(managers)}")
        return

    if chat_id != ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ Только для админа.")
        return

    if text == "➕ Добавить менеджера":
        await update.message.reply_text("Введите ID:")
        return WAITING_FOR_ADD_ID

    if text == "➖ Удалить менеджера":
        await update.message.reply_text("Введите ID:")
        return WAITING_FOR_REMOVE_ID

    if text == "📋 Список менеджеров":
        managers = load_managers()
        if not managers:
            await update.message.reply_text("Список пуст.")
        else:
            await update.message.reply_text("📋 " + "\n".join(str(m) for m in managers))
        return

    await update.message.reply_text("Неизвестно.")

async def add_manager_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Ошибка, введите число.")
        return WAITING_FOR_ADD_ID
    if add_manager(user_id):
        await update.message.reply_text(f"✅ Добавлен {user_id}.")
    else:
        await update.message.reply_text(f"⚠️ Уже есть.")
    await update.message.reply_text("Готово.", reply_markup=get_admin_keyboard())
    return ConversationHandler.END

async def remove_manager_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Ошибка, введите число.")
        return WAITING_FOR_REMOVE_ID
    if remove_manager(user_id):
        await update.message.reply_text(f"✅ Удалён {user_id}.")
    else:
        await update.message.reply_text(f"❌ Не найден.")
    await update.message.reply_text("Готово.", reply_markup=get_admin_keyboard())
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=get_admin_keyboard())
    return ConversationHandler.END

# ---------- ЗАПУСК ----------
def main():
    if not all([OZON_CLIENT_ID, OZON_API_KEY, TELEGRAM_BOT_TOKEN]) or ADMIN_CHAT_ID == 0:
        print("ОШИБКА: Не все переменные установлены!")
        return

    # Создаём приложение с увеличенными таймаутами
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .build()
    )

    application.add_handler(CommandHandler("start", start))

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Text("➕ Добавить менеджера"), handle_message),
            MessageHandler(filters.Text("➖ Удалить менеджера"), handle_message),
        ],
        states={
            WAITING_FOR_ADD_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_manager_input)],
            WAITING_FOR_REMOVE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_manager_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(conv)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # JobQueue
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(send_scheduled_report, interval=60*60, first=0)
        print("Планировщик запущен.")
    else:
        print("JobQueue не доступен!")

    print("Бот запущен.")
    # Запускаем polling с обработкой ошибок
    application.run_polling(allowed_updates=Update.ALL_TYPES, timeout=30)

if __name__ == "__main__":
    main()
