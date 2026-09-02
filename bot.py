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
ADMIN_CHAT_ID = 6134182006

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
WAITING_MANAGER_PHONE = 7
WAITING_PERIOD_YEAR = 8
WAITING_PERIOD_MONTH = 9
WAITING_PERIOD_QUARTER = 10
WAITING_YEAR_SELECT = 11

# Ставка налога (7%)
TAX_RATE = 0.07
# =====================================================

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
    moscow_tz = datetime.timezone(datetime.timedelta(hours=3))
    return datetime.datetime.now(moscow_tz).date()

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
        total_sum_seller = 0.0
        total_sum_buyer = 0.0
        for product in products:
            qty = int(product.get("quantity", 0))
            price_buyer_str = product.get("price", "0")
            try:
                price_buyer = float(price_buyer_str)
            except:
                price_buyer = 0.0
            price_seller_str = product.get("old_price")
            if price_seller_str is not None:
                try:
                    price_seller = float(price_seller_str)
                except:
                    price_seller = price_buyer
            else:
                price_seller = price_buyer
            total_units += qty
            total_sum_seller += price_seller * qty
            total_sum_buyer += price_buyer * qty

        status = posting.get("status", "")
        if date_str not in aggregated:
            aggregated[date_str] = {
                "ordered_units": 0,
                "ordered_sum": 0.0,
                "delivered_units": 0,
                "delivered_sum": 0.0,
                "canceled_units": 0,
                "canceled_sum": 0.0,
                "taxable_delivered_sum": 0.0,
            }

        aggregated[date_str]["ordered_units"] += total_units
        aggregated[date_str]["ordered_sum"] += total_sum_seller

        if status in ("cancelled", "canceled"):
            aggregated[date_str]["canceled_units"] += total_units
            aggregated[date_str]["canceled_sum"] += total_sum_seller
        elif status in ("delivered", "completed"):
            aggregated[date_str]["delivered_units"] += total_units
            aggregated[date_str]["delivered_sum"] += total_sum_seller
            aggregated[date_str]["taxable_delivered_sum"] += total_sum_buyer

    return aggregated

# ---------- ФИНАНСОВЫЙ API (ИСПРАВЛЕННЫЙ) ----------
def fetch_financial_data_v2(date_from, date_to):
    """
    Получает финансовые транзакции за период через /v2/finance/realization.
    Год передаётся на корневом уровне, даты внутри filter.
    """
    url = "https://api-seller.ozon.ru/v2/finance/realization"
    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }
    try:
        from_dt = datetime.datetime.strptime(date_from, "%Y-%m-%d")
        to_dt = datetime.datetime.strptime(date_to, "%Y-%m-%d") + datetime.timedelta(days=1) - datetime.timedelta(seconds=1)
        year = from_dt.year
    except:
        write_log(f"❌ Ошибка парсинга дат: {date_from} {date_to}")
        return None

    payload = {
        "year": year,  # обязательное поле на корневом уровне
        "filter": {
            "date_from": from_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "date_to": to_dt.strftime("%Y-%m-%dT%H:%M:%S.999Z"),
        },
        "limit": 1000,
        "offset": 0,
    }
    all_transactions = []
    while True:
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            if response.status_code == 429:
                write_log("⚠️ 429 Too Many Requests (финансы), ждём 10 сек")
                time.sleep(10)
                continue
            response.raise_for_status()
            data = response.json()
            transactions = data.get("result", {}).get("rows", [])
            if not transactions:
                break
            all_transactions.extend(transactions)
            if len(transactions) < payload["limit"]:
                break
            payload["offset"] += payload["limit"]
        except Exception as e:
            write_log(f"❌ Ошибка получения финансов (v2): {e}")
            break
    write_log(f"💰 Загружено финансовых транзакций (v2): {len(all_transactions)} за {date_from}–{date_to}")
    return all_transactions

