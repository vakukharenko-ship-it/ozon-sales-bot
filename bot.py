#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import aiohttp
import json
import re
import os
import io
import sys
import socket
import calendar
from datetime import datetime, timedelta, timezone, date, time
from dateutil.relativedelta import relativedelta  # нужно для LFL, но можно обойтись timedelta

# Проверим наличие dateutil, если нет, можно заменить на timedelta, но для месяцев лучше dateutil
try:
    from dateutil.relativedelta import relativedelta
except ImportError:
    # fallback: реализуем свой relativedelta? Но для простоты оставим и потребуем установить.
    print("Не хватает python-dateutil. Установите: pip install python-dateutil")
    sys.exit(1)

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes, JobQueue
)

# Загрузка .env из папки бота
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# --- Константы ---
VERSION = "2.2.0"
API_TIMEOUT = 120
API_MAX_DAYS_PER_REQUEST = 90
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY = 2
CACHE_TTL_SECONDS = 300
RATE_LIMIT_REQUESTS_PER_SECOND = 2
MOSCOW_TZ = timezone(timedelta(hours=3))
LOG_FILE = "data/ozon_log.txt"
MANAGERS_FILE = "managers.json"
SETTINGS_FILE = "settings.json"

# --- Глобальные переменные ---
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", 0))
OZON_PERFORMANCE_CLIENT_ID = os.getenv("OZON_PERFORMANCE_CLIENT_ID")
OZON_PERFORMANCE_CLIENT_SECRET = os.getenv("OZON_PERFORMANCE_CLIENT_SECRET")

# Проверка обязательных переменных
if not all([OZON_CLIENT_ID, OZON_API_KEY, TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID]):
    print("Ошибка: не заданы обязательные переменные окружения в .env")
    sys.exit(1)

# Создание папок
os.makedirs("data", exist_ok=True)

# Глобальный семафор для ограничения частоты запросов
rate_limiter = asyncio.Semaphore(RATE_LIMIT_REQUESTS_PER_SECOND)

# Простой кэш
cache = {}
cache_lock = asyncio.Lock()

# --- Логирование ---
def write_log(message: str):
    timestamp = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception as e:
        print(f"Ошибка записи в лог: {e}")

