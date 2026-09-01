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

OZON_ANALYTICS_URL = "https://api-seller.ozon.ru/v1/analytics/data"
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

# ---------- ЗАПРОС К АНАЛИТИКЕ (КОЛИЧЕСТВО) ----------
def get_ozon_analytics(date_from, date_to, metric_names):
    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": metric_names,
        "dimension": ["day"],
        "filters": [],
        "sort": [],
        "limit": 1000,
    }
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            response = requests.post(OZON_ANALYTICS_URL, headers=headers, json=payload, timeout=15)
            if response.status_code == 429:
                wait = 10 * (attempt + 1)
                write_log(f"⚠️ 429, повтор через {wait} сек (попытка {attempt+1}/{max_attempts})")
                time.sleep(wait)
                continue
            response.raise_for_status()
            data = response.json()
            if "result" in data and "data" in data["result"]:
                return data["result"]["data"]
            else:
                write_log("⚠️ Неожиданный формат аналитики")
                return None
        except Exception as e:
            write_log(f"❌ Ошибка аналитики: {e}")
            if attempt == max_attempts - 1:
                return None
            time.sleep(5)
    return None

# ---------- ЗАПРОС К СПИСКУ ОТГРУЗОК FBO (СУММЫ) ----------
def get_ozon_postings(date_from, date_to):
    """Возвращает список отгрузок FBO за период."""
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
            if postings and not all_postings:
                # Логируем структуру первой отгрузки для отладки
                write_log(f"🔍 Пример отгрузки (первая): {json.dumps(postings[0], indent=2, ensure_ascii=False)[:1500]}")
            all_postings.extend(postings)
            if len(postings) < payload["limit"]:
                break
            payload["offset"] += payload["limit"]
        except Exception as e:
            write_log(f"❌ Ошибка получения отгрузок FBO: {e}")
            break
    write_log(f"📦 Всего получено отгрузок: {len(all_postings)}")
    return all_postings

def extract_metrics_from_day_data(day_data, metric_names):
    if not day_data or "metrics" not in day_data:
        return {name: 0 for name in metric_names}
    values = day_data["metrics"]
    while len(values) < len(metric_names):
        values.append(0)
    return dict(zip(metric_names, values))

def get_quantities(date_from, date_to):
    metric_names = ["ordered_units", "delivered_units", "canceled_units"]
    rows = get_ozon_analytics(date_from, date_to, metric_names)
    if rows is None:
        return {}
    result = {}
    for row in rows:
        if "dimensions" in row and len(row["dimensions"]) > 0:
            date_str = row["dimensions"][0].get("id", "")
            if date_str:
                result[date_str] = extract_metrics_from_day_data(row, metric_names)
    return result

def get_amounts(date_from, date_to):
    """
    Извлекает суммы из отгрузок FBO.
    Пытается найти сумму в полях:
      - total_price (общая сумма)
      - если нет, суммирует products[].price
    """
    postings = get_ozon_postings(date_from, date_to)
    amounts = {}
    for posting in postings:
        created_at = posting.get("created_at", "")
        if not created_at:
            continue
        date_str = created_at[:10]
        if date_str not in amounts:
            amounts[date_str] = {"ordered_sum": 0, "delivered_sum": 0, "canceled_sum": 0}

        # Ищем сумму
        total = 0
        if "total_price" in posting and posting["total_price"] is not None:
            total = float(posting["total_price"])
        else:
            # Если total_price нет, суммируем цены товаров
            products = posting.get("products", [])
            for product in products:
                price = product.get("price", 0)
                quantity = product.get("quantity", 1)
                total += float(price) * int(quantity)

        status = posting.get("status", "")
        if status in ["cancelled", "canceled"]:
            amounts[date_str]["canceled_sum"] += total
        elif status in ["delivered", "completed"]:
            amounts[date_str]["delivered_sum"] += total
        else:
            amounts[date_str]["ordered_sum"] += total

    return amounts

def get_full_report():
    today = datetime.date.today()
    first_day = today.replace(day=1)
    date_from = first_day.strftime("%Y-%m-%d")
    date_to = today.strftime("%Y-%m-%d")

    quantities = get_quantities(date_from, date_to)
    amounts = get_amounts(date_from, date_to)

    all_dates = set(quantities.keys()) | set(amounts.keys())
    full_data = {}
    for d in all_dates:
        full_data[d] = {
            "ordered_units": quantities.get(d, {}).get("ordered_units", 0),
            "delivered_units": quantities.get(d, {}).get("delivered_units", 0),
            "canceled_units": quantities.get(d, {}).get("canceled_units", 0),
            "ordered_sum": amounts.get(d, {}).get("ordered_sum", 0),
            "delivered_sum": amounts.get(d, {}).get("delivered_sum", 0),
            "canceled_sum": amounts.get(d, {}).get("canceled_sum", 0),
        }

    today_str = today.strftime("%Y-%m-%d")
    yesterday_str = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    today_data = full_data.get(today_str, {})
    yesterday_data = full_data.get(yesterday_str, {})

    month_data = {"ordered_units": 0, "delivered_units": 0, "canceled_units": 0,
                  "ordered_sum": 0, "delivered_sum": 0, "canceled_sum": 0}
    for d, vals in full_data.items():
        for key in month_data:
            month_data[key] += vals.get(key, 0)

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