def fetch_financial_data_v3(date_from, date_to):
    """
    Получает финансовые транзакции через /v3/finance/transaction/list (более новый метод).
    """
    url = "https://api-seller.ozon.ru/v3/finance/transaction/list"
    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }
    try:
        from_dt = datetime.datetime.strptime(date_from, "%Y-%m-%d")
        to_dt = datetime.datetime.strptime(date_to, "%Y-%m-%d") + datetime.timedelta(days=1) - datetime.timedelta(seconds=1)
    except:
        write_log(f"❌ Ошибка парсинга дат: {date_from} {date_to}")
        return None

    payload = {
        "filter": {
            "date_from": from_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "date_to": to_dt.strftime("%Y-%m-%dT%H:%M:%S.999Z"),
        },
        "limit": 1000,
        "offset": 0,
    }
    all_transactions = []
    while True:
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            if response.status_code == 429:
                write_log("⚠️ 429 Too Many Requests (финансы v3), ждём 10 сек")
                time.sleep(10)
                continue
            response.raise_for_status()
            data = response.json()
            transactions = data.get("result", {}).get("items", [])
            if not transactions:
                break
            all_transactions.extend(transactions)
            if len(transactions) < payload["limit"]:
                break
            payload["offset"] += payload["limit"]
        except Exception as e:
            write_log(f"❌ Ошибка получения финансов (v3): {e}")
            break
    write_log(f"💰 Загружено финансовых транзакций (v3): {len(all_transactions)} за {date_from}–{date_to}")
    return all_transactions

# ---------- ОСТАЛЬНЫЕ ФУНКЦИИ ----------
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
    start_dt = datetime.datetime.strptime(date_from, "%Y-%m-%d") - datetime.timedelta(days=1)
    end_dt = datetime.datetime.strptime(date_to, "%Y-%m-%d") + datetime.timedelta(days=1)
    params = {
        "dateFrom": start_dt.strftime("%Y-%m-%d"),
        "dateTo": end_dt.strftime("%Y-%m-%d"),
    }
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if response.status_code == 429:
            write_log("⚠️ 429 Too Many Requests, ждём 10 сек")
            time.sleep(10)
            response = requests.get(url, headers=headers, params=params, timeout=15)

        write_log(f"📥 Статус ответа Performance API: {response.status_code}")
        response.raise_for_status()
        data = response.json()
        write_log(f"📥 Ответ Performance API (расширенный): {json.dumps(data, ensure_ascii=False)[:500]}")

        total_expense = 0.0
        if isinstance(data, dict) and "rows" in data:
            rows = data["rows"]
            if isinstance(rows, list):
                for item in rows:
                    item_date = item.get("date")
                    if item_date and len(item_date) >= 10:
                        item_date_str = item_date[:10]
                        if date_from <= item_date_str <= date_to:
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
        write_log(f"📊 Рекламные расходы за {date_from}–{date_to} (отфильтровано): {total_expense:.2f} ₽")
        return total_expense
    except Exception as e:
        write_log(f"❌ Ошибка получения рекламных расходов: {e}")
        return None

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

    delivered_revenue = metrics.get("delivered_sum", 0)
    if delivered_revenue > 0 and ad_expense is not None:
        metrics["effective_drr"] = (ad_expense / delivered_revenue) * 100
    else:
        metrics["effective_drr"] = None

    taxable_delivered = metrics.get("taxable_delivered_sum", 0)
    metrics["tax"] = taxable_delivered * TAX_RATE

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
        "taxable_delivered_sum": 0.0,
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

    delivered_revenue = total.get("delivered_sum", 0)
    if delivered_revenue > 0 and ad_expense is not None:
        total["effective_drr"] = (ad_expense / delivered_revenue) * 100
    else:
        total["effective_drr"] = None

    total["tax"] = total.get("taxable_delivered_sum", 0) * TAX_RATE

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
        "taxable_delivered_sum": 0.0,
    }
    for vals in agg.values():
        for key in month_data:
            month_data[key] += vals.get(key, 0)

    def add_drr_and_tax(metrics, ad_expense):
        metrics["ad_expense"] = ad_expense if ad_expense is not None else 0.0
        revenue = metrics.get("ordered_sum", 0)
        if revenue > 0 and ad_expense is not None:
            metrics["drr"] = (ad_expense / revenue) * 100
        else:
            metrics["drr"] = None
        delivered_revenue = metrics.get("delivered_sum", 0)
        if delivered_revenue > 0 and ad_expense is not None:
            metrics["effective_drr"] = (ad_expense / delivered_revenue) * 100
        else:
            metrics["effective_drr"] = None
        metrics["tax"] = metrics.get("taxable_delivered_sum", 0) * TAX_RATE
        return metrics

    ad_expense_today = fetch_advertising_expense(today_str, today_str)
    today_data = add_drr_and_tax(today_data, ad_expense_today)

    ad_expense_yesterday = fetch_advertising_expense(yesterday_str, yesterday_str)
    yesterday_data = add_drr_and_tax(yesterday_data, ad_expense_yesterday)

    ad_expense_month = fetch_advertising_expense(date_from, date_to)
    month_data = add_drr_and_tax(month_data, ad_expense_month)

    return today_data, yesterday_data, month_data