# --- Работа с файлами данных ---
def load_managers():
    try:
        with open(MANAGERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_managers(managers):
    with open(MANAGERS_FILE, "w", encoding="utf-8") as f:
        json.dump(managers, f, ensure_ascii=False, indent=2)

def load_settings():
    default = {
        "mode": "hourly",
        "hourly": {"quiet_start": 23, "quiet_end": 9, "yesterday_hour": 9},
        "slots": []
    }
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_settings(settings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

# --- Проверка доступа ---
def is_admin(chat_id: int) -> bool:
    return chat_id == ADMIN_CHAT_ID

def is_manager(chat_id: int) -> bool:
    managers = load_managers()
    return any(m.get("id") == chat_id for m in managers)

def has_access(chat_id: int) -> bool:
    return is_admin(chat_id) or is_manager(chat_id)

# --- Вспомогательные функции ---
def format_number(value):
    """Форматирование числа с пробелами тысяч."""
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except:
        return "0"

def format_money(value):
    return f"{format_number(value)} ₽"

def calc_delta(current, previous):
    """Расчет изменения в процентах."""
    if previous == 0:
        if current > 0:
            return "+∞"
        elif current == 0:
            return "0.0%"
        else:
            return "−∞"
    delta = ((current - previous) / abs(previous)) * 100
    return f"{delta:+.1f}%"

def indicator(delta_str, better_is_higher=True):
    """Возвращает эмодзи на основе знака дельты."""
    if delta_str.startswith("+"):
        return "🟢" if better_is_higher else "🔴"
    elif delta_str.startswith("-"):
        return "🔴" if better_is_higher else "🟢"
    else:
        return ""

def moscow_now():
    return datetime.now(MOSCOW_TZ)

def start_of_day_msk(dt=None):
    if dt is None:
        dt = moscow_now()
    return datetime(dt.year, dt.month, dt.day, tzinfo=MOSCOW_TZ)

def end_of_day_msk(dt=None):
    if dt is None:
        dt = moscow_now()
    return datetime(dt.year, dt.month, dt.day, 23, 59, 59, 999999, tzinfo=MOSCOW_TZ)

def to_utc_iso(dt):
    """Переводит datetime в UTC и возвращает строку ISO с Z."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOSCOW_TZ)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

def from_utc_iso(s):
    """Преобразует строку UTC в datetime в МСК."""
    dt_utc = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt_utc.astimezone(MOSCOW_TZ)

# --- API Ozon ---
async def api_request_with_retry(session, method, url, headers=None, json_data=None, params=None):
    """Выполняет HTTP-запрос с повторными попытками и rate limiting."""
    for attempt in range(API_RETRY_ATTEMPTS):
        try:
            async with rate_limiter:
                async with session.request(method, url, headers=headers, json=json_data, params=params, timeout=API_TIMEOUT) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status in (429, 500, 502, 503, 504):
                        if attempt < API_RETRY_ATTEMPTS - 1:
                            wait = API_RETRY_DELAY * (2 ** attempt)
                            await asyncio.sleep(wait)
                            continue
                    else:
                        # Другие ошибки
                        text = await resp.text()
                        write_log(f"API ошибка {resp.status}: {text}")
                        return None
        except asyncio.TimeoutError:
            write_log(f"Таймаут при запросе к {url}")
            if attempt < API_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(API_RETRY_DELAY * (2 ** attempt))
                continue
        except Exception as e:
            write_log(f"Ошибка API: {e}")
            if attempt < API_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(API_RETRY_DELAY * (2 ** attempt))
                continue
    return None

async def get_seller_postings(session, date_from, date_to):
    """Получение постингов FBO за период (даты в формате YYYY-MM-DD)."""
    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json"
    }
    all_postings = []
    offset = 0
    while True:
        payload = {
            "date_from": date_from,
            "date_to": date_to,
            "status": "",
            "limit": 1000,
            "offset": offset
        }
        data = await api_request_with_retry(session, "POST", "https://api-seller.ozon.ru/v2/posting/fbo/list", headers=headers, json_data=payload)
        if not data or "result" not in data:
            break
        result = data["result"]
        all_postings.extend(result)
        if len(result) < 1000:
            break
        offset += 1000
    return all_postings

async def get_seller_finance(session, date_from: datetime, date_to: datetime):
    """Получение финансовых операций за период (datetime в МСК, преобразуем в UTC)."""
    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json"
    }
    all_ops = []
    page = 1
    while True:
        payload = {
            "filter": {
                "date": {
                    "from": to_utc_iso(date_from),
                    "to": to_utc_iso(date_to)
                }
            },
            "page": page,
            "page_size": 1000
        }
        data = await api_request_with_retry(session, "POST", "https://api-seller.ozon.ru/v3/finance/transaction/list", headers=headers, json_data=payload)
        if not data or "result" not in data:
            break
        result = data["result"]
        ops = result.get("operations", [])
        all_ops.extend(ops)
        total = result.get("total", 0)
        if len(all_ops) >= total:
            break
        page += 1
    return all_ops

async def get_performance_token(session):
    """Получение токена для Performance API."""
    if not OZON_PERFORMANCE_CLIENT_ID or not OZON_PERFORMANCE_CLIENT_SECRET:
        return None
    payload = {
        "client_id": OZON_PERFORMANCE_CLIENT_ID,
        "client_secret": OZON_PERFORMANCE_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }
    data = await api_request_with_retry(session, "POST", "https://api-performance.ozon.ru/api/client/token", json_data=payload)
    if data and "access_token" in data:
        return data["access_token"]
    return None

async def get_performance_expenses(session, token, date_from: datetime, date_to: datetime):
    """Получение расходов на рекламу за период."""
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "dateFrom": date_from.strftime("%Y-%m-%d"),
        "dateTo": date_to.strftime("%Y-%m-%d")
    }
    data = await api_request_with_retry(session, "GET", "https://api-performance.ozon.ru/api/client/statistics/expense/json", headers=headers, params=params)
    if data and "rows" in data:
        total = sum(float(row.get("moneySpent", 0)) for row in data["rows"])
        return total
    return 0.0

# --- Агрегация постингов ---
def aggregate_postings(postings):
    """Агрегирует постинги по всем товарам в общие суммы."""
    agg = {
        "ordered_units": 0,
        "ordered_sum": 0.0,
        "delivered_units": 0,
        "delivered_sum": 0.0,
        "canceled_units": 0,
        "canceled_sum": 0.0
    }
    for post in postings:
        status = post.get("status", "")
        products = post.get("products", [])
        for prod in products:
            qty = int(prod.get("quantity", 0))
            price = float(prod.get("price", 0) or 0)
            total = qty * price
            if status == "delivered":
                agg["delivered_units"] += qty
                agg["delivered_sum"] += total
            elif status == "cancelled":
                agg["canceled_units"] += qty
                agg["canceled_sum"] += total
            # Заказано включает все, кроме отмен? Обычно заказано = все постинги, не отмененные.
            # По спецификации "Заказано" - сумма всех заказов, кроме отмен.
            # Поэтому будем считать ordered = delivered + returned? Но проще: все постинги, у которых статус не cancelled.
            if status != "cancelled":
                agg["ordered_units"] += qty
                agg["ordered_sum"] += total
    return agg

def aggregate_postings_multi(postings, periods):
    """Агрегирует постинги сразу для нескольких периодов.
    periods - dict {label: (start_dt, end_dt)} в МСК.
    Возвращает {label: agg}."""
    result = {label: aggregate_postings([]) for label in periods}  # инициализация
    for post in postings:
        created_str = post.get("created_at")
        if not created_str:
            continue
        created = from_utc_iso(created_str)
        status = post.get("status", "")
        products = post.get("products", [])
        for label, (start, end) in periods.items():
            if start <= created <= end:
                for prod in products:
                    qty = int(prod.get("quantity", 0))
                    price = float(prod.get("price", 0) or 0)
                    total = qty * price
                    if status == "delivered":
                        result[label]["delivered_units"] += qty
                        result[label]["delivered_sum"] += total
                    elif status == "cancelled":
                        result[label]["canceled_units"] += qty
                        result[label]["canceled_sum"] += total
                    if status != "cancelled":
                        result[label]["ordered_units"] += qty
                        result[label]["ordered_sum"] += total
    return result

# --- Финансовые расходы ---
# Маппинг категорий (сокращения)
NAME_MAP = {
    "Комиссия Ozon": "Комиссия",
    "Оплата эквайринга": "Эквайринг",
    "Доставка покупателю": "Доставка покупателю",
    "MarketplaceServiceItemDirectFlowLogistic": "Логистика прямая",
    "MarketplaceServiceItemReturnFlowLogistic": "Логистика возвратная",
    "MarketplaceServiceItemFbsLogistic": "Логистика FBS",
    "MarketplaceServiceItemFboLogistic": "Логистика FBO",
    "MarketplaceServiceItemFboFulfillment": "Фулфилмент",
    "MarketplaceServiceItemStorage": "Хранение",
    "MarketplaceServiceItemLastMile": "Последняя миля",
    "MarketplaceServiceItemDropoff": "Дроп-офф",
    "MarketplaceServiceItemPickup": "ПВЗ",
    "MarketplaceServiceItemDelivery": "Доставка",
    "MarketplaceServiceItemReturn": "Возвраты",
    "SaleCommission": "Комиссия с продажи",
    "MarketplaceMarketingExpense": "Маркетинг",
    "MarketplaceServiceItemProcessing": "Обработка",
    "MarketplaceServiceItemAssembly": "Сборка",
    "MarketplaceServiceItemLabeling": "Маркировка",
}

def parse_finance_operations(operations):
    """Возвращает словарь расходов по категориям и итог."""
    expenses = {}
    total = 0.0
    for op in operations:
        amount = float(op.get("amount", 0))
        if amount >= 0:
            continue  # нас интересуют расходы (отрицательные суммы)
        expense = -amount
        # Определяем категорию
        category = "Прочее"
        # Проверяем поля
        for field in ["sale_commission", "accruals_for_sale", "delivery_charge", "return_delivery_charge"]:
            val = op.get(field)
            if val is not None:
                val = float(val)
                if val < 0:
                    expense += val  # уже отрицательное? Лучше обработать ниже
        # Сервисы
        services = op.get("services", [])
        for srv in services:
            name = srv.get("name", "")
            srv_amount = float(srv.get("price", srv.get("amount", 0)))
            if srv_amount < 0:
                exp = -srv_amount
                mapped = NAME_MAP.get(name, name)
                expenses[mapped] = expenses.get(mapped, 0.0) + exp
                total += exp
        # Если нет сервисов, но есть operation_type_name
        if not services and expense > 0:
            op_type = op.get("operation_type_name", "Прочее")
            mapped = NAME_MAP.get(op_type, op_type)
            expenses[mapped] = expenses.get(mapped, 0.0) + expense
            total += expense
    return total, expenses

# --- Форматирование отчётов ---
def format_combined_metrics_with_deltas(data_today, data_month, data_prev_month, include_yesterday=False, data_yesterday=None):
    """Формирует текст комбинированного отчёта.
    data_today: агрегированные данные за сегодня
    data_month: за текущий месяц (LFL)
    data_prev_month: за аналогичный период прошлого месяца
    include_yesterday: добавлять ли блок Вчера
    data_yesterday: агрегированные данные за вчера (если include_yesterday)
    Дополнительно: расходы, реклама и т.д. передаются отдельно.
    Эта функция будет вызываться из более высокоуровневой, которая соберёт все данные."""
    # Заглушка, реальная функция ниже
    pass

# --- Построение графиков ---
def generate_sales_chart(years_list):
    """Генерирует график динамики продаж по годам (сумма доставленных по месяцам)."""
    # Здесь будет загрузка данных и построение
    pass

def generate_product_chart_by_metric(sku, metric, years):
    pass

# --- Inline-календарь ---
def build_calendar(year, month, selected_callback_prefix):
    """Создает inline-клавиатуру календаря на месяц."""
    # Заголовок: Месяц Год + кнопки навигации
    month_name = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"][month - 1]
    header = f"{month_name} {year}"
    # Кнопки навигации
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1
    buttons = [
        [InlineKeyboardButton("◀️", callback_data=f"cal_nav_{prev_year}_{prev_month}"),
         InlineKeyboardButton(header, callback_data="ignore"),
         InlineKeyboardButton("▶️", callback_data=f"cal_nav_{next_year}_{next_month}")]
    ]
    # Дни недели
    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    buttons.append([InlineKeyboardButton(day, callback_data="ignore") for day in week_days])
    # Календарная сетка
    cal = calendar.Calendar(firstweekday=0)
    month_days = cal.monthdayscalendar(year, month)
    for week in month_days:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="ignore"))
            else:
                row.append(InlineKeyboardButton(str(day), callback_data=f"{selected_callback_prefix}_{year}-{month:02d}-{day:02d}"))
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)

# --- Обработчики ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not has_access(chat_id):
        await update.message.reply_text("❌ Нет доступа! Обратитесь к администратору.", reply_markup=ReplyKeyboardRemove())
        return
    # Приветствие
    now = moscow_now()
    hour = now.hour
    if 5 <= hour < 12:
        greeting = "Доброе утро"
    elif 12 <= hour < 18:
        greeting = "Добрый день"
    elif 18 <= hour < 24:
        greeting = "Добрый вечер"
    else:
        greeting = "Доброй ночи"
    user = update.effective_user
    name = user.first_name or "уважаемый пользователь"
    text = f"{greeting}, {name}!\nВыберите раздел:"
    # Клавиатура
    buttons = [
        ["📊 Отчёт по продажам", "📦 Отчёт по товарам"]
    ]
    if is_admin(chat_id):
        buttons.append(["⚙️ Администрирование", "📖 Справка"])
    else:
        buttons.append(["📖 Справка"])
    reply_markup = ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    await update.message.reply_text(text, reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(update.effective_chat.id):
        await update.message.reply_text("❌ Нет доступа! Обратитесь к администратору.")
        return
    help_text = (
        f"🤖 Бот аналитики Ozon v{VERSION}\n\n"
        "📊 Отчёт по продажам — данные о заказах, доставках, отменах и расходах.\n"
        "📦 Отчёт по товарам — топ товаров, аналитика по SKU.\n"
        "📖 Справка — это сообщение.\n"
        "⚙️ Администрирование (только для админа) — управление менеджерами и рассылками."
    )
    await update.message.reply_text(help_text)

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Обработчик для кнопки "Назад"
    await start(update, context)

# --- Обработчики подменю продаж ---
async def sales_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(update.effective_chat.id):
        await update.message.reply_text("❌ Нет доступа!")
        return
    keyboard = [
        ["📅 Продажи за сегодня"],
        ["📆 Выбрать дату", "📊 Выбрать период"],
        ["📈 Динамика продаж"],
        ["🔙 Назад"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def sales_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not has_access(chat_id):
        await update.message.reply_text("❌ Нет доступа!")
        return
    # Отправляем заглушку
    msg = await update.message.reply_text("⏳ Загружаю данные...")
    try:
        # Здесь будет сбор данных и отправка
        # Пока заглушка
        await msg.edit_text("📊 Данные за сегодня будут здесь.")
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

# --- Администрирование ---
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("❌ Нет доступа!")
        return
    keyboard = [
        ["➕ Добавить менеджера", "➖ Удалить менеджера"],
        ["📋 Список менеджеров"],
        ["⚙️ Настройка рассылок"],
        ["🔙 Назад"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Администрирование:", reply_markup=reply_markup)

# --- Добавление менеджера (ConversationHandler) ---
ASK_ID, ASK_PHONE = range(2)

async def add_manager_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("❌ Нет доступа!")
        return ConversationHandler.END
    await update.message.reply_text("Введите Telegram ID менеджера (число) или username (без @):")
    return ASK_ID

async def add_manager_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("❌ Нет доступа!")
        return ConversationHandler.END
    input_data = update.message.text.strip()
    # Проверка: число или username
    if input_data.isdigit():
        manager_id = int(input_data)
        try:
            chat = await context.bot.get_chat(manager_id)
        except Exception:
            await update.message.reply_text("❌ Пользователь не найден в Telegram. Попросите его написать боту.")
            return ConversationHandler.END
        # Сохраняем во временные данные
        context.user_data['new_manager'] = {
            'id': manager_id,
            'username': chat.username,
            'first_name': chat.first_name,
            'last_name': chat.last_name
        }
    else:
        username = input_data.lstrip('@')
        # Попытка получить chat по username не всегда работает, попробуем
        try:
            chat = await context.bot.get_chat(f"@{username}")
            manager_id = chat.id
            context.user_data['new_manager'] = {
                'id': manager_id,
                'username': chat.username,
                'first_name': chat.first_name,
                'last_name': chat.last_name
            }
        except Exception:
            await update.message.reply_text("❌ Пользователь не найден. Убедитесь, что username верный и пользователь начал диалог с ботом.")
            return ConversationHandler.END
    await update.message.reply_text("Введите номер телефона менеджера (или '-' чтобы пропустить):")
    return ASK_PHONE

async def add_manager_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("❌ Нет доступа!")
        return ConversationHandler.END
    phone = update.message.text.strip()
    if phone == "-":
        phone = ""
    manager_data = context.user_data.get('new_manager')
    if not manager_data:
        await update.message.reply_text("Ошибка, начните заново.")
        return ConversationHandler.END
    manager_data['phone'] = phone
    # Проверка на дубликат
    managers = load_managers()
    if any(m.get('id') == manager_data['id'] for m in managers):
        await update.message.reply_text("❌ Этот менеджер уже добавлен.")
        return ConversationHandler.END
    managers.append(manager_data)
    save_managers(managers)
    await update.message.reply_text(f"✅ Менеджер добавлен: ID {manager_data['id']}")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END

# --- Удаление менеджера ---
async def delete_manager_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("❌ Нет доступа!")
        return
    await update.message.reply_text("Введите Telegram ID менеджера для удаления:")
    context.user_data['delete_state'] = True

async def handle_delete_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("❌ Нет доступа!")
        return
    if not context.user_data.get('delete_state'):
        return
    try:
        manager_id = int(update.message.text.strip())
        if manager_id == ADMIN_CHAT_ID:
            await update.message.reply_text("❌ Нельзя удалить администратора.")
            context.user_data['delete_state'] = False
            return
        managers = load_managers()
        new_managers = [m for m in managers if m.get('id') != manager_id]
        if len(new_managers) == len(managers):
            await update.message.reply_text("❌ Менеджер с таким ID не найден.")
        else:
            save_managers(new_managers)
            await update.message.reply_text("✅ Менеджер удалён.")
    except ValueError:
        await update.message.reply_text("❌ Введите корректный числовой ID.")
    context.user_data['delete_state'] = False

# --- Список менеджеров ---
async def list_managers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("❌ Нет доступа!")
        return
    managers = load_managers()
    if not managers:
        await update.message.reply_text("Список менеджеров пуст.")
        return
    lines = []
    for m in managers:
        name_parts = [m.get('first_name', ''), m.get('last_name', '')]
        name = " ".join([p for p in name_parts if p]).strip() or "—"
        username = f"@{m['username']}" if m.get('username') else ""
        phone = f"📞 {m.get('phone')}" if m.get('phone') else ""
        lines.append(f"ID: {m['id']}, {username}, {name}, {phone}")
    text = "Менеджеры:\n" + "\n".join(lines)
    await update.message.reply_text(text)

# --- Настройка рассылок ---
async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("❌ Нет доступа!")
        return
    # Покажем текущие настройки и inline-кнопки
    settings = load_settings()
    mode = settings.get("mode", "hourly")
    text = f"Текущий режим: {mode}\n"
    if mode == "hourly":
        h = settings.get("hourly", {})
        text += f"Тишина: {h.get('quiet_start', 0)}:00 - {h.get('quiet_end', 0)}:00\n"
        text += f"Час для «Вчера»: {h.get('yesterday_hour', 0)}:00\n"
    else:
        slots = settings.get("slots", [])
        text += "Слоты:\n"
        for slot in slots:
            inc = "✅" if slot.get("include_yesterday") else "❌"
            text += f"  {slot['hour']}:00 {inc}\n"
    keyboard = [
        [InlineKeyboardButton("🔄 Переключить режим", callback_data="toggle_mode")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_admin")]
    ]
    if mode == "hourly":
        keyboard.append([InlineKeyboardButton("Настроить тишину (начало)", callback_data="set_quiet_start")])
        keyboard.append([InlineKeyboardButton("Настроить тишину (конец)", callback_data="set_quiet_end")])
        keyboard.append([InlineKeyboardButton("Настроить час «Вчера»", callback_data="set_yesterday_hour")])
    else:
        keyboard.append([InlineKeyboardButton("➕ Добавить слот", callback_data="add_slot")])
        keyboard.append([InlineKeyboardButton("Управление слотами", callback_data="manage_slots")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    if not is_admin(chat_id):
        return
    settings = load_settings()
    if data == "toggle_mode":
        settings["mode"] = "slots" if settings.get("mode") == "hourly" else "hourly"
        save_settings(settings)
        await query.edit_message_text("Режим переключен.")
        # Обновим меню заново
        await settings_menu(update, context)
    elif data == "back_to_admin":
        # Вернуться в админ-меню (удалить inline, показать обычное)
        await query.message.delete()
        await admin_menu(update, context)
    elif data in ("set_quiet_start", "set_quiet_end", "set_yesterday_hour"):
        # Показать часы 0-23
        buttons = []
        for h in range(0, 24, 4):
            row = []
            for hh in range(h, min(h+4, 24)):
                row.append(InlineKeyboardButton(str(hh), callback_data=f"hour_{data}_{hh}"))
            buttons.append(row)
        await query.edit_message_text("Выберите час:", reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("hour_"):
        parts = data.split("_")
        action = parts[1]  # set_quiet_start, set_quiet_end, set_yesterday_hour
        hour = int(parts[2])
        if action == "set_quiet_start":
            settings["hourly"]["quiet_start"] = hour
        elif action == "set_quiet_end":
            settings["hourly"]["quiet_end"] = hour
        elif action == "set_yesterday_hour":
            settings["hourly"]["yesterday_hour"] = hour
        save_settings(settings)
        await query.edit_message_text("Сохранено.")
        # Вернуться в настройки
        await settings_menu(update, context)
    elif data == "add_slot":
        # Показать часы для выбора
        buttons = []
        for h in range(0, 24, 4):
            row = []
            for hh in range(h, min(h+4, 24)):
                row.append(InlineKeyboardButton(str(hh), callback_data=f"addslot_{hh}"))
            buttons.append(row)
        await query.edit_message_text("Выберите час для нового слота:", reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("addslot_"):
        hour = int(data.split("_")[1])
        if any(s.get("hour") == hour for s in settings.get("slots", [])):
            await query.edit_message_text("❌ Слот с таким часом уже существует.")
        else:
            settings.setdefault("slots", []).append({"hour": hour, "include_yesterday": False})
            save_settings(settings)
            await query.edit_message_text("✅ Слот добавлен.")
        # Вернуться в настройки
        await settings_menu(update, context)
    elif data == "manage_slots":
        # Показать список слотов с кнопками управления
        slots = settings.get("slots", [])
        if not slots:
            await query.edit_message_text("Слотов нет.")
            return
        buttons = []
        for slot in slots:
            inc = "✅" if slot.get("include_yesterday") else "❌"
            hour = slot["hour"]
            buttons.append([
                InlineKeyboardButton(f"🔄 {hour:02d}:00 {inc}", callback_data=f"toggleslot_{hour}"),
                InlineKeyboardButton("❌ Удалить", callback_data=f"delslot_{hour}")
            ])
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_settings")])
        await query.edit_message_text("Управление слотами:", reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("toggleslot_"):
        hour = int(data.split("_")[1])
        for slot in settings.get("slots", []):
            if slot.get("hour") == hour:
                slot["include_yesterday"] = not slot.get("include_yesterday", False)
                break
        save_settings(settings)
        # Обновить меню управления слотами
        await settings_callback(update, context)
    elif data.startswith("delslot_"):
        hour = int(data.split("_")[1])
        settings["slots"] = [s for s in settings.get("slots", []) if s.get("hour") != hour]
        save_settings(settings)
        # Обновить меню
        await settings_callback(update, context)
    elif data == "back_to_settings":
        await settings_menu(update, context)

# --- Планировщик ---
async def scheduled_report(context: ContextTypes.DEFAULT_TYPE):
    now = moscow_now()
    hour = now.hour
    settings = load_settings()
    include_yesterday = False
    send = False
    if settings.get("mode") == "hourly":
        qs = settings.get("hourly", {}).get("quiet_start", 23)
        qe = settings.get("hourly", {}).get("quiet_end", 9)
        yh = settings.get("hourly", {}).get("yesterday_hour", 9)
        # Проверка интервала тишины
        if qs <= qe:
            in_quiet = qs <= hour <= qe
        else:
            in_quiet = hour >= qs or hour <= qe
        if not in_quiet:
            send = True
            include_yesterday = (hour == yh)
    else:  # slots
        for slot in settings.get("slots", []):
            if slot.get("hour") == hour:
                send = True
                include_yesterday = slot.get("include_yesterday", False)
                break
    if send:
        # Отправка отчёта всем менеджерам
        managers = load_managers()
        # Здесь должна быть функция формирования отчёта
        # Пока заглушка
        text = "Автоматический отчёт (заглушка)"
        for manager in managers:
            try:
                await context.bot.send_message(chat_id=manager["id"], text=text)
            except Exception as e:
                write_log(f"Ошибка отправки менеджеру {manager['id']}: {e}")

# --- Сборка приложения ---
def main():
    # Инициализация Application
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # ConversationHandler для добавления менеджера
    add_manager_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r'^➕ Добавить менеджера$'), add_manager_start)],
        states={
            ASK_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_manager_id)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_manager_phone)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # Обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # Основные кнопки
    app.add_handler(MessageHandler(filters.Regex(r'^📊 Отчёт по продажам$'), sales_menu))
    app.add_handler(MessageHandler(filters.Regex(r'^📦 Отчёт по товарам$'), lambda u, c: u.message.reply_text("Товары (заглушка)")))
    app.add_handler(MessageHandler(filters.Regex(r'^📖 Справка$'), help_command))
    app.add_handler(MessageHandler(filters.Regex(r'^⚙️ Администрирование$'), admin_menu))
    app.add_handler(MessageHandler(filters.Regex(r'^🔙 Назад$'), back_to_main))
    app.add_handler(MessageHandler(filters.Regex(r'^📅 Продажи за сегодня$'), sales_today))
    # Остальные кнопки продаж можно добавить позже

    # Админ-меню
    app.add_handler(MessageHandler(filters.Regex(r'^📋 Список менеджеров$'), list_managers))
    app.add_handler(MessageHandler(filters.Regex(r'^➖ Удалить менеджера$'), delete_manager_start))
    app.add_handler(MessageHandler(filters.Regex(r'^⚙️ Настройка рассылок$'), settings_menu))

    # Обработчик текстовых сообщений для удаления менеджера (когда ожидается ID)
    # Используем фильтр, который проверяет флаг delete_state
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_delete_manager))

    # ConversationHandler для добавления менеджера
    app.add_handler(add_manager_handler)

    # Callback query handler
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^(toggle_mode|back_to_admin|set_quiet_|hour_|add_slot|addslot_|manage_slots|toggleslot_|delslot_|back_to_settings)"))

    # Планировщик (каждый час)
    job_queue = app.job_queue
    job_queue.run_repeating(scheduled_report, interval=3600, first=10)

    # Запуск
    write_log(f"Бот запущен, версия {VERSION}")
    write_log(f"Ключи: ClientId={OZON_CLIENT_ID[:4]}..., ApiKey={OZON_API_KEY[:4]}...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
