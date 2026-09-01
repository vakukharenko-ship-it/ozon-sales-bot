import asyncio
import datetime
import json
import os
import sys
import time
import requests
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler

# ---------------------- ПРОВЕРКА ПЕРЕМЕННЫХ ----------------------
def check_env():
    env_vars = {
        "OZON_CLIENT_ID": os.getenv("OZON_CLIENT_ID"),
        "OZON_API_KEY": os.getenv("OZON_API_KEY"),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
        "ADMIN_CHAT_ID": os.getenv("ADMIN_CHAT_ID"),
    }
    print("🔍 Проверка переменных окружения:", flush=True)
    for name, value in env_vars.items():
        if value:
            print(f"   ✅ {name} задан (длина: {len(value)})", flush=True)
        else:
            print(f"   ❌ {name} НЕ ЗАДАН!", flush=True)
    return all(env_vars.values())

# ---------------------- КЛЮЧИ ----------------------
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
# ---------------------------------------------------

OZON_POSTING_FBO_URL = "https://api-seller.ozon.ru/v2/posting/fbo/list"
MANAGERS_FILE = "managers.json"
LOG_FILE = "/app/data/ozon_log.txt"

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

# ---------- ЗАПИСЬ В ЛОГ-ФАЙЛ ----------
def write_log(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"[{timestamp}] {message}"
    print(full_msg, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(full_msg + "\n")
    except Exception as e:
        print(f"⚠️ Не удалось записать в файл: {e}", flush=True)

# ---------- ПОЛУЧЕНИЕ ОТГРУЗОК FBO ----------
def get_all_postings(date_from, date_to):
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
                write_log("⚠️ 429 Too Many Requests, ждём 10 сек")
                time.sleep(10)
                continue
            response.raise_for_status()
            data = response.json()
            postings = data.get("result", [])
            if postings and not all_postings:
                write_log(f"🔍 Пример отгрузки: {json.dumps(postings[0], indent=2, ensure_ascii=False)[:800]}")
            all_postings.extend(postings)
            if len(postings) < payload["limit"]:
                break
            payload["offset"] += payload["limit"]
        except Exception as e:
            write_log(f"❌ Ошибка получения отгрузок: {e}")
            break
    write_log(f"📦 Всего получено отгрузок: {len(all_postings)}")
    return all_postings

def extract_posting_metrics(posting):
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
    status = posting.get("status", "")
    created_at = posting.get("created_at", "")
    if created_at:
        date_str = created_at[:10]
    else:
        date_str = None
    return {
        "date": date_str,
        "status": status,
        "units": total_units,
        "sum": total_sum
    }

def aggregate_postings(postings):
    """
    Агрегирует отгрузки по дням.
    "Заказано" = сумма всех отгрузок (независимо от статуса).
    "Доставлено" = только со статусами delivered/completed.
    "Отмены" = только со статусами cancelled/canceled.
    """
    aggregated = {}
    status_stats = {}  # для отладки: сколько штук в каждом статусе
    total_units_all = 0
    for posting in postings:
        metrics = extract_posting_metrics(posting)
        date_str = metrics["date"]
        if not date_str:
            continue
        if date_str not in aggregated:
            aggregated[date_str] = {
                "ordered_units": 0,
                "ordered_sum": 0.0,
                "delivered_units": 0,
                "delivered_sum": 0.0,
                "canceled_units": 0,
                "canceled_sum": 0.0,
            }
        # Все отгрузки добавляем в "заказано"
        aggregated[date_str]["ordered_units"] += metrics["units"]
        aggregated[date_str]["ordered_sum"] += metrics["sum"]

        status = metrics["status"]
        units = metrics["units"]
        sum_val = metrics["sum"]
        total_units_all += units
        # Собираем статистику по статусам
        status_stats[status] = status_stats.get(status, 0) + units

        # Отдельно добавляем в доставленные или отменённые
        if status in ["cancelled", "canceled"]:
            aggregated[date_str]["canceled_units"] += units
            aggregated[date_str]["canceled_sum"] += sum_val
        elif status in ["delivered", "completed"]:
            aggregated[date_str]["delivered_units"] += units
            aggregated[date_str]["delivered_sum"] += sum_val
        # Остальные статусы не добавляются в доставленные/отмены, но уже учтены в "заказано"

    # Логируем статистику по статусам
    write_log(f"📊 Статистика по статусам (штук): {status_stats}")
    write_log(f"📊 Всего штук во всех отгрузках: {total_units_all}")
    return aggregated

def get_full_report():
    today = datetime.date.today()
    first_day = today.replace(day=1)
    date_from = first_day.strftime("%Y-%m-%d")
    date_to = today.strftime("%Y-%m-%d")
    write_log(f"📅 Запрос за период: {date_from} – {date_to}")

    postings = get_all_postings(date_from, date_to)
    agg = aggregate_postings(postings)

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
    for date, vals in agg.items():
        for key in month_data:
            month_data[key] += vals.get(key, 0)

    write_log(f"📊 За месяц: ordered_units={month_data['ordered_units']}, delivered_units={month_data['delivered_units']}, canceled_units={month_data['canceled_units']}")

    return today_data, yesterday_data, month_data

def format_sales_message(period_name, metrics_dict):
    if not metrics_dict or all(v == 0 for v in metrics_dict.values()):
        return f"❌ Нет данных за {period_name}."
    return (
        f"📊 *{period_name}*\n\n"
        f"🛒 *Заказано*\n  На сумму: {metrics_dict.get('ordered_sum', 0):,.2f} ₽\n"
        f"  Штук: {metrics_dict.get('ordered_units', 0)}\n\n"
        f"📦 *Доставлено*\n  На сумму: {metrics_dict.get('delivered_sum', 0):,.2f} ₽\n"
        f"  Штук: {metrics_dict.get('delivered_units', 0)}\n\n"
        f"❌ *Отмены*\n  На сумму: {metrics_dict.get('canceled_sum', 0):,.2f} ₽\n"
        f"  Штук: {metrics_dict.get('canceled_units', 0)}"
    )

# ---------- РАССЫЛКА ПО РАСПИСАНИЮ ----------
async def send_scheduled_report(context):
    moscow_tz = datetime.timezone(datetime.timedelta(hours=3))
    now = datetime.datetime.now(moscow_tz)
    if not (9 <= now.hour <= 23):
        return
    managers = load_managers()
    if not managers:
        return

    today_m, yesterday_m, month_m = get_full_report()
    msg_today = format_sales_message("Сегодня", today_m)
    msg_yesterday = format_sales_message("Вчера", yesterday_m)
    msg_month = format_sales_message("Текущий месяц", month_m)

    for chat_id in managers:
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg_today, parse_mode="Markdown")
            await context.bot.send_message(chat_id=chat_id, text=msg_yesterday, parse_mode="Markdown")
            await context.bot.send_message(chat_id=chat_id, text=msg_month, parse_mode="Markdown")
        except Exception as e:
            write_log(f"❌ Ошибка отправки для {chat_id}: {e}")

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
    write_log(f"👤 Команда /start от {chat_id}")
    if chat_id == ADMIN_CHAT_ID:
        await update.message.reply_text("👋 Администратор!", reply_markup=get_admin_keyboard())
    elif is_manager(chat_id):
        await update.message.reply_text("👋 Менеджер!", reply_markup=get_user_keyboard())
    else:
        await update.message.reply_text("Доступ запрещён.", reply_markup=ReplyKeyboardMarkup([], resize_keyboard=True))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text
    write_log(f"📩 Получено сообщение от {chat_id}: '{text}'")

    if text == "📊 Отчёт":
        write_log(f"📊 Пользователь {chat_id} запросил отчёт")
        if not is_manager(chat_id):
            await update.message.reply_text("⛔ Нет доступа.")
            return
        today_m, yesterday_m, month_m = get_full_report()
        msg_today = format_sales_message("Сегодня", today_m)
        msg_yesterday = format_sales_message("Вчера", yesterday_m)
        msg_month = format_sales_message("Текущий месяц", month_m)
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
    if not check_env():
        write_log("❌ ОШИБКА: Не все переменные окружения установлены! Бот не запустится.")
        return

    if not all([OZON_CLIENT_ID, OZON_API_KEY, TELEGRAM_BOT_TOKEN]) or ADMIN_CHAT_ID == 0:
        write_log("❌ ОШИБКА: Одна из переменных пуста или ADMIN_CHAT_ID = 0!")
        return

    write_log("🚀 Бот запускается...")

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

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(send_scheduled_report, interval=60*60, first=0)
        write_log("✅ Планировщик запущен.")
    else:
        write_log("⚠️ JobQueue не доступен!")

    write_log("🚀 Бот готов к работе.")
    application.run_polling(allowed_updates=Update.ALL_TYPES, timeout=30)

if __name__ == "__main__":
    main()