def format_metrics(metrics, title):
    if not metrics:
        return f"📊 *{title}*\n\n❌ Нет данных за указанный период."
    has_data = False
    for key, val in metrics.items():
        if key in ["drr", "effective_drr", "ad_expense", "tax", "taxable_delivered_sum"]:
            continue
        if isinstance(val, (int, float)) and val != 0:
            has_data = True
            break
    if not has_data:
        return f"📊 *{title}*\n\n❌ Нет данных за указанный период."

    ad_expense = metrics.get("ad_expense", 0)
    drr = metrics.get("drr")
    eff_drr = metrics.get("effective_drr")
    tax = metrics.get("tax", 0)
    drr_text = f"{drr:.2f}%" if drr is not None else "∞"
    eff_drr_text = f"{eff_drr:.2f}%" if eff_drr is not None else "∞"

    return (
        f"📊 *{title}*\n\n"
        f"🛒 *Заказано*\n  На сумму: {metrics.get('ordered_sum', 0):,.2f} ₽\n"
        f"  Штук: {metrics.get('ordered_units', 0)}\n\n"
        f"📦 *Доставлено*\n  На сумму: {metrics.get('delivered_sum', 0):,.2f} ₽\n"
        f"  Штук: {metrics.get('delivered_units', 0)}\n\n"
        f"❌ *Отмены*\n  На сумму: {metrics.get('canceled_sum', 0):,.2f} ₽\n"
        f"  Штук: {metrics.get('canceled_units', 0)}\n\n"
        f"📢 *Реклама*\n"
        f"  Расходы: {ad_expense:,.2f} ₽\n"
        f"  ДРР (общий): {drr_text}\n"
        f"  ДРР (по доставленным): {eff_drr_text}\n\n"
        f"🧾 *Налоги*\n"
        f"  Налог (7% от оплаченной суммы): {tax:,.2f} ₽"
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

# ---------- ОТЛАДОЧНЫЕ КОМАНДЫ ----------
async def debug_finance_variants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тестирует разные варианты запросов к финансовому API (только для админа)."""
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("⛔ Только для администратора.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Укажите даты: /debug_finance_variants 2026-07-01 2026-07-31")
        return

    date_from = args[0]
    date_to = args[1]
    try:
        datetime.datetime.strptime(date_from, "%Y-%m-%d")
        datetime.datetime.strptime(date_to, "%Y-%m-%d")
    except:
        await update.message.reply_text("Неверный формат даты. Используйте ГГГГ-ММ-ДД")
        return

    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }
    year = date_from[:4]

    msg = f"🧪 Тестирование запросов к /v2/finance/realization\nПериод: {date_from} – {date_to}\n\n"

    # Вариант 1: year + date_from/date_to
    payload1 = {
        "year": int(year),
        "filter": {
            "date_from": date_from + "T00:00:00.000Z",
            "date_to": date_to + "T23:59:59.999Z",
        },
        "limit": 10
    }
    try:
        r1 = requests.post("https://api-seller.ozon.ru/v2/finance/realization", headers=headers, json=payload1, timeout=10)
        msg += f"📌 Вариант 1 (year + date_from/date_to): код {r1.status_code}\n"
        if r1.status_code == 200:
            data1 = r1.json()
            rows = data1.get("result", {}).get("rows", [])
            msg += f"   Найдено записей: {len(rows)}\n"
            if rows:
                msg += f"   Пример: {json.dumps(rows[0], indent=2, ensure_ascii=False)[:300]}\n"
        else:
            msg += f"   Ошибка: {r1.text[:200]}\n"
    except Exception as e:
        msg += f"   Исключение: {e}\n"

    # Вариант 2: только year (без date_from/date_to)
    payload2 = {
        "year": int(year),
        "limit": 10
    }
    try:
        r2 = requests.post("https://api-seller.ozon.ru/v2/finance/realization", headers=headers, json=payload2, timeout=10)
        msg += f"\n📌 Вариант 2 (только year={year}): код {r2.status_code}\n"
        if r2.status_code == 200:
            data2 = r2.json()
            rows = data2.get("result", {}).get("rows", [])
            msg += f"   Найдено записей: {len(rows)}\n"
            if rows:
                msg += f"   Пример: {json.dumps(rows[0], indent=2, ensure_ascii=False)[:300]}\n"
        else:
            msg += f"   Ошибка: {r2.text[:200]}\n"
    except Exception as e:
        msg += f"   Исключение: {e}\n"

    # Вариант 3: year + posting_number
    posting_number = "0237561952-0099-1"
    payload3 = {
        "year": int(year),
        "filter": {
            "posting_number": posting_number
        },
        "limit": 10
    }
    try:
        r3 = requests.post("https://api-seller.ozon.ru/v2/finance/realization", headers=headers, json=payload3, timeout=10)
        msg += f"\n📌 Вариант 3 (year={year} + posting_number={posting_number}): код {r3.status_code}\n"
        if r3.status_code == 200:
            data3 = r3.json()
            rows = data3.get("result", {}).get("rows", [])
            msg += f"   Найдено записей: {len(rows)}\n"
            if rows:
                msg += f"   Пример: {json.dumps(rows[0], indent=2, ensure_ascii=False)[:300]}\n"
        else:
            msg += f"   Ошибка: {r3.text[:200]}\n"
    except Exception as e:
        msg += f"   Исключение: {e}\n"

    # Вариант 4: год + даты + posting_number
    payload4 = {
        "year": int(year),
        "filter": {
            "date_from": date_from + "T00:00:00.000Z",
            "date_to": date_to + "T23:59:59.999Z",
            "posting_number": posting_number
        },
        "limit": 10
    }
    try:
        r4 = requests.post("https://api-seller.ozon.ru/v2/finance/realization", headers=headers, json=payload4, timeout=10)
        msg += f"\n📌 Вариант 4 (год+даты+posting_number): код {r4.status_code}\n"
        if r4.status_code == 200:
            data4 = r4.json()
            rows = data4.get("result", {}).get("rows", [])
            msg += f"   Найдено записей: {len(rows)}\n"
            if rows:
                msg += f"   Пример: {json.dumps(rows[0], indent=2, ensure_ascii=False)[:300]}\n"
        else:
            msg += f"   Ошибка: {r4.text[:200]}\n"
    except Exception as e:
        msg += f"   Исключение: {e}\n"

    if len(msg) > 4000:
        msg = msg[:4000] + "\n...(обрезано)"
    await update.message.reply_text(msg)

async def debug_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает отгрузку и финансовые данные для заказа."""
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("⛔ Только для администратора.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Укажите номер отгрузки: /debug_order 08928221-0180-1")
        return

    posting_number = args[0]

    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }

    url_posting = "https://api-seller.ozon.ru/v2/posting/fbo/get"
    payload_posting = {
        "posting_number": posting_number,
        "with_financial_data": True
    }
    try:
        response = requests.post(url_posting, headers=headers, json=payload_posting, timeout=15)
        response.raise_for_status()
        data_posting = response.json()
        result = data_posting.get("result", {})
        if not result:
            await update.message.reply_text("❌ Отгрузка не найдена.")
            return

        products = result.get("products", [])
        status = result.get("status")
        created_at = result.get("created_at")
        financial_data = result.get("financial_data")
        msg = f"📦 Отгрузка {posting_number}\nСтатус: {status}\nСоздана: {created_at}\n"
        msg += f"💰 financial_data: {json.dumps(financial_data, indent=2, ensure_ascii=False) if financial_data else 'None'}\n\n"
        for idx, product in enumerate(products, 1):
            msg += f"Товар #{idx}:\n"
            msg += f"  SKU: {product.get('sku')}\n"
            msg += f"  Название: {product.get('name')}\n"
            msg += f"  Количество: {product.get('quantity')}\n"
            msg += f"  price: {product.get('price')}\n"
            msg += f"  old_price: {product.get('old_price')}\n"
            msg += "\n"

        if created_at:
            month_year = created_at[:7]
            year, month = month_year.split('-')
            date_from = f"{year}-{month}-01"
            next_month = datetime.date(int(year), int(month), 1) + datetime.timedelta(days=32)
            last_day = next_month.replace(day=1) - datetime.timedelta(days=1)
            date_to = last_day.strftime("%Y-%m-%d")
            msg += f"📅 Финансовый запрос за {date_from} – {date_to}\n"

            transactions = fetch_financial_data_v2(date_from, date_to)
            if transactions:
                found = [t for t in transactions if t.get("posting_number") == posting_number]
                if found:
                    msg += f"✅ Найдено {len(found)} транзакций для этого заказа:\n"
                    for i, t in enumerate(found[:3]):
                        msg += f"  Транзакция #{i+1}: {json.dumps(t, indent=2, ensure_ascii=False)}\n"
                else:
                    msg += "⚠️ Транзакции для этого заказа не найдены.\n"
                    if transactions:
                        msg += "📋 Пример структуры транзакции (первая):\n"
                        msg += json.dumps(transactions[0], indent=2, ensure_ascii=False)[:1500] + "\n"
            else:
                msg += "❌ Не удалось получить финансовые транзакции.\n"

        if len(msg) > 4000:
            msg = msg[:4000] + "\n...(обрезано)"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def debug_finance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает финансовые транзакции за указанный период через два метода (v2 и v3)."""
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("⛔ Только для администратора.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Укажите даты: /debug_finance 2026-07-01 2026-07-31")
        return

    date_from = args[0]
    date_to = args[1]
    try:
        datetime.datetime.strptime(date_from, "%Y-%m-%d")
        datetime.datetime.strptime(date_to, "%Y-%m-%d")
    except:
        await update.message.reply_text("Неверный формат даты. Используйте ГГГГ-ММ-ДД")
        return

    msg = f"💰 Финансовые транзакции за {date_from} – {date_to}\n\n"

    msg += "📌 Метод /v2/finance/realization:\n"
    transactions_v2 = fetch_financial_data_v2(date_from, date_to)
    if transactions_v2 is None:
        msg += "❌ Ошибка при получении данных (v2).\n"
    elif not transactions_v2:
        msg += "ℹ️ Транзакций не найдено (v2).\n"
    else:
        msg += f"✅ Найдено {len(transactions_v2)} транзакций.\n"
        for i, t in enumerate(transactions_v2[:3]):
            msg += f"--- Транзакция #{i+1} (v2) ---\n"
            msg += json.dumps(t, indent=2, ensure_ascii=False)[:800] + "\n\n"

    msg += "📌 Метод /v3/finance/transaction/list:\n"
    transactions_v3 = fetch_financial_data_v3(date_from, date_to)
    if transactions_v3 is None:
        msg += "❌ Ошибка при получении данных (v3).\n"
    elif not transactions_v3:
        msg += "ℹ️ Транзакций не найдено (v3).\n"
    else:
        msg += f"✅ Найдено {len(transactions_v3)} транзакций.\n"
        for i, t in enumerate(transactions_v3[:3]):
            msg += f"--- Транзакция #{i+1} (v3) ---\n"
            msg += json.dumps(t, indent=2, ensure_ascii=False)[:800] + "\n\n"

    if len(msg) > 4000:
        msg = msg[:4000] + "\n...(обрезано)"
    await update.message.reply_text(msg)

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id
    if text == "📊 Отчёт":
        if not has_access(chat_id):
            await update.message.reply_text("❌ Нет доступа! Обратитесь к администратору.")
            return
        await update.message.reply_text("Выберите тип отчёта:", reply_markup=reports_keyboard())
    elif text == "⚙️ Администрирование":
        if not is_admin(chat_id):
            await update.message.reply_text("⛔ Только для администратора.")
            return
        await update.message.reply_text("Управление менеджерами:", reply_markup=admin_keyboard())
    else:
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
    elif text == "📆 Выбрать дату":
        now = get_moscow_today()
        keyboard = create_calendar(now.year, now.month, "date_")
        await update.message.reply_text("Выберите дату:", reply_markup=keyboard)
        return WAITING_DATE_SINGLE
    elif text == "📊 Выбрать период":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗓️ По месяцам", callback_data="period_month")],
            [InlineKeyboardButton("📅 По кварталам", callback_data="period_quarter")],
            [InlineKeyboardButton("📆 По годам", callback_data="period_year")],
            [InlineKeyboardButton("📊 Произвольный период", callback_data="period_custom")],
            [InlineKeyboardButton("❌ Отмена", callback_data="period_cancel")]
        ])
        await update.message.reply_text("Выберите тип периода:", reply_markup=keyboard)
        return WAITING_PERIOD_TYPE
    else:
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
    elif text == "🔙 Назад":
        await update.message.reply_text("Главное меню", reply_markup=main_admin_keyboard())
    else:
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

    if data.startswith("date_"):
        if data == "date_cancel":
            await query.edit_message_text("Выбор даты отменён.")
            await query.message.reply_text("Выберите действие:", reply_markup=reports_keyboard())
            return ConversationHandler.END
        if "prev_month" in data or "next_month" in data:
            match = re.search(r'prev_month_(\d+)_(\d+)|next_month_(\d+)_(\d+)', data)
            if match:
                if match.group(1) and match.group(2):
                    year = int(match.group(1)); month = int(match.group(2)); action = "prev_month"
                else:
                    year = int(match.group(3)); month = int(match.group(4)); action = "next_month"
            else:
                await query.edit_message_text("❌ Ошибка формата навигации.")
                return WAITING_DATE_SINGLE
            if action == "prev_month":
                month -= 1
                if month == 0: month = 12; year -= 1
            else:
                month += 1
                if month == 13: month = 1; year += 1
            keyboard = create_calendar(year, month, "date_")
            await query.edit_message_reply_markup(reply_markup=keyboard)
            return WAITING_DATE_SINGLE
        date_str = data[5:]
        if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            metrics = get_metrics_for_date(date_str)
            msg = format_metrics(metrics, f"Отчёт за {date_str}")
            await query.edit_message_text(msg, parse_mode="Markdown")
            await query.message.reply_text("Выберите действие:", reply_markup=reports_keyboard())
            return ConversationHandler.END
        else:
            await query.edit_message_text("❌ Ошибка формата даты.")
            return WAITING_DATE_SINGLE

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

    if data.startswith("period_year_month_"):
        year = int(data.split("_")[-1])
        context.user_data['period_year'] = year
        months = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
        buttons = [[InlineKeyboardButton(name, callback_data=f"period_month_{i}_{year}")] for i, name in enumerate(months, 1)]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="period_cancel")])
        await query.edit_message_text(f"Выберите месяц {year}:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PERIOD_MONTH
    if data.startswith("period_year_quarter_"):
        year = int(data.split("_")[-1])
        context.user_data['period_year'] = year
        quarters = ["1 квартал (янв-мар)", "2 квартал (апр-июн)", "3 квартал (июл-сен)", "4 квартал (окт-дек)"]
        buttons = [[InlineKeyboardButton(name, callback_data=f"period_quarter_{i}_{year}")] for i, name in enumerate(quarters, 1)]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="period_cancel")])
        await query.edit_message_text(f"Выберите квартал {year}:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PERIOD_QUARTER

    if data.startswith("period_year_only_"):
        year = int(data.split("_")[-1])
        first_day = datetime.date(year, 1, 1)
        last_day = datetime.date(year, 12, 31)
        metrics = get_metrics_for_period(first_day.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d"))
        msg = format_metrics(metrics, f"Год {year}")
        await query.edit_message_text(msg, parse_mode="Markdown")
        await query.message.reply_text("Выберите действие:", reply_markup=reports_keyboard())
        return ConversationHandler.END

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

    if data.startswith("start_"):
        if data == "start_cancel":
            await query.edit_message_text("Выбор периода отменён.")
            await query.message.reply_text("Выберите действие:", reply_markup=reports_keyboard())
            return ConversationHandler.END
        if "prev_month" in data or "next_month" in data:
            match = re.search(r'prev_month_(\d+)_(\d+)|next_month_(\d+)_(\d+)', data)
            if match:
                if match.group(1) and match.group(2):
                    year = int(match.group(1)); month = int(match.group(2)); action = "prev_month"
                else:
                    year = int(match.group(3)); month = int(match.group(4)); action = "next_month"
            else:
                await query.edit_message_text("❌ Ошибка формата навигации.")
                return WAITING_PERIOD_START
            if action == "prev_month":
                month -= 1
                if month == 0: month = 12; year -= 1
            else:
                month += 1
                if month == 13: month = 1; year += 1
            keyboard = create_calendar(year, month, "start_")
            await query.edit_message_reply_markup(reply_markup=keyboard)
            return WAITING_PERIOD_START
        date_str = data[6:]
        if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            context.user_data['period_start_date'] = date_str
            now = get_moscow_today()
            keyboard = create_calendar(now.year, now.month, "end_")
            await query.edit_message_text(f"Начало: {date_str}\nТеперь выберите конечную дату:", reply_markup=keyboard)
            return WAITING_PERIOD_END
        else:
            await query.edit_message_text("❌ Ошибка формата даты.")
            return WAITING_PERIOD_START

    if data.startswith("end_"):
        if data == "end_cancel":
            await query.edit_message_text("Выбор периода отменён.")
            await query.message.reply_text("Выберите действие:", reply_markup=reports_keyboard())
            return ConversationHandler.END
        if "prev_month" in data or "next_month" in data:
            match = re.search(r'prev_month_(\d+)_(\d+)|next_month_(\d+)_(\d+)', data)
            if match:
                if match.group(1) and match.group(2):
                    year = int(match.group(1)); month = int(match.group(2)); action = "prev_month"
                else:
                    year = int(match.group(3)); month = int(match.group(4)); action = "next_month"
            else:
                await query.edit_message_text("❌ Ошибка формата навигации.")
                return WAITING_PERIOD_END
            if action == "prev_month":
                month -= 1
                if month == 0: month = 12; year -= 1
            else:
                month += 1
                if month == 13: month = 1; year += 1
            keyboard = create_calendar(year, month, "end_")
            await query.edit_message_reply_markup(reply_markup=keyboard)
            return WAITING_PERIOD_END
        end_date_str = data[4:]
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
    user_id = data['id']; username = data['username']; first_name = data['first_name']; last_name = data['last_name']
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
    application.add_handler(CommandHandler("debug_order", debug_order))
    application.add_handler(CommandHandler("debug_finance", debug_finance))
    application.add_handler(CommandHandler("debug_finance_variants", debug_finance_variants))

    application.add_handler(MessageHandler(filters.Text(["📊 Отчёт", "⚙️ Администрирование"]), handle_main_menu))
    application.add_handler(MessageHandler(filters.Text(["📅 Текущие показатели", "📆 Выбрать дату", "📊 Выбрать период", "🔙 Назад"]), handle_reports_menu))
    application.add_handler(MessageHandler(filters.Text(["📋 Список менеджеров", "🔙 Назад"]), handle_admin_menu))

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
