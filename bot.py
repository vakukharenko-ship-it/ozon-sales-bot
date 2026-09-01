import datetime
import json
import os
import time
import re
import calendar
import requests
import warnings
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    filters, ConversationHandler, CallbackQueryHandler
)
from telegram.warnings import PTBUserWarning
warnings.filterwarnings("ignore", category=PTBUserWarning)

# ==================== КОНФИГУРАЦИЯ ====================
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
OZON_PERFORMANCE_CLIENT_ID = os.getenv("OZON_PERFORMANCE_CLIENT_ID")
OZON_PERFORMANCE_CLIENT_SECRET = os.getenv("OZON_PERFORMANCE_CLIENT_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = 6134182006  # ID администратора

OZON_POSTING_FBO_URL = "https://api-seller.ozon.ru/v2/posting/fbo/list"
MANAGERS_FILE = "managers.json"
LOG_FILE = "/app/data/ozon_log.txt"

# Состояния
WAITING_DATE_SINGLE = 1
WAITING_PERIOD_TYPE = 2
WAITING_PERIOD_START = 3
WAITING_PERIOD_END = 4
WAITING_ADD_MANAGER = 5
WAITING_REMOVE_MANAGER = 6
WAITING_MANAGER_PHONE = 7
WAITING_PERIOD_YEAR = 8
WAITING_PERIOD_MONTH = 9
WAITING_PERIOD_QUARTER = 10
WAITING_YEAR_SELECT = 11
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
    managers = load_managers()
    return any(m.get("id") == chat_id for m in managers)

def get_manager_info(chat_id):
    managers = load_managers()
    for m in managers:
        if m.get("id") == chat_id:
            return m
    return None

def add_manager(chat_id, username=None, first_name=None, last_name=None, phone=None):
    managers = load_managers()
    for m in managers:
        if m.get("id") == chat_id:
            return False
    managers.append({
        "id": chat_id,
        "username": username or "",
        "first_name": first_name or "",
        "last_name": last_name or "",
        "phone": phone or ""
    })
    save_managers(managers)
    return True

def remove_manager(chat_id):
    managers = load_managers()
    new_managers = [m for m in managers if m.get("id") != chat_id]
    if len(new_managers) == len(managers):
        return False
    save_managers(new_managers)
    return True

def is_admin(chat_id):
    return chat_id == ADMIN_CHAT_ID

def has_access(chat_id):
    return is_admin(chat_id) or is_manager(chat_id)

def get_greeting(name):
    """Возвращает приветствие в зависимости от времени суток (МСК)."""
    moscow_tz = datetime.timezone(datetime.timedelta(hours=3))
    now = datetime.datetime.now(moscow_tz)
    hour = now.hour
    if 5 <= hour < 12:
        part = "Доброе утро"
    elif 12 <= hour < 18:
        part = "Добрый день"
    elif 18 <= hour < 24:
        part = "Добрый вечер"
    else:
        part = "Доброй ночи"
    if name:
        return f"{part}, {name}!"
    else:
        return f"{part}, уважаемый пользователь!"

def get_moscow_today():
    """Возвращает текущую дату в московском часовом поясе."""
    moscow_tz = datetime.timezone(datetime.timedelta(hours=3))
    return datetime.datetime.now(moscow_tz).date()

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

# ---------- ЗАПРОСЫ К OZON (SELLER API) ----------
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
    write_log(f"📦 Загружено отгрузок: {len(all_postings)} за {date_from}–{date_to}")
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

# ---------- ЗАПРОСЫ К OZON (PERFORMANCE API - РЕКЛАМА) ----------
def get_performance_token():
    if not OZON_PERFORMANCE_CLIENT_ID or not OZON_PERFORMANCE_CLIENT_SECRET:
        write_log("⚠️ OZON_PERFORMANCE_CLIENT_ID или CLIENT_SECRET не заданы!")
        return None

    url = "https://api-performance.ozon.ru/api/client/token"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "client_id": OZON_PERFORMANCE_CLIENT_ID,
        "client_secret": OZON_PERFORMANCE_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        token_data = response.json()
        token = token_data.get("access_token")
        if token:
            write_log("✅ Токен Performance API успешно получен.")
            return token
        else:
            write_log(f"❌ Ошибка получения токена: {token_data}")
            return None
    except Exception as e:
        write_log(f"❌ Ошибка при запросе токена: {e}")
        return None

def fetch_advertising_expense(date_from, date_to):
    token = get_performance_token()
    if not token:
        write_log("⚠️ Не удалось получить токен. Рекламные расходы не будут отображаться.")
        return None

    url = "https://api-performance.ozon.ru/api/client/statistics/expense/json"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    params = {
        "dateFrom": date_from,
        "dateTo": date_to,
    }
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if response.status_code == 429:
            write_log("⚠️ 429 Too Many Requests (Performance API), ждём 10 сек")
            time.sleep(10)
            response = requests.get(url, headers=headers, params=params, timeout=15)

        write_log(f"📥 Статус ответа Performance API: {response.status_code}")
        response.raise_for_status()
        data = response.json()
        write_log(f"📥 Ответ Performance API: {json.dumps(data, ensure_ascii=False)[:500]}")

        total_expense = 0.0
        if isinstance(data, dict) and "rows" in data:
            rows = data["rows"]
            if isinstance(rows, list):
                for item in rows:
                    money_spent_str = item.get("moneySpent")
                    if money_spent_str is not None:
                        try:
                            money_spent = float(money_spent_str.replace(",", "."))
                            total_expense += money_spent
                        except:
                            pass
        elif isinstance(data, list):
            for item in data:
                expense = item.get("expense") or item.get("cost") or 0
                try:
                    total_expense += float(expense)
                except:
                    pass
        elif isinstance(data, dict):
            for key in ["expense", "cost", "total_expense", "total"]:
                if key in data:
                    try:
                        total_expense = float(data[key])
                        break
                    except:
                        pass
        write_log(f"📊 Рекламные расходы за {date_from}–{date_to}: {total_expense:.2f} ₽")
        return total_expense
    except Exception as e:
        write_log(f"❌ Ошибка получения рекламных расходов: {e}")
        return None

# ---------- ОСНОВНЫЕ ФУНКЦИИ ДЛЯ ОТЧЁТОВ ----------
def get_metrics_for_date(date_str):
    today = get_moscow_today()
    start = (today - datetime.timedelta(days=183)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    postings = fetch_postings(start, end)
    agg = aggregate_postings(postings, date_from=date_str, date_to=date_str)
    metrics = agg.get(date_str, {})
    ad_expense = fetch_advertising_expense(date_str, date_str)
    metrics["ad_expense"] = ad_expense if ad_expense is not None else 0.0
    revenue = metrics.get("ordered_sum", 0)
    if revenue > 0 and ad_expense is not None:
        metrics["drr"] = (ad_expense / revenue) * 100
    else:
        metrics["drr"] = None
    return metrics

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
    ad_expense = fetch_advertising_expense(date_from, date_to)
    total["ad_expense"] = ad_expense if ad_expense is not None else 0.0
    revenue = total.get("ordered_sum", 0)
    if revenue > 0 and ad_expense is not None:
        total["drr"] = (ad_expense / revenue) * 100
    else:
        total["drr"] = None
    return total

def get_current_metrics():
    today = get_moscow_today()
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

    ad_expense_today = fetch_advertising_expense(today_str, today_str)
    today_data["ad_expense"] = ad_expense_today if ad_expense_today is not None else 0.0
    revenue_today = today_data.get("ordered_sum", 0)
    if revenue_today > 0 and ad_expense_today is not None:
        today_data["drr"] = (ad_expense_today / revenue_today) * 100
    else:
        today_data["drr"] = None

    ad_expense_yesterday = fetch_advertising_expense(yesterday_str, yesterday_str)
    yesterday_data["ad_expense"] = ad_expense_yesterday if ad_expense_yesterday is not None else 0.0
    revenue_yesterday = yesterday_data.get("ordered_sum", 0)
    if revenue_yesterday > 0 and ad_expense_yesterday is not None:
        yesterday_data["drr"] = (ad_expense_yesterday / revenue_yesterday) * 100
    else:
        yesterday_data["drr"] = None

    ad_expense_month = fetch_advertising_expense(date_from, date_to)
    month_data["ad_expense"] = ad_expense_month if ad_expense_month is not None else 0.0
    revenue_month = month_data.get("ordered_sum", 0)
    if revenue_month > 0 and ad_expense_month is not None:
        month_data["drr"] = (ad_expense_month / revenue_month) * 100
    else:
        month_data["drr"] = None

    return today_data, yesterday_data, month_data

# ---------- ФОРМАТИРОВАНИЕ ----------
def format_metrics(metrics, title):
    if not metrics:
        return f"📊 *{title}*\n\n❌ Нет данных за указанный период."
    
    has_data = False
    for key, val in metrics.items():
        if key in ["drr", "ad_expense"]:
            continue
        if isinstance(val, (int, float)) and val != 0:
            has_data = True
            break
    if not has_data:
        return f"📊 *{title}*\n\n❌ Нет данных за указанный период."

    ad_expense = metrics.get("ad_expense", 0)
    drr = metrics.get("drr")
    drr_text = f"{drr:.2f}%" if drr is not None else "∞"

    return (
        f"📊 *{title}*\n\n"
        f"🛒 *Заказано*\n  На сумму: {metrics.get('ordered_sum', 0):,.2f} ₽\n"
        f"  Штук: {metrics.get('ordered_units', 0)}\n\n"
        f"📦 *Доставлено*\n  На сумму: {metrics.get('delivered_sum', 0):,.2f} ₽\n"
        f"  Штук: {metrics.get('delivered_units', 0)}\n\n"
        f"❌ *Отмены*\n  На сумму: {metrics.get('canceled_sum', 0):,.2f} ₽\n"
        f"  Штук: {metrics.get('canceled_units', 0)}\n\n"
        f"📢 *Реклама*\n  Расходы: {ad_expense:,.2f} ₽\n"
        f"  ДРР: {drr_text}"
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
    user = update.effective_user

    if is_admin(chat_id):
        name = user.first_name if user.first_name else ""
        greeting = get_greeting(name)
        await update.message.reply_text(greeting, reply_markup=main_admin_keyboard())
    elif is_manager(chat_id):
        manager = get_manager_info(chat_id)
        name = manager.get("first_name") if manager and manager.get("first_name") else user.first_name or ""
        greeting = get_greeting(name)
        await update.message.reply_text(greeting, reply_markup=main_user_keyboard())
    else:
        await update.message.reply_text("❌ Нет доступа! Обратитесь к администратору.", reply_markup=ReplyKeyboardRemove())

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if text == "📊 Отчёт":
        if not has_access(chat_id):
            await update.message.reply_text("❌ Нет доступа! Обратитесь к администратору.")
            return
        await update.message.reply_text("Выберите тип отчёта:", reply_markup=reports_keyboard())
        return

    if text == "⚙️ Администрирование":
        if not is_admin(chat_id):
            await update.message.reply_text("⛔ Только для администратора.")
            return
        await update.message.reply_text("Управление менеджерами:", reply_markup=admin_keyboard())
        return

    await update.message.reply_text("Используйте кнопки меню.")

async def handle_reports_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if text == "🔙 Назад":
        if is_admin(chat_id):
            await update.message.reply_text("Главное меню", reply_markup=main_admin_keyboard())
        else:
            await update.message.reply_text("Главное меню", reply_markup=main_user_keyboard())
        return

    if not has_access(chat_id):
        await update.message.reply_text("❌ Нет доступа! Обратитесь к администратору.")
        return

    if text == "📅 Текущие показатели":
        today_m, yesterday_m, month_m = get_current_metrics()
        await update.message.reply_text(format_metrics(today_m, "Сегодня"), parse_mode="Markdown")
        await update.message.reply_text(format_metrics(yesterday_m, "Вчера"), parse_mode="Markdown")
        await update.message.reply_text(format_metrics(month_m, "Текущий месяц"), parse_mode="Markdown")
        return

    if text == "📆 Выбрать дату":
        now = get_moscow_today()
        keyboard = create_calendar(now.year, now.month, "date_")
        await update.message.reply_text("Выберите дату:", reply_markup=keyboard)
        return WAITING_DATE_SINGLE

    if text == "📊 Выбрать период":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗓️ По месяцам", callback_data="period_month")],
            [InlineKeyboardButton("📅 По кварталам", callback_data="period_quarter")],
            [InlineKeyboardButton("📆 По годам", callback_data="period_year")],
            [InlineKeyboardButton("📊 Произвольный период", callback_data="period_custom")],
            [InlineKeyboardButton("❌ Отмена", callback_data="period_cancel")]
        ])
        await update.message.reply_text("Выберите тип периода:", reply_markup=keyboard)
        return WAITING_PERIOD_TYPE

    await update.message.reply_text("Неизвестная команда.")

async def handle_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("⛔ Только для администратора.")
        return

    text = update.message.text
    if text == "📋 Список менеджеров":
        managers = load_managers()
        if not managers:
            await update.message.reply_text("Список менеджеров пуст.")
        else:
            lines = ["📋 Список менеджеров:"]
            for m in managers:
                info = f"ID: {m.get('id')}"
                if m.get('username'):
                    info += f", @{m.get('username')}"
                if m.get('first_name'):
                    info += f", {m.get('first_name')}"
                if m.get('phone'):
                    info += f", 📞 {m.get('phone')}"
                lines.append(info)
            await update.message.reply_text("\n".join(lines))
        return

    if text == "🔙 Назад":
        await update.message.reply_text("Главное меню", reply_markup=main_admin_keyboard())
        return

    await update.message.reply_text("Неизвестная команда.")

# ---------- INLINE CALLBACK ----------
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id

    if not has_access(chat_id):
        await query.edit_message_text("❌ Нет доступа! Обратитесь к администратору.")
        return ConversationHandler.END

    # ---------- КАЛЕНДАРЬ ДЛЯ ВЫБОРА ДАТЫ (префикс "date_") ----------
    if data.startswith("date_"):
        if data == "date_cancel":
            await query.edit_message_text("Выбор даты отменён.")
            await query.message.reply_text("Выберите действие:", reply_markup=reports_keyboard())
            return ConversationHandler.END

        # Проверяем навигацию: prev_month или next_month
        if "prev_month" in data or "next_month" in data:
            # Извлекаем год и месяц с помощью регулярного выражения
            match = re.search(r'prev_month_(\d+)_(\d+)|next_month_(\d+)_(\d+)', data)
            if match:
                if match.group(1) and match.group(2):
                    year = int(match.group(1))
                    month = int(match.group(2))
                    action = "prev_month"
                else:
                    year = int(match.group(3))
                    month = int(match.group(4))
                    action = "next_month"
            else:
                await query.edit_message_text("❌ Ошибка формата навигации.")
                return WAITING_DATE_SINGLE

            # Корректируем месяц
            if action == "prev_month":
                month -= 1
                if month == 0:
                    month = 12
                    year -= 1
            elif action == "next_month":
                month += 1
                if month == 13:
                    month = 1
                    year += 1
            else:
                await query.edit_message_text("❌ Неизвестное действие.")
                return WAITING_DATE_SINGLE

            keyboard = create_calendar(year, month, "date_")
            await query.edit_message_reply_markup(reply_markup=keyboard)
            return WAITING_DATE_SINGLE

        # Если не навигация, пробуем распарсить дату
        date_str = data[5:]  # убираем "date_"
        if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            metrics = get_metrics_for_date(date_str)
            msg = format_metrics(metrics, f"Отчёт за {date_str}")
            await query.edit_message_text(msg, parse_mode="Markdown")
            await query.message.reply_text("Выберите действие:", reply_markup=reports_keyboard())
            return ConversationHandler.END
        else:
            await query.edit_message_text("❌ Ошибка формата даты.")
            return WAITING_DATE_SINGLE

    # ---------- ВЫБОР ПЕРИОДА (общие действия) ----------
    if data == "period_month":
        current_year = get_moscow_today().year
        years = list(range(current_year - 9, current_year + 1))
        buttons = [[InlineKeyboardButton(str(y), callback_data=f"period_year_month_{y}")] for y in years]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="period_cancel")])
        await query.edit_message_text("Выберите год:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PERIOD_YEAR

    if data == "period_quarter":
        current_year = get_moscow_today().year
        years = list(range(current_year - 9, current_year + 1))
        buttons = [[InlineKeyboardButton(str(y), callback_data=f"period_year_quarter_{y}")] for y in years]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="period_cancel")])
        await query.edit_message_text("Выберите год:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PERIOD_YEAR

    if data == "period_year":
        current_year = get_moscow_today().year
        years = list(range(current_year - 9, current_year + 1))
        buttons = [[InlineKeyboardButton(str(y), callback_data=f"period_year_only_{y}")] for y in years]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="period_cancel")])
        await query.edit_message_text("Выберите год:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_YEAR_SELECT

    if data == "period_custom":
        now = get_moscow_today()
        keyboard = create_calendar(now.year, now.month, "start_")
        await query.edit_message_text("Выберите начальную дату:", reply_markup=keyboard)
        return WAITING_PERIOD_START

    if data == "period_cancel":
        await query.edit_message_text("Выбор периода отменён.")
        await query.message.reply_text("Выберите действие:", reply_markup=reports_keyboard())
        return ConversationHandler.END

    # ---------- ВЫБОР ГОДА ДЛЯ МЕСЯЦА ----------
    if data.startswith("period_year_month_"):
        year = int(data.split("_")[-1])
        context.user_data['period_year'] = year
        months = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
        buttons = [[InlineKeyboardButton(name, callback_data=f"period_month_{i}_{year}")] for i, name in enumerate(months, 1)]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="period_cancel")])
        await query.edit_message_text(f"Выберите месяц {year}:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PERIOD_MONTH

    # ---------- ВЫБОР ГОДА ДЛЯ КВАРТАЛА ----------
    if data.startswith("period_year_quarter_"):
        year = int(data.split("_")[-1])
        context.user_data['period_year'] = year
        quarters = ["1 квартал (янв-мар)", "2 квартал (апр-июн)", "3 квартал (июл-сен)", "4 квартал (окт-дек)"]
        buttons = [[InlineKeyboardButton(name, callback_data=f"period_quarter_{i}_{year}")] for i, name in enumerate(quarters, 1)]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="period_cancel")])
        await query.edit_message_text(f"Выберите квартал {year}:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PERIOD_QUARTER

    # ---------- ТОЛЬКО ГОД ----------
    if data.startswith("period_year_only_"):
        year = int(data.split("_")[-1])
        first_day = datetime.date(year, 1, 1)
        last_day = datetime.date(year, 12, 31)
        metrics = get_metrics_for_period(first_day.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d"))
        msg = format_metrics(metrics, f"Год {year}")
        await query.edit_message_text(msg, parse_mode="Markdown")
        await query.message.reply_text("Выберите действие:", reply_markup=reports_keyboard())
        return ConversationHandler.END

    # ---------- МЕСЯЦ ПОСЛЕ ВЫБОРА ГОДА ----------
    if data.startswith("period_month_"):
        parts = data.split("_")
        month_num, year = int(parts[2]), int(parts[3])
        first_day = datetime.date(year, month_num, 1)
        if month_num == 12:
            last_day = datetime.date(year, 12, 31)
        else:
            last_day = datetime.date(year, month_num+1, 1) - datetime.timedelta(days=1)
        date_from, date_to = first_day.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d")
        metrics = get_metrics_for_period(date_from, date_to)
        msg = format_metrics(metrics, f"Месяц {first_day.strftime('%B %Y')}")
        await query.edit_message_text(msg, parse_mode="Markdown")
        await query.message.reply_text("Выберите действие:", reply_markup=reports_keyboard())
        return ConversationHandler.END

    # ---------- КВАРТАЛ ПОСЛЕ ВЫБОРА ГОДА ----------
    if data.startswith("period_quarter_"):
        parts = data.split("_")
        q, year = int(parts[2]), int(parts[3])
        start_month = (q-1)*3 + 1
        end_month = q*3
        first_day = datetime.date(year, start_month, 1)
        if end_month == 12:
            last_day = datetime.date(year, 12, 31)
        else:
            last_day = datetime.date(year, end_month+1, 1) - datetime.timedelta(days=1)
        date_from, date_to = first_day.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d")
        metrics = get_metrics_for_period(date_from, date_to)
        msg = format_metrics(metrics, f"{q} квартал {year}")
        await query.edit_message_text(msg, parse_mode="Markdown")
        await query.message.reply_text("Выберите действие:", reply_markup=reports_keyboard())
        return ConversationHandler.END

    # ---------- ПРОИЗВОЛЬНЫЙ ПЕРИОД – НАЧАЛО (префикс "start_") ----------
    if data.startswith("start_"):
        if data == "start_cancel":
            await query.edit_message_text("Выбор периода отменён.")
            await query.message.reply_text("Выберите действие:", reply_markup=reports_keyboard())
            return ConversationHandler.END

        if "prev_month" in data or "next_month" in data:
            match = re.search(r'prev_month_(\d+)_(\d+)|next_month_(\d+)_(\d+)', data)
            if match:
                if match.group(1) and match.group(2):
                    year = int(match.group(1))
                    month = int(match.group(2))
                    action = "prev_month"
                else:
                    year = int(match.group(3))
                    month = int(match.group(4))
                    action = "next_month"
            else:
                await query.edit_message_text("❌ Ошибка формата навигации.")
                return WAITING_PERIOD_START

            if action == "prev_month":
                month -= 1
                if month == 0:
                    month = 12
                    year -= 1
            elif action == "next_month":
                month += 1
                if month == 13:
                    month = 1
                    year += 1
            else:
                await query.edit_message_text("❌ Неизвестное действие.")
                return WAITING_PERIOD_START

            keyboard = create_calendar(year, month, "start_")
            await query.edit_message_reply_markup(reply_markup=keyboard)
            return WAITING_PERIOD_START

        # Парсим дату начала
        date_str = data[6:]  # убираем "start_"
        if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            context.user_data['period_start_date'] = date_str
            now = get_moscow_today()
            keyboard = create_calendar(now.year, now.month, "end_")
            await query.edit_message_text(f"Начало: {date_str}\nТеперь выберите конечную дату:", reply_markup=keyboard)
            return WAITING_PERIOD_END
        else:
            await query.edit_message_text("❌ Ошибка формата даты.")
            return WAITING_PERIOD_START

    # ---------- ПРОИЗВОЛЬНЫЙ ПЕРИОД – КОНЕЦ (префикс "end_") ----------
    if data.startswith("end_"):
        if data == "end_cancel":
            await query.edit_message_text("Выбор периода отменён.")
            await query.message.reply_text("Выберите действие:", reply_markup=reports_keyboard())
            return ConversationHandler.END

        if "prev_month" in data or "next_month" in data:
            match = re.search(r'prev_month_(\d+)_(\d+)|next_month_(\d+)_(\d+)', data)
            if match:
                if match.group(1) and match.group(2):
                    year = int(match.group(1))
                    month = int(match.group(2))
                    action = "prev_month"
                else:
                    year = int(match.group(3))
                    month = int(match.group(4))
                    action = "next_month"
            else:
                await query.edit_message_text("❌ Ошибка формата навигации.")
                return WAITING_PERIOD_END

            if action == "prev_month":
                month -= 1
                if month == 0:
                    month = 12
                    year -= 1
            elif action == "next_month":
                month += 1
                if month == 13:
                    month = 1
                    year += 1
            else:
                await query.edit_message_text("❌ Неизвестное действие.")
                return WAITING_PERIOD_END

            keyboard = create_calendar(year, month, "end_")
            await query.edit_message_reply_markup(reply_markup=keyboard)
            return WAITING_PERIOD_END

        # Парсим дату конца
        end_date_str = data[4:]  # убираем "end_"
        if re.match(r"\d{4}-\d{2}-\d{2}", end_date_str):
            start_date = context.user_data.get('period_start_date')
            if not start_date:
                await query.edit_message_text("❌ Ошибка: начальная дата не найдена. Попробуйте снова.")
                return ConversationHandler.END
            if start_date > end_date_str:
                await query.edit_message_text("❌ Начальная дата позже конечной. Попробуйте сначала.")
                now = get_moscow_today()
                keyboard = create_calendar(now.year, now.month, "start_")
                await query.message.reply_text("Выберите начальную дату заново:", reply_markup=keyboard)
                return WAITING_PERIOD_START
            metrics = get_metrics_for_period(start_date, end_date_str)
            msg = format_metrics(metrics, f"Период {start_date} – {end_date_str}")
            await query.edit_message_text(msg, parse_mode="Markdown")
            await query.message.reply_text("Выберите действие:", reply_markup=reports_keyboard())
            context.user_data.pop('period_start_date', None)
            return ConversationHandler.END
        else:
            await query.edit_message_text("❌ Ошибка формата даты.")
            return WAITING_PERIOD_END

    await query.edit_message_text("❌ Неизвестная команда.")
    return ConversationHandler.END

# ---------- АДМИНИСТРИРОВАНИЕ ----------
async def add_manager_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите ID (число) или username (без @):")
    return WAITING_ADD_MANAGER

async def add_manager_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("⛔ Только для администратора.")
        return ConversationHandler.END

    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❌ Введите ID или username.")
        return WAITING_ADD_MANAGER

    if text.isdigit():
        user_id = int(text)
        try:
            user = await context.bot.get_chat(user_id)
            username = user.username or ""
            first_name = user.first_name or ""
            last_name = user.last_name or ""
        except Exception:
            await update.message.reply_text(f"❌ Не удалось найти пользователя с ID {user_id}. Убедитесь, что он уже написал боту.")
            return WAITING_ADD_MANAGER
    else:
        username = text.lstrip('@')
        try:
            user = await context.bot.get_chat(username)
            user_id = user.id
            first_name = user.first_name or ""
            last_name = user.last_name or ""
        except Exception:
            await update.message.reply_text(f"❌ Не удалось найти пользователя @{username}. Убедитесь, что он уже написал боту.")
            return WAITING_ADD_MANAGER

    if user_id == ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Администратор уже имеет доступ.")
        return WAITING_ADD_MANAGER

    context.user_data['new_manager'] = {
        'id': user_id,
        'username': username,
        'first_name': first_name,
        'last_name': last_name
    }
    await update.message.reply_text("Введите номер телефона менеджера (или '-' чтобы пропустить):")
    return WAITING_MANAGER_PHONE

async def add_manager_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("⛔ Только для администратора.")
        return ConversationHandler.END

    phone = update.message.text.strip()
    if phone == "-":
        phone = ""

    data = context.user_data.get('new_manager')
    if not data:
        await update.message.reply_text("❌ Ошибка: данные менеджера потеряны. Начните заново.")
        return ConversationHandler.END

    user_id = data['id']
    username = data['username']
    first_name = data['first_name']
    last_name = data['last_name']

    if add_manager(user_id, username, first_name, last_name, phone):
        await update.message.reply_text(f"✅ Менеджер с ID {user_id} (username: @{username}) добавлен.")
    else:
        await update.message.reply_text(f"⚠️ Менеджер с ID {user_id} уже существует.")
    context.user_data.pop('new_manager', None)
    await update.message.reply_text("Управление менеджерами:", reply_markup=admin_keyboard())
    return ConversationHandler.END

async def remove_manager_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите ID менеджера (цифры):")
    return WAITING_REMOVE_MANAGER

async def remove_manager_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("⛔ Только для администратора.")
        return ConversationHandler.END

    try:
        user_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Введите число.")
        return WAITING_REMOVE_MANAGER

    if user_id == ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Администратора нельзя удалить.")
        return WAITING_REMOVE_MANAGER

    if remove_manager(user_id):
        await update.message.reply_text(f"✅ Менеджер с ID {user_id} удалён.")
    else:
        await update.message.reply_text(f"❌ Менеджер с ID {user_id} не найден.")
    await update.message.reply_text("Управление менеджерами:", reply_markup=admin_keyboard())
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    keyboard = main_admin_keyboard() if is_admin(chat_id) else main_user_keyboard()
    await update.message.reply_text("Действие отменено.", reply_markup=keyboard)
    return ConversationHandler.END

# ---------- ЗАПУСК ----------
def main():
    if not all([OZON_CLIENT_ID, OZON_API_KEY, TELEGRAM_BOT_TOKEN]):
        write_log("❌ ОШИБКА: Не все переменные окружения установлены!")
        return

    if not OZON_PERFORMANCE_CLIENT_ID or not OZON_PERFORMANCE_CLIENT_SECRET:
        write_log("⚠️ ВНИМАНИЕ: OZON_PERFORMANCE_CLIENT_ID или CLIENT_SECRET не заданы. Рекламные расходы не будут отображаться.")

    write_log("🚀 Запуск бота...")
    application = (Application.builder()
                   .token(TELEGRAM_BOT_TOKEN)
                   .connect_timeout(30.0)
                   .read_timeout(30.0)
                   .write_timeout(30.0)
                   .build())

    application.add_handler(CommandHandler("start", start))

    application.add_handler(MessageHandler(filters.Text(["📊 Отчёт", "⚙️ Администрирование"]), handle_main_menu))
    application.add_handler(MessageHandler(filters.Text(["📅 Текущие показатели", "📆 Выбрать дату", "📊 Выбрать период", "🔙 Назад"]), handle_reports_menu))
    application.add_handler(MessageHandler(filters.Text(["📋 Список менеджеров", "🔙 Назад"]), handle_admin_menu))

    # Диалоги
    conv_date = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📆 Выбрать дату"), handle_reports_menu)],
        states={WAITING_DATE_SINGLE: [CallbackQueryHandler(handle_callback_query)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    conv_period = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📊 Выбрать период"), handle_reports_menu)],
        states={
            WAITING_PERIOD_TYPE: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PERIOD_START: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PERIOD_END: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PERIOD_YEAR: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PERIOD_MONTH: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PERIOD_QUARTER: [CallbackQueryHandler(handle_callback_query)],
            WAITING_YEAR_SELECT: [CallbackQueryHandler(handle_callback_query)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    conv_add = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("➕ Добавить менеджера"), add_manager_start)],
        states={
            WAITING_ADD_MANAGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_manager_input)],
            WAITING_MANAGER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_manager_phone)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    conv_remove = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("➖ Удалить менеджера"), remove_manager_start)],
        states={WAITING_REMOVE_MANAGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_manager_input)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_date)
    application.add_handler(conv_period)
    application.add_handler(conv_add)
    application.add_handler(conv_remove)

    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Планировщик отчётов
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
        for m in managers:
            try:
                await context.bot.send_message(chat_id=m['id'], text=msg_today, parse_mode="Markdown")
                await context.bot.send_message(chat_id=m['id'], text=msg_yesterday, parse_mode="Markdown")
                await context.bot.send_message(chat_id=m['id'], text=msg_month, parse_mode="Markdown")
            except Exception as e:
                write_log(f"Ошибка отправки {m['id']}: {e}")

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(scheduled_report, interval=60*60, first=0)
        write_log("✅ Планировщик запущен.")
    else:
        write_log("⚠️ JobQueue недоступен.")

    write_log("🚀 Бот готов.")
    application.run_polling(allowed_updates=Update.ALL_TYPES, timeout=30)

if __name__ == "__main__":
    main()
