import asyncio
import datetime
import json
import os
import time
import requests
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler

# ==================== КОНФИГУРАЦИЯ ====================
# Ключи берутся из переменных окружения (настраиваются в BotHost)
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

# Адреса API Ozon
OZON_POSTING_FBO_URL = "https://api-seller.ozon.ru/v2/posting/fbo/list"

# Файлы для хранения данных
MANAGERS_FILE = "managers.json"
LOG_FILE = "/app/data/ozon_log.txt"   # лог-файл внутри контейнера

# Состояния для диалогов
WAITING_FOR_ADD_ID = 1
WAITING_FOR_REMOVE_ID = 2
# =====================================================

# ---------- РАБОТА СО СПИСКОМ МЕНЕДЖЕРОВ ----------
def load_managers():
    """Загружает список chat_id менеджеров из файла."""
    if os.path.exists(MANAGERS_FILE):
        with open(MANAGERS_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except:
                return []
    return []

def save_managers(managers):
    """Сохраняет список chat_id менеджеров в файл."""
    with open(MANAGERS_FILE, "w", encoding="utf-8") as f:
        json.dump(managers, f, ensure_ascii=False, indent=2)

def is_manager(chat_id):
    """Проверяет, есть ли пользователь в списке менеджеров."""
    return chat_id in load_managers()

def add_manager(chat_id):
    """Добавляет пользователя в список менеджеров, если его там нет."""
    managers = load_managers()
    if chat_id not in managers:
        managers.append(chat_id)
        save_managers(managers)
        return True
    return False

def remove_manager(chat_id):
    """Удаляет пользователя из списка менеджеров."""
    managers = load_managers()
    if chat_id in managers:
        managers.remove(chat_id)
        save_managers(managers)
        return True
    return False

# ---------- ЛОГИРОВАНИЕ ----------
def write_log(message):
    """Записывает сообщение в лог-файл и в консоль."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"[{timestamp}] {message}"
    print(full_msg, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(full_msg + "\n")
    except:
        pass  # если не удалось записать, просто игнорируем

# ---------- ПОЛУЧЕНИЕ ОТГРУЗОК FBO ----------
def fetch_postings(date_from, date_to):
    """
    Получает все отгрузки FBO за указанный период.
    Возвращает список отгрузок (словарей).
    """
    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "date_from": date_from,
        "date_to": date_to,
        "status": "",               # все статусы
        "limit": 1000,
        "offset": 0,
    }
    all_postings = []
    while True:
        try:
            response = requests.post(OZON_POSTING_FBO_URL, headers=headers, json=payload, timeout=15)
            if response.status_code == 429:
                write_log("⚠️ Превышен лимит запросов, пауза 10 сек")
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
    write_log(f"📦 Загружено отгрузок: {len(all_postings)}")
    return all_postings

def aggregate_postings(postings):
    """
    Агрегирует отгрузки по дням.
    Возвращает словарь: {дата: {ordered_units, ordered_sum, delivered_units, delivered_sum, canceled_units, canceled_sum}}
    """
    aggregated = {}
    for posting in postings:
        # Извлекаем дату создания
        created_at = posting.get("created_at", "")
        if not created_at:
            continue
        date_str = created_at[:10]  # YYYY-MM-DD

        # Считаем общее количество товаров и сумму
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

        # Инициализируем запись для даты, если её нет
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
        aggregated[date_str]["ordered_units"] += total_units
        aggregated[date_str]["ordered_sum"] += total_sum

        # По статусу распределяем в доставленные или отменённые
        status = posting.get("status", "")
        if status in ("cancelled", "canceled"):
            aggregated[date_str]["canceled_units"] += total_units
            aggregated[date_str]["canceled_sum"] += total_sum
        elif status in ("delivered", "completed"):
            aggregated[date_str]["delivered_units"] += total_units
            aggregated[date_str]["delivered_sum"] += total_sum
        # остальные статусы остаются только в "заказано"

    return aggregated

def get_metrics():
    """
    Возвращает три словаря с метриками: за сегодня, за вчера, за текущий месяц.
    """
    today = datetime.date.today()
    first_day = today.replace(day=1)
    date_from = first_day.strftime("%Y-%m-%d")
    date_to = today.strftime("%Y-%m-%d")

    postings = fetch_postings(date_from, date_to)
    agg = aggregate_postings(postings)

    today_str = today.strftime("%Y-%m-%d")
    yesterday_str = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    today_data = agg.get(today_str, {})
    yesterday_data = agg.get(yesterday_str, {})

    # Суммируем за месяц (все даты в agg уже лежат в пределах месяца)
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
def format_sales_message(period_name, metrics):
    """Форматирует метрики в читаемое сообщение."""
    if not metrics or all(v == 0 for v in metrics.values()):
        return f"❌ Нет данных за {period_name}."
    return (
        f"📊 *{period_name}*\n\n"
        f"🛒 *Заказано*\n  На сумму: {metrics.get('ordered_sum', 0):,.2f} ₽\n"
        f"  Штук: {metrics.get('ordered_units', 0)}\n\n"
        f"📦 *Доставлено*\n  На сумму: {metrics.get('delivered_sum', 0):,.2f} ₽\n"
        f"  Штук: {metrics.get('delivered_units', 0)}\n\n"
        f"❌ *Отмены*\n  На сумму: {metrics.get('canceled_sum', 0):,.2f} ₽\n"
        f"  Штук: {metrics.get('canceled_units', 0)}"
    )

# ---------- РАССЫЛКА ПО РАСПИСАНИЮ ----------
async def scheduled_report(context):
    """Отправляет отчёт всем менеджерам каждый час."""
    # Проверяем, что сейчас в интервале 9–23 по Москве
    moscow_tz = datetime.timezone(datetime.timedelta(hours=3))
    now = datetime.datetime.now(moscow_tz)
    if not (9 <= now.hour <= 23):
        return

    managers = load_managers()
    if not managers:
        return

    today_metrics, yesterday_metrics, month_metrics = get_metrics()
    msg_today = format_sales_message("Сегодня", today_metrics)
    msg_yesterday = format_sales_message("Вчера", yesterday_metrics)
    msg_month = format_sales_message("Текущий месяц", month_metrics)

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

# ---------- ОБРАБОТЧИКИ КОМАНД ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветствие и показ клавиатуры в зависимости от роли."""
    chat_id = update.effective_chat.id
    if chat_id == ADMIN_CHAT_ID:
        await update.message.reply_text("👋 Администратор!", reply_markup=get_admin_keyboard())
    elif is_manager(chat_id):
        await update.message.reply_text("👋 Менеджер!", reply_markup=get_user_keyboard())
    else:
        await update.message.reply_text("Доступ запрещён.", reply_markup=ReplyKeyboardMarkup([], resize_keyboard=True))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на кнопки и текстовые сообщения."""
    chat_id = update.effective_chat.id
    text = update.message.text

    # ---- Отчёт ----
    if text == "📊 Отчёт":
        if not is_manager(chat_id):
            await update.message.reply_text("⛔ Нет доступа.")
            return
        today_metrics, yesterday_metrics, month_metrics = get_metrics()
        await update.message.reply_text(
            format_sales_message("Сегодня", today_metrics),
            parse_mode="Markdown"
        )
        await update.message.reply_text(
            format_sales_message("Вчера", yesterday_metrics),
            parse_mode="Markdown"
        )
        await update.message.reply_text(
            format_sales_message("Текущий месяц", month_metrics),
            parse_mode="Markdown"
        )
        return

    # ---- Количество менеджеров (только для админа) ----
    if text == "👥 Менеджеры (кол-во)":
        if chat_id != ADMIN_CHAT_ID:
            await update.message.reply_text("⛔ Только для администратора.")
            return
        managers = load_managers()
        await update.message.reply_text(f"📊 Менеджеров: {len(managers)}")
        return

    # ---- Админские функции ----
    if chat_id != ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ Только для администратора.")
        return

    if text == "➕ Добавить менеджера":
        await update.message.reply_text("Введите ID пользователя (только цифры):")
        return WAITING_FOR_ADD_ID

    if text == "➖ Удалить менеджера":
        await update.message.reply_text("Введите ID пользователя (только цифры):")
        return WAITING_FOR_REMOVE_ID

    if text == "📋 Список менеджеров":
        managers = load_managers()
        if not managers:
            await update.message.reply_text("Список пуст.")
        else:
            await update.message.reply_text("📋 " + "\n".join(str(m) for m in managers))
        return

    await update.message.reply_text("Неизвестная команда. Используйте кнопки.")

# ---------- ОБРАБОТЧИКИ ВВОДА ID ДЛЯ ДОБАВЛЕНИЯ/УДАЛЕНИЯ ----------
async def add_manager_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Ошибка: ID должен быть числом. Попробуйте снова.")
        return WAITING_FOR_ADD_ID

    if add_manager(user_id):
        await update.message.reply_text(f"✅ Менеджер с ID {user_id} добавлен.")
    else:
        await update.message.reply_text(f"⚠️ Менеджер с ID {user_id} уже существует.")
    await update.message.reply_text("Готово.", reply_markup=get_admin_keyboard())
    return ConversationHandler.END

async def remove_manager_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Ошибка: ID должен быть числом. Попробуйте снова.")
        return WAITING_FOR_REMOVE_ID

    if remove_manager(user_id):
        await update.message.reply_text(f"✅ Менеджер с ID {user_id} удалён.")
    else:
        await update.message.reply_text(f"❌ Менеджер с ID {user_id} не найден.")
    await update.message.reply_text("Готово.", reply_markup=get_admin_keyboard())
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена диалога."""
    await update.message.reply_text("Действие отменено.", reply_markup=get_admin_keyboard())
    return ConversationHandler.END

# ---------- ЗАПУСК ----------
def main():
    # Проверяем наличие всех переменных окружения
    if not all([OZON_CLIENT_ID, OZON_API_KEY, TELEGRAM_BOT_TOKEN]) or ADMIN_CHAT_ID == 0:
        write_log("❌ ОШИБКА: Не все переменные окружения установлены!")
        return

    write_log("🚀 Запуск бота...")

    # Создаём приложение с увеличенными таймаутами
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .build()
    )

    # Регистрируем команду /start
    application.add_handler(CommandHandler("start", start))

    # Диалог для добавления/удаления менеджеров (с кнопками)
    conv_handler = ConversationHandler(
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
    application.add_handler(conv_handler)

    # Обработчик всех остальных текстовых сообщений (кнопки)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Настраиваем планировщик автоматических отчётов (каждый час)
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
