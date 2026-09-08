import datetime
import json
import os
import time
import re
import calendar
import asyncio
import aiohttp
import warnings
import sys
from typing import Optional, List, Tuple, Dict
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    filters, ConversationHandler, CallbackQueryHandler
)
from telegram.warnings import PTBUserWarning

# Графики
import matplotlib.pyplot as plt
import io
from matplotlib.dates import MonthLocator, DateFormatter
import matplotlib.dates as mdates

warnings.filterwarnings("ignore", category=PTBUserWarning)

# ==================== ВЕРСИЯ И ИСТОРИЯ ====================
VERSION = "2.3.4"
CHANGELOG_MESSAGE = "Добавлено логирование payload и тела ответа для финансового API"

# ==================== КОНСТАНТЫ ====================
API_TIMEOUT = 15
API_MAX_DAYS_PER_REQUEST = 90
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY = 2
CACHE_TTL_SECONDS = 300
RATE_LIMIT_REQUESTS_PER_SECOND = 2
SETTINGS_FILE = "settings.json"
VERSION_HISTORY_FILE = "version_history.json"
SETTINGS_LOCK = asyncio.Lock()
LOG_FILE = "/app/data/ozon_log.txt"

# ==================== ГЛОБАЛЬНЫЕ ОБЪЕКТЫ ====================
_http_session = None
_rate_limiter = None
_cache_lock = asyncio.Lock()
_api_cache = {}
_cache_timestamps = {}

# ==================== КЭШ ====================
async def get_from_cache(key):
    async with _cache_lock:
        if key in _api_cache:
            timestamp = _cache_timestamps.get(key)
            if timestamp and (time.time() - timestamp) < CACHE_TTL_SECONDS:
                return _api_cache[key]
    return None

async def save_to_cache(key, value):
    async with _cache_lock:
        _api_cache[key] = value
        _cache_timestamps[key] = time.time()

# ==================== КОНФИГУРАЦИЯ ====================
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
OZON_PERFORMANCE_CLIENT_ID = os.getenv("OZON_PERFORMANCE_CLIENT_ID")
OZON_PERFORMANCE_CLIENT_SECRET = os.getenv("OZON_PERFORMANCE_CLIENT_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID_STR = os.getenv("ADMIN_CHAT_ID")
ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_STR) if ADMIN_CHAT_ID_STR and ADMIN_CHAT_ID_STR.isdigit() else 0
OZON_COMPANY_ID = os.getenv("OZON_COMPANY_ID")  # необязательный параметр

OZON_POSTING_FBO_URL = "https://api-seller.ozon.ru/v2/posting/fbo/list"
OZON_FINANCE_URL = "https://api-seller.ozon.ru/v3/finance/transaction/list"
MANAGERS_FILE = "managers.json"

# Состояния для диалогов (оставлены без изменений)
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
WAITING_DYNAMICS_SELECT = 12
WAITING_DYNAMICS_RANGE_START = 13
WAITING_DYNAMICS_RANGE_END = 14
WAITING_PRODUCT_DATE = 20
WAITING_PRODUCT_PERIOD_TYPE = 21
WAITING_PRODUCT_PERIOD_START = 22
WAITING_PRODUCT_PERIOD_END = 23
WAITING_PRODUCT_YEAR = 24
WAITING_PRODUCT_MONTH = 25
WAITING_PRODUCT_QUARTER = 26
WAITING_PRODUCT_YEAR_SELECT = 27
WAITING_PRODUCT_SELECT = 30
WAITING_PRODUCT_METRIC = 31
WAITING_PRODUCT_PERIOD_CHOICE = 32
WAITING_PRODUCT_SINGLE_YEAR = 33
WAITING_PRODUCT_RANGE_START = 34
WAITING_PRODUCT_RANGE_END = 35

# Состояния для раздела "Автоматические рассылки"
WAITING_AUTO_SCHEDULE = 40
WAITING_AUTO_YESTERDAY = 41
WAITING_AUTO_SILENCE_START = 42
WAITING_AUTO_SILENCE_END = 43

MOSCOW_TZ = datetime.timezone(datetime.timedelta(hours=3))

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
def mask_secret(value: Optional[str], visible_chars: int = 4) -> str:
    if not value:
        return "***"
    if len(value) <= visible_chars:
        return "***"
    return f"{value[:visible_chars]}***"

def validate_env_vars() -> bool:
    required = {
        "OZON_CLIENT_ID": OZON_CLIENT_ID,
        "OZON_API_KEY": OZON_API_KEY,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "ADMIN_CHAT_ID": ADMIN_CHAT_ID_STR,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        write_log(f"❌ Missing required env vars: {', '.join(missing)}")
        return False
    if not ADMIN_CHAT_ID_STR.isdigit():
        write_log("❌ ADMIN_CHAT_ID must be a numeric Telegram user ID")
        return False
    return True

def write_log(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"[{timestamp}] {message}"
    print(full_msg, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(full_msg + "\n")
    except Exception as e:
        print(f"⚠️ Не удалось записать лог: {e}")

def update_version_history(version: str, message: str) -> None:
    history = []
    if os.path.exists(VERSION_HISTORY_FILE):
        try:
            with open(VERSION_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except:
            history = []
    for entry in history:
        if entry.get("version") == version:
            write_log(f"ℹ️ Версия {version} уже зарегистрирована в истории.")
            return
    now = datetime.datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S")
    history.append({"version": version, "date": now, "message": message})
    try:
        with open(VERSION_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        write_log(f"✅ Добавлена запись в историю версий: {version} - {message}")
    except Exception as e:
        write_log(f"❌ Ошибка при записи истории версий: {e}")

async def load_settings() -> Dict:
    async with SETTINGS_LOCK:
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                write_log(f"⚠️ Ошибка загрузки настроек: {e}")
                return {}
        return {}

async def save_settings(settings: Dict):
    async with SETTINGS_LOCK:
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
            write_log("✅ Настройки сохранены.")
        except Exception as e:
            write_log(f"❌ Ошибка сохранения настроек: {e}")

def get_default_settings() -> Dict:
    return {
        "schedule_hours": [],
        "yesterday_report_hours": [],
        "silence_start": None,
        "silence_end": None,
        "last_yesterday_report_sent": None
    }

# ---------- РАБОТА С МЕНЕДЖЕРАМИ ----------
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
    moscow_tz = MOSCOW_TZ
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
    return datetime.datetime.now(MOSCOW_TZ).date()

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
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f"{callback_prefix}cancel")])
    return InlineKeyboardMarkup(keyboard)

def validate_date(date_str):
    try:
        date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        today = get_moscow_today()
        if date > today:
            return False, "❌ Дата не может быть в будущем"
        two_years_ago = today - datetime.timedelta(days=730)
        if date < two_years_ago:
            return False, "❌ Дата слишком старая (более 2 лет назад)"
        return True, date
    except ValueError:
        return False, "❌ Неверный формат даты. Используйте YYYY-MM-DD"

def validate_period(date_from, date_to):
    valid_from, from_date = validate_date(date_from)
    if not valid_from:
        return False, from_date
    valid_to, to_date = validate_date(date_to)
    if not valid_to:
        return False, to_date
    if from_date > to_date:
        return False, "❌ Начальная дата не может быть позже конечной"
    delta = (to_date - from_date).days
    if delta > 365:
        return False, "❌ Период не может быть больше года"
    return True, (from_date, to_date)

def get_current_time_msk():
    return datetime.datetime.now(MOSCOW_TZ)

# ---------- УТИЛИТЫ ДЛЯ ФОРМАТИРОВАНИЯ ----------
def fmt_num(val):
    return f"{val:,.2f}".replace(",", " ") if val else "0.00"

def fmt_int(val):
    return str(val) if val else "0"

def fmt_pct(val):
    if val is None:
        return "∞"
    if val > 0:
        return f"+{val:.1f}%"
    elif val < 0:
        return f"{val:.1f}%"
    else:
        return f"{val:.1f}%"

def calc_delta(current, previous):
    if previous == 0:
        return None
    try:
        return ((current - previous) / abs(previous)) * 100
    except:
        return None

def indicator(value_current, value_prev, better_is_higher):
    if value_prev is None or value_current is None:
        return ""
    if value_prev == 0:
        return "🟢" if value_current > 0 else ""
    delta = calc_delta(value_current, value_prev)
    if delta is None:
        return ""
    if better_is_higher:
        return "🟢" if delta > 0 else ("🔴" if delta < 0 else "")
    else:
        return "🟢" if delta < 0 else ("🔴" if delta > 0 else "")

def format_expense_comparison(expenses_current, expenses_prev, title):
    if not expenses_current and not expenses_prev:
        return f"🔹 *{title}*\nНет данных о расходах.\n"
    
    total_current = sum(expenses_current.values()) if expenses_current else 0
    total_prev = sum(expenses_prev.values()) if expenses_prev else 0
    total_indicator = indicator(total_current, total_prev, False)
    lines = [f"🔹 *{title}*"]
    lines.append(f"  Итого расходов: {fmt_num(total_current)} ₽ / {fmt_num(total_prev)} ₽ {total_indicator}")
    
    all_categories = set(expenses_current.keys()) | set(expenses_prev.keys())
    sorted_categories = sorted(all_categories, key=lambda x: expenses_current.get(x, 0), reverse=True)
    
    name_map = {
        "Комиссия Ozon": "Комиссия",
        "Оплата эквайринга": "Эквайринг",
        "Доставка покупателю": "Доставка покупателю",
        "Доставка и обработка возврата, отмены, невыкупа": "Доставка/возвраты",
        "Кросс-докинг": "Кросс-докинг",
        "Страхование товара от массовых повреждений": "Страхование",
        "Обеспечение материалами для упаковки товара": "Обеспечение упаковкой",
        "Упаковка товара партнёрами": "Упаковка",
        "Подписка Управление отзывами": "Подписка",
        "Оплата за клик": "Оплата за клик",
        "Получение возврата, отмены, невыкупа от покупателя": "Получение возвратов",
        "MarketplaceServiceItemDirectFlowLogistic": "Логистика прямая",
        "MarketplaceServiceItemRedistributionLastMileCourier": "Логистика последняя миля",
        "MarketplaceServiceItemReturnFlowLogistic": "Логистика возврат",
        "MarketplaceServiceItemDeliveryToHandoverPlaceOzon": "Доставка до ПВЗ",
        "MarketplaceRedistributionOfAcquiringOperation": "Эквайринг",
        "MarketplaceServiceItemRedistributionReturnsPVZ": "Обработка возвратов (ПВЗ)",
        "MarketplaceServiceItemPackageRedistribution": "Переупаковка",
        "MarketplaceServiceItemPackageMaterialsProvision": "Обеспечение упаковкой",
        "MarketplaceServiceItemProductReviewsManagementSubscription": "Подписка",
        "MarketplaceServiceItemRedistributionLastMilePVZ": "Логистика последняя миля (ПВЗ)",
        "MarketplaceServiceItemDirectFlowLogisticFBS": "Логистика прямая (FBS)",
        "MarketplaceServiceItemReturnFlowLogisticFBS": "Логистика возврат (FBS)",
        "ItemAgentServiceStarsMembership": "Звёздные товары",
        "MarketplaceServiceSellerReturnsCargoAssortment": "Обработка возвратов партнёрами",
        "MarketplaceServiceItemTemporaryStorageRedistribution": "Временное размещение",
        "MarketplaceServiceProductMovementFromWarehouse": "Вывоз до ПВЗ",
        "MarketplaceServiceItemDisposalDetailed": "Утилизация",
        "Звёздные товары": "Звёздные товары",
        "Временное размещение товара партнерами": "Временное размещение",
        "Обработка товара в составе грузоместа: Поштучная приёмка": "Поштучная приёмка",
        "Обработка товара в составе грузоместа на FBO": "Поштучная приёмка",
        "Подготовка товара к вывозу: Брак": "Подготовка к вывозу (брак)",
        "Вывоз товара со склада силами Ozon: Доставка до ПВЗ": "Вывоз до ПВЗ",
        "Вывоз товара со Склада силами Ozon: Доставка до ПВЗ": "Вывоз до ПВЗ",
        "Бронирование места и персонала для поставки с неполным составом в составе грузоместа": "Бронирование места",
        "Услуга по бронированию места и персонала для поставки с неполным составом в составе ГМ": "Бронирование места",
        "Обработка опознанных излишков в составе грузоместа": "Обработка излишков",
        "Услуга по обработке опознанных излишков в составе ГМ": "Обработка излишков",
        "Утилизация товара: Пролились/просыпались из-за упаковки": "Утилизация",
        "Потеря по вине Ozon на складе": "Потеря (склад)",
        "Потеря по вине Ozon в логистике": "Потеря (логистика)",
        "Вознаграждение за продажу": "Вознаграждение",
        "Возврат вознаграждения": "Возврат вознаграждения",
        "Программы партнёров": "Программы партнёров",
        "Баллы за скидки": "Баллы за скидки",
        "Выручка": "Выручка",
        "Возврат выручки": "Возврат выручки",
    }
    
    for category in sorted_categories:
        amount_cur = expenses_current.get(category, 0)
        amount_prev = expenses_prev.get(category, 0)
        ind = indicator(amount_cur, amount_prev, False)
        short_name = name_map.get(category, category)
        lines.append(f"    {short_name}: {fmt_num(amount_cur)} ₽ / {fmt_num(amount_prev)} ₽ {ind}")
    
    return "\n".join(lines)

def format_period_comparison_metrics(metrics_current, metrics_prev, period_name):
    cur_ordered_sum = metrics_current.get('ordered_sum', 0)
    cur_ordered_units = metrics_current.get('ordered_units', 0)
    cur_delivered_sum = metrics_current.get('delivered_sum', 0)
    cur_delivered_units = metrics_current.get('delivered_units', 0)
    cur_canceled_sum = metrics_current.get('canceled_sum', 0)
    cur_canceled_units = metrics_current.get('canceled_units', 0)
    cur_ad_expense = metrics_current.get('ad_expense', 0)
    cur_drr = metrics_current.get('drr')
    cur_eff_drr = metrics_current.get('effective_drr')
    cur_expenses = metrics_current.get('expenses', {})

    prev_ordered_sum = metrics_prev.get('ordered_sum', 0)
    prev_ordered_units = metrics_prev.get('ordered_units', 0)
    prev_delivered_sum = metrics_prev.get('delivered_sum', 0)
    prev_delivered_units = metrics_prev.get('delivered_units', 0)
    prev_canceled_sum = metrics_prev.get('canceled_sum', 0)
    prev_canceled_units = metrics_prev.get('canceled_units', 0)
    prev_ad_expense = metrics_prev.get('ad_expense', 0)
    prev_drr = metrics_prev.get('drr')
    prev_eff_drr = metrics_prev.get('effective_drr')
    prev_expenses = metrics_prev.get('expenses', {})

    cur_cancel_rate = (cur_canceled_units / cur_delivered_units * 100) if cur_delivered_units > 0 else None
    prev_cancel_rate = (prev_canceled_units / prev_delivered_units * 100) if prev_delivered_units > 0 else None

    ind_ordered_sum = indicator(cur_ordered_sum, prev_ordered_sum, True)
    ind_ordered_units = indicator(cur_ordered_units, prev_ordered_units, True)
    ind_delivered_sum = indicator(cur_delivered_sum, prev_delivered_sum, True)
    ind_delivered_units = indicator(cur_delivered_units, prev_delivered_units, True)
    ind_canceled_sum = indicator(cur_canceled_sum, prev_canceled_sum, False)
    ind_canceled_units = indicator(cur_canceled_units, prev_canceled_units, False)
    ind_cancel_rate = indicator(cur_cancel_rate, prev_cancel_rate, False)
    ind_ad_expense = indicator(cur_ad_expense, prev_ad_expense, False)
    ind_drr = indicator(cur_drr, prev_drr, False)
    ind_eff_drr = indicator(cur_eff_drr, prev_eff_drr, False)

    lines = []
    lines.append(f"📊 *Продажи за {period_name}*")
    lines.append("")

    lines.append(f"🛒 *Заказано*")
    lines.append(f"  На сумму: {fmt_num(cur_ordered_sum)} ₽ {ind_ordered_sum}")
    lines.append(f"  Штук: {fmt_int(cur_ordered_units)} {ind_ordered_units}")
    lines.append("vs предыдущий период:")
    lines.append(f"  На сумму: {fmt_num(prev_ordered_sum)} ₽")
    lines.append(f"  Штук: {fmt_int(prev_ordered_units)}")
    lines.append("")

    lines.append(f"📦 *Доставлено*")
    lines.append(f"  На сумму: {fmt_num(cur_delivered_sum)} ₽ {ind_delivered_sum}")
    lines.append(f"  Штук: {fmt_int(cur_delivered_units)} {ind_delivered_units}")
    lines.append("vs предыдущий период:")
    lines.append(f"  На сумму: {fmt_num(prev_delivered_sum)} ₽")
    lines.append(f"  Штук: {fmt_int(prev_delivered_units)}")
    lines.append("")

    lines.append(f"❌ *Отменено*")
    lines.append(f"  На сумму: {fmt_num(cur_canceled_sum)} ₽ {ind_canceled_sum}")
    lines.append(f"  Штук: {fmt_int(cur_canceled_units)} {ind_canceled_units}")
    cur_cancel_rate_text = f"{cur_cancel_rate:.2f}%" if cur_cancel_rate is not None else "∞"
    prev_cancel_rate_text = f"{prev_cancel_rate:.2f}%" if prev_cancel_rate is not None else "∞"
    lines.append(f"  Доля отмен: {cur_cancel_rate_text} {ind_cancel_rate}")
    lines.append("vs предыдущий период:")
    lines.append(f"  На сумму: {fmt_num(prev_canceled_sum)} ₽")
    lines.append(f"  Штук: {fmt_int(prev_canceled_units)}")
    lines.append(f"  Доля отмен: {prev_cancel_rate_text}")
    lines.append("")

    lines.append(f"📢 *Реклама*")
    lines.append(f"  Расходы: {fmt_num(cur_ad_expense)} ₽ {ind_ad_expense}")
    cur_drr_text = f"{cur_drr:.2f}%" if cur_drr is not None else "∞"
    cur_eff_drr_text = f"{cur_eff_drr:.2f}%" if cur_eff_drr is not None else "∞"
    prev_drr_text = f"{prev_drr:.2f}%" if prev_drr is not None else "∞"
    prev_eff_drr_text = f"{prev_eff_drr:.2f}%" if prev_eff_drr is not None else "∞"
    lines.append(f"  ДРР (общий): {cur_drr_text} {ind_drr}")
    lines.append(f"  ДРР (по доставленным): {cur_eff_drr_text} {ind_eff_drr}")
    lines.append("vs предыдущий период:")
    lines.append(f"  Расходы: {fmt_num(prev_ad_expense)} ₽")
    lines.append(f"  ДРР (общий): {prev_drr_text}")
    lines.append(f"  ДРР (по доставленным): {prev_eff_drr_text}")
    lines.append("")

    expense_block = format_expense_comparison(cur_expenses, prev_expenses, "Расходы за период")
    lines.append(expense_block)

    return "\n".join(lines)

# ==================== КЛАСС ДЛЯ РЕЙТ-ЛИМИТА ====================
class RateLimiter:
    def __init__(self, rate=RATE_LIMIT_REQUESTS_PER_SECOND):
        self.rate = rate
        self.lock = asyncio.Lock()
        self.last_request_time = 0

    async def acquire(self):
        async with self.lock:
            now = time.time()
            wait_time = (1.0 / self.rate) - (now - self.last_request_time)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            self.last_request_time = time.time()

# ==================== ИНИЦИАЛИЗАЦИЯ HTTP СЕССИИ ====================
async def init_http_session(app):
    global _http_session, _rate_limiter
    _rate_limiter = RateLimiter()
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    connector = aiohttp.TCPConnector(limit=50, limit_per_host=10, ttl_dns_cache=300)
    _http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    write_log(f"✅ HTTP-сессия aiohttp инициализирована (v{VERSION})")

async def close_http_session(app):
    global _http_session
    if _http_session:
        await _http_session.close()
        write_log("🔒 HTTP-сессия закрыта.")

# ==================== ФУНКЦИИ API С RETRY ====================
async def api_request_with_retry(url, headers, payload=None, method='POST'):
    global _http_session, _rate_limiter
    for attempt in range(API_RETRY_ATTEMPTS):
        try:
            await _rate_limiter.acquire()
            if method == 'POST':
                async with _http_session.post(url, headers=headers, json=payload) as resp:
                    if resp.status == 429:
                        wait_time = API_RETRY_DELAY * (2 ** attempt)
                        write_log(f"⚠️ Rate limit hit, waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            else:
                async with _http_session.get(url, headers=headers, params=payload) as resp:
                    if resp.status == 429:
                        wait_time = API_RETRY_DELAY * (2 ** attempt)
                        write_log(f"⚠️ Rate limit hit, waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
        except aiohttp.ClientResponseError as e:
            try:
                body = await e.response.text()
                write_log(f"❌ API error {e.status}: {e.message}, тело: {body[:1000]}")
            except Exception as read_err:
                write_log(f"❌ API error {e.status}: {e.message}, не удалось прочитать тело: {read_err}")
            if attempt == API_RETRY_ATTEMPTS - 1:
                raise
            write_log(f"⚠️ Request failed (attempt {attempt+1}/{API_RETRY_ATTEMPTS}), retrying...")
            await asyncio.sleep(API_RETRY_DELAY * (attempt + 1))
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == API_RETRY_ATTEMPTS - 1:
                write_log(f"❌ API request failed after {API_RETRY_ATTEMPTS} attempts: {e}")
                raise
            write_log(f"⚠️ Request failed (attempt {attempt+1}/{API_RETRY_ATTEMPTS}): {e}")
            await asyncio.sleep(API_RETRY_DELAY * (attempt + 1))
    raise Exception("API request failed after retries")
# ---------- ТОКЕН PERFORMANCE ----------
async def get_performance_token():
    if not OZON_PERFORMANCE_CLIENT_ID or not OZON_PERFORMANCE_CLIENT_SECRET:
        write_log("⚠️ OZON_PERFORMANCE_CLIENT_ID или CLIENT_SECRET не заданы!")
        return None
    url = "https://api-performance.ozon.ru/api/client/token"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    payload = {"client_id": OZON_PERFORMANCE_CLIENT_ID, "client_secret": OZON_PERFORMANCE_CLIENT_SECRET, "grant_type": "client_credentials"}
    try:
        token_data = await api_request_with_retry(url, headers, payload, method='POST')
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

# ---------- ОТГРУЗКИ ----------
async def fetch_postings(date_from, date_to, progress_callback=None):
    cache_key = f"fetch_postings_{date_from}_{date_to}"
    cached = await get_from_cache(cache_key)
    if cached is not None:
        return cached
    headers = {"Client-Id": OZON_CLIENT_ID, "Api-Key": OZON_API_KEY, "Content-Type": "application/json"}
    payload = {"date_from": date_from, "date_to": date_to, "status": "", "limit": 1000, "offset": 0}
    all_postings = []
    total_pages = None
    page = 0
    while True:
        page += 1
        try:
            data = await api_request_with_retry(OZON_POSTING_FBO_URL, headers, payload, method='POST')
            postings = data.get("result", [])
            if not postings:
                break
            all_postings.extend(postings)
            if total_pages is None and 'total' in data:
                total_pages = (data['total'] + payload['limit'] - 1) // payload['limit']
            if progress_callback:
                progress = min(100, int((page / (total_pages or 1)) * 100))
                await progress_callback(f"Загрузка отгрузок... {len(all_postings)} шт.", progress)
            if len(postings) < payload["limit"]:
                break
            payload["offset"] += payload["limit"]
        except Exception as e:
            write_log(f"❌ Ошибка получения отгрузок: {e}")
            break
    write_log(f"📦 Загружено отгрузок: {len(all_postings)} за {date_from}–{date_to}")
    await save_to_cache(cache_key, all_postings)
    return all_postings

# ---------- РЕКЛАМНЫЕ РАСХОДЫ ----------
async def fetch_advertising_expense_single(date_from, date_to):
    cache_key = f"fetch_advertising_expense_single_{date_from}_{date_to}"
    cached = await get_from_cache(cache_key)
    if cached is not None:
        return cached
    token = await get_performance_token()
    if not token:
        return 0.0
    url = "https://api-performance.ozon.ru/api/client/statistics/expense/json"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    params = {"dateFrom": date_from, "dateTo": date_to}
    try:
        data = await api_request_with_retry(url, headers, params, method='GET')
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
        await save_to_cache(cache_key, total_expense)
        return total_expense
    except Exception as e:
        write_log(f"❌ Ошибка получения рекламных расходов: {e}")
        return 0.0

async def fetch_advertising_expense(date_from, date_to, progress_callback=None):
    cache_key = f"fetch_advertising_expense_{date_from}_{date_to}"
    cached = await get_from_cache(cache_key)
    if cached is not None:
        return cached
    start_dt = datetime.datetime.strptime(date_from, "%Y-%m-%d")
    end_dt = datetime.datetime.strptime(date_to, "%Y-%m-%d")
    today = get_moscow_today()
    if start_dt.date() > today:
        return 0.0
    if end_dt.date() > today:
        end_dt = datetime.datetime.combine(today, datetime.time(23, 59, 59))
    delta = (end_dt - start_dt).days
    if delta <= API_MAX_DAYS_PER_REQUEST:
        result = await fetch_advertising_expense_single(date_from, end_dt.strftime("%Y-%m-%d"))
        await save_to_cache(cache_key, result)
        if progress_callback:
            await progress_callback("Реклама загружена", 100)
        return result
    total = 0.0
    months = []
    current = start_dt.replace(day=1)
    while current <= end_dt:
        month_start = current.strftime("%Y-%m-%d")
        next_month = current.replace(day=28) + datetime.timedelta(days=4)
        month_end = (next_month - datetime.timedelta(days=next_month.day)).strftime("%Y-%m-%d")
        if month_end > end_dt.strftime("%Y-%m-%d"):
            month_end = end_dt.strftime("%Y-%m-%d")
        if current.date() > today:
            break
        months.append((month_start, month_end))
        current = current.replace(day=28) + datetime.timedelta(days=4)
        current = current.replace(day=1)
    total_months = len(months)
    for i, (m_start, m_end) in enumerate(months):
        if progress_callback:
            progress = int((i / total_months) * 100) if total_months > 0 else 100
            await progress_callback(f"Загрузка рекламы {m_start}–{m_end}", progress)
        total += await fetch_advertising_expense_single(m_start, m_end)
    await save_to_cache(cache_key, total)
    if progress_callback:
        await progress_callback("Реклама загружена", 100)
    return total

# ---------- ФИНАНСОВЫЕ ТРАНЗАКЦИИ (ИСПРАВЛЕННАЯ ВЕРСИЯ) ----------
async def fetch_finance_transactions_parallel(date_from: str, date_to: str) -> List[Dict]:
    today_str = get_moscow_today().isoformat()
    if date_from > today_str:
        return []
    if date_to > today_str:
        date_to = today_str

    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }

    from_iso = date_from + "T00:00:00.000Z"
    to_iso = date_to + "T23:59:59.999Z"

    payload = {
        "filter": {"date": {"from": from_iso, "to": to_iso}},
        "page": 1,
        "page_size": 1000,
    }
    if OZON_COMPANY_ID:
        payload["company_id"] = OZON_COMPANY_ID
        write_log(f"ℹ️ Используем company_id: {OZON_COMPANY_ID}")

    write_log(f"🔍 Финансовый запрос: {payload}")

    try:
        first_data = await api_request_with_retry(OZON_FINANCE_URL, headers, payload, method='POST')
    except Exception as e:
        write_log(f"❌ Ошибка получения первой страницы финансов: {e}")
        return []

    result = first_data.get("result", {})
    total_items = result.get("total", 0)
    all_operations = result.get("operations", [])

    if total_items <= 1000:
        return all_operations

    total_pages = (total_items + 999) // 1000
    sem = asyncio.Semaphore(10)

    async def fetch_page(page_num: int):
        async with sem:
            p = {
                "filter": {"date": {"from": from_iso, "to": to_iso}},
                "page": page_num,
                "page_size": 1000,
            }
            if OZON_COMPANY_ID:
                p["company_id"] = OZON_COMPANY_ID
            try:
                data = await api_request_with_retry(OZON_FINANCE_URL, headers, p, method='POST')
                return data.get("result", {}).get("operations", [])
            except Exception as e:
                write_log(f"⚠️ Ошибка загрузки страницы {page_num}: {e}")
                return []

    tasks = [fetch_page(p) for p in range(2, total_pages + 1)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for res in results:
        if isinstance(res, list):
            all_operations.extend(res)
        elif isinstance(res, Exception):
            write_log(f"⚠️ Исключение при загрузке страницы: {res}")

    write_log(f"💰 Загружено финансовых транзакций: {len(all_operations)} за {date_from}–{date_to} (параллельно)")
    return all_operations

async def fetch_finance_transactions(date_from, date_to, progress_callback=None):
    cache_key = f"fetch_finance_transactions_{date_from}_{date_to}"
    cached = await get_from_cache(cache_key)
    if cached is not None:
        return cached
    start_dt = datetime.datetime.strptime(date_from, "%Y-%m-%d")
    end_dt = datetime.datetime.strptime(date_to, "%Y-%m-%d")
    today = get_moscow_today()
    if start_dt.date() > today:
        return []
    if end_dt.date() > today:
        end_dt = datetime.datetime.combine(today, datetime.time(23, 59, 59))
    delta = (end_dt - start_dt).days
    if delta <= API_MAX_DAYS_PER_REQUEST:
        result = await fetch_finance_transactions_parallel(date_from, end_dt.strftime("%Y-%m-%d"))
        await save_to_cache(cache_key, result)
        if progress_callback:
            await progress_callback("Финансы загружены", 100)
        return result
    all_transactions = []
    months = []
    current = start_dt.replace(day=1)
    while current <= end_dt:
        month_start = current.strftime("%Y-%m-%d")
        next_month = current.replace(day=28) + datetime.timedelta(days=4)
        month_end = (next_month - datetime.timedelta(days=next_month.day)).strftime("%Y-%m-%d")
        if month_end > end_dt.strftime("%Y-%m-%d"):
            month_end = end_dt.strftime("%Y-%m-%d")
        if current.date() > today:
            break
        months.append((month_start, month_end))
        current = current.replace(day=28) + datetime.timedelta(days=4)
        current = current.replace(day=1)
    total_months = len(months)
    for i, (m_start, m_end) in enumerate(months):
        if progress_callback:
            progress = int((i / total_months) * 100) if total_months > 0 else 100
            await progress_callback(f"Загрузка финансов {m_start}–{m_end}", progress)
        all_transactions.extend(await fetch_finance_transactions_parallel(m_start, m_end))
    write_log(f"💰 Всего загружено финансовых транзакций: {len(all_transactions)} за {date_from}–{date_to}")
    await save_to_cache(cache_key, all_transactions)
    if progress_callback:
        await progress_callback("Финансы загружены", 100)
    return all_transactions

# ---------- АГРЕГАЦИЯ ФИНАНСОВ ----------
def aggregate_finance_expenses(transactions):
    expense_by_type = {}
    for t in transactions:
        sale_comm = t.get("sale_commission", 0)
        if sale_comm < 0:
            expense_by_type["Комиссия Ozon"] = expense_by_type.get("Комиссия Ozon", 0) + abs(sale_comm)
        accruals = t.get("accruals_for_sale", 0)
        if accruals < 0:
            expense_by_type["Возврат выручки"] = expense_by_type.get("Возврат выручки", 0) + abs(accruals)
        delivery_charge = t.get("delivery_charge", 0)
        if delivery_charge < 0:
            expense_by_type["Доставка (отдельно)"] = expense_by_type.get("Доставка (отдельно)", 0) + abs(delivery_charge)
        return_delivery = t.get("return_delivery_charge", 0)
        if return_delivery < 0:
            expense_by_type["Возвратная доставка"] = expense_by_type.get("Возвратная доставка", 0) + abs(return_delivery)
        amount = t.get("amount", 0)
        op_type = t.get("operation_type_name", "Неизвестный тип")
        services = t.get("services", [])
        if services and isinstance(services, list):
            has_negative_service = False
            for service in services:
                service_name = service.get("name", "Неизвестная услуга")
                service_amount = service.get("price", 0)
                if service_amount == 0:
                    service_amount = service.get("amount", 0)
                if service_amount < 0:
                    has_negative_service = True
                    expense_by_type[service_name] = expense_by_type.get(service_name, 0) + abs(service_amount)
            if not has_negative_service and amount < 0:
                expense_by_type[op_type] = expense_by_type.get(op_type, 0) + abs(amount)
        else:
            if amount < 0:
                expense_by_type[op_type] = expense_by_type.get(op_type, 0) + abs(amount)
    return expense_by_type

# ---------- АГРЕГАЦИЯ ОТГРУЗОК ----------
def aggregate_postings(postings, date_from=None, date_to=None, time_limit=None, apply_limit_on_day=None):
    aggregated = {}
    for posting in postings:
        created_at = posting.get("created_at", "")
        if not created_at:
            continue
        try:
            dt = datetime.datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            dt_msk = dt.astimezone(MOSCOW_TZ)
        except:
            continue
        date_str = dt_msk.date().isoformat()
        if date_from and date_str < date_from:
            continue
        if date_to and date_str > date_to:
            continue
        if time_limit is not None and apply_limit_on_day is not None and date_str == apply_limit_on_day:
            if dt_msk.time() > time_limit:
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
        status = posting.get("status", "")
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
        if status in ("cancelled", "canceled"):
            aggregated[date_str]["canceled_units"] += total_units
            aggregated[date_str]["canceled_sum"] += total_sum
        elif status in ("delivered", "completed"):
            aggregated[date_str]["delivered_units"] += total_units
            aggregated[date_str]["delivered_sum"] += total_sum
    return aggregated

def aggregate_postings_multi(postings, ranges):
    results = {label: {"ordered_units": 0, "ordered_sum": 0.0, "delivered_units": 0, "delivered_sum": 0.0, "canceled_units": 0, "canceled_sum": 0.0}
               for label, _, _, _, _ in ranges}
    for posting in postings:
        created_at = posting.get("created_at", "")
        if not created_at:
            continue
        try:
            dt = datetime.datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            dt_msk = dt.astimezone(MOSCOW_TZ)
        except:
            continue
        date_str = dt_msk.date().isoformat()
        time = dt_msk.time()
        for label, date_from, date_to, time_limit, apply_day in ranges:
            if date_from and date_str < date_from:
                continue
            if date_to and date_str > date_to:
                continue
            if time_limit is not None and apply_day is not None and date_str == apply_day:
                if time > time_limit:
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
            status = posting.get("status", "")
            res = results[label]
            res["ordered_units"] += total_units
            res["ordered_sum"] += total_sum
            if status in ("cancelled", "canceled"):
                res["canceled_units"] += total_units
                res["canceled_sum"] += total_sum
            elif status in ("delivered", "completed"):
                res["delivered_units"] += total_units
                res["delivered_sum"] += total_sum
    return results

# ---------- ПАРАЛЛЕЛЬНАЯ ЗАГРУЗКА ДАННЫХ ----------
async def fetch_metrics_parallel(date_from, date_to, progress_callback=None):
    if progress_callback:
        await progress_callback("Начинаем загрузку данных...", 0)
    postings_task = fetch_postings(date_from, date_to)
    ad_task = fetch_advertising_expense(date_from, date_to)
    finance_task = fetch_finance_transactions(date_from, date_to)
    postings, ad_expense, transactions = await asyncio.gather(postings_task, ad_task, finance_task)
    if progress_callback:
        await progress_callback("Все данные загружены", 100)
    return postings, ad_expense, transactions

async def fetch_metrics_for_period_parallel(date_from, date_to, progress_callback=None):
    postings, ad_expense, transactions = await fetch_metrics_parallel(date_from, date_to, progress_callback)
    if progress_callback:
        await progress_callback("Агрегируем данные...", 50)
    agg = aggregate_postings(postings, date_from=date_from, date_to=date_to)
    total = {"ordered_units": 0, "ordered_sum": 0.0, "delivered_units": 0, "delivered_sum": 0.0, "canceled_units": 0, "canceled_sum": 0.0}
    for vals in agg.values():
        for key in total:
            total[key] += vals.get(key, 0)
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
    expenses = aggregate_finance_expenses(transactions)
    total["expenses"] = expenses
    if progress_callback:
        await progress_callback("Готово", 100)
    return total

# ---------- ТОВАРНАЯ АНАЛИТИКА ----------
def aggregate_products(postings, date_from=None, date_to=None, time_limit=None, apply_limit_on_day=None):
    product_stats = {}
    for posting in postings:
        created_at = posting.get("created_at", "")
        if not created_at:
            continue
        try:
            dt = datetime.datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            dt_msk = dt.astimezone(MOSCOW_TZ)
        except:
            continue
        date_str = dt_msk.date().isoformat()
        if date_from and date_str < date_from:
            continue
        if date_to and date_str > date_to:
            continue
        if time_limit is not None and apply_limit_on_day is not None and date_str == apply_limit_on_day:
            if dt_msk.time() > time_limit:
                continue
        status = posting.get("status", "")
        products = posting.get("products", [])
        for product in products:
            sku = str(product.get("sku", "0"))
            name = product.get("name", "Без названия")
            offer_id = product.get("offer_id", "")
            qty = int(product.get("quantity", 0))
            price_str = product.get("price", "0")
            try:
                price = float(price_str)
            except:
                price = 0.0
            if sku not in product_stats:
                product_stats[sku] = {
                    "name": name[:60],
                    "offer_id": offer_id,
                    "ordered_units": 0,
                    "ordered_sum": 0.0,
                    "delivered_units": 0,
                    "delivered_sum": 0.0,
                    "canceled_units": 0,
                    "canceled_sum": 0.0,
                    "order_count": 0,
                }
            stats = product_stats[sku]
            stats["ordered_units"] += qty
            stats["ordered_sum"] += price * qty
            stats["order_count"] += 1
            if status in ("delivered", "completed"):
                stats["delivered_units"] += qty
                stats["delivered_sum"] += price * qty
            elif status in ("cancelled", "canceled"):
                stats["canceled_units"] += qty
                stats["canceled_sum"] += price * qty
    return product_stats

def format_top_products(products, title, limit=15):
    if not products:
        return f"📦 {title}\n\n❌ Нет данных за указанный период."
    sorted_items = sorted(products.items(), key=lambda x: x[1]["ordered_sum"], reverse=True)[:limit]
    lines = [f"📦 {title}", ""]
    for idx, (sku, stats) in enumerate(sorted_items, 1):
        name = stats["name"][:40]
        offer_id = stats.get("offer_id", "")
        ordered_sum = f"{stats['ordered_sum']:,.2f}".replace(",", " ")
        ordered_units = stats["ordered_units"]
        delivered_sum = f"{stats['delivered_sum']:,.2f}".replace(",", " ")
        delivered_units = stats["delivered_units"]
        canceled_sum = f"{stats['canceled_sum']:,.2f}".replace(",", " ")
        canceled_units = stats["canceled_units"]
        avg_check = (stats["ordered_sum"] / stats["order_count"]) if stats["order_count"] > 0 else 0
        avg_check_str = f"{avg_check:,.2f}".replace(",", " ")
        lines.append(f"{idx}. SKU: {sku} | {name} | Арт: {offer_id}" if offer_id else f"{idx}. SKU: {sku} | {name}")
        lines.append(f"   🛒 Заказано: {ordered_sum} ₽ / {ordered_units} шт.")
        lines.append(f"   📦 Доставлено: {delivered_sum} ₽ / {delivered_units} шт.")
        lines.append(f"   ❌ Отменено: {canceled_sum} ₽ / {canceled_units} шт.")
        lines.append(f"   💰 Средний чек: {avg_check_str} ₽")
        lines.append("")
    return "\n".join(lines)

def format_products_summary(products):
    if not products:
        return "Нет данных"
    total_revenue = sum(p["ordered_sum"] for p in products.values())
    total_units = sum(p["ordered_units"] for p in products.values())
    total_orders = sum(p["order_count"] for p in products.values())
    avg_check = (total_revenue / total_orders) if total_orders > 0 else 0
    return (f"Сводка\n  Уникальных товаров: {len(products)}\n  Общая выручка: {total_revenue:,.2f} ₽\n"
            f"  Всего единиц: {total_units}\n  Всего заказов: {total_orders}\n  Средний чек: {avg_check:,.2f} ₽")

async def get_top_products_for_select(days=30):
    now = get_current_time_msk()
    end_date = now.date().isoformat()
    start_date = (now.date() - datetime.timedelta(days=days)).isoformat()
    postings = await fetch_postings(start_date, end_date)
    products = aggregate_products(postings, date_from=start_date, date_to=end_date,
                                  time_limit=now.time(), apply_limit_on_day=end_date)
    sorted_items = sorted(products.items(), key=lambda x: x[1]["ordered_sum"], reverse=True)[:20]
    return [(sku, stats) for sku, stats in sorted_items]

# ---------- ГРАФИКИ ----------
async def generate_product_chart_by_metric(sku, metric, years):
    data = {}
    for year in years:
        start_date = datetime.date(year, 1, 1).isoformat()
        end_date = datetime.date(year, 12, 31).isoformat()
        postings = await fetch_postings(start_date, end_date)
        monthly_data = {m: 0.0 for m in range(12)}
        order_counts = {m: 0 for m in range(12)}
        for posting in postings:
            created_at = posting.get("created_at", "")
            if not created_at:
                continue
            try:
                dt = datetime.datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                dt_msk = dt.astimezone(MOSCOW_TZ)
            except:
                continue
            if dt_msk.year != year:
                continue
            month_idx = dt_msk.month - 1
            products = posting.get("products", [])
            for product in products:
                if str(product.get("sku", "0")) != sku:
                    continue
                qty = int(product.get("quantity", 0))
                price_str = product.get("price", "0")
                try:
                    price = float(price_str)
                except:
                    price = 0.0
                status = posting.get("status", "")
                if metric == 'ordered_sum':
                    monthly_data[month_idx] += price * qty
                elif metric == 'ordered_units':
                    monthly_data[month_idx] += qty
                elif metric == 'delivered_sum' and status in ("delivered", "completed"):
                    monthly_data[month_idx] += price * qty
                elif metric == 'delivered_units' and status in ("delivered", "completed"):
                    monthly_data[month_idx] += qty
                elif metric == 'canceled_sum' and status in ("cancelled", "canceled"):
                    monthly_data[month_idx] += price * qty
                elif metric == 'canceled_units' and status in ("cancelled", "canceled"):
                    monthly_data[month_idx] += qty
                elif metric == 'avg_check':
                    monthly_data[month_idx] += price * qty
                    order_counts[month_idx] += 1
        if metric == 'avg_check':
            for m in range(12):
                if order_counts[m] > 0:
                    monthly_data[m] = monthly_data[m] / order_counts[m]
                else:
                    monthly_data[m] = 0.0
        data[year] = [monthly_data[i] for i in range(12)]
    if not any(any(v > 0 for v in vals) for vals in data.values()):
        return None
    fig, ax = plt.subplots(figsize=(10, 6))
    months = [datetime.date(2000, m, 1) for m in range(1, 13)]
    metric_labels = {'ordered_sum': 'Заказано (₽)', 'ordered_units': 'Заказано (шт.)',
                     'delivered_sum': 'Доставлено (₽)', 'delivered_units': 'Доставлено (шт.)',
                     'canceled_sum': 'Отменено (₽)', 'canceled_units': 'Отменено (шт.)',
                     'avg_check': 'Средний чек (₽)'}
    ylabel = metric_labels.get(metric, 'Значение')
    for year, values in data.items():
        ax.plot(months, values, marker='o', label=str(year), linewidth=2)
    ax.set_title(f"Динамика по товару (SKU: {sku})", fontsize=14)
    ax.set_xlabel("Месяц")
    ax.set_ylabel(ylabel)
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    ax.grid(True, linestyle='--', alpha=0.7)
    ax.legend()
    if metric in ['ordered_sum', 'delivered_sum', 'canceled_sum', 'avg_check']:
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'{int(x):,}'.replace(',', ' ')))
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100)
    buf.seek(0)
    plt.close(fig)
    return buf

async def get_monthly_delivered_sum(year):
    start_date = datetime.date(year, 1, 1).isoformat()
    end_date = datetime.date(year, 12, 31).isoformat()
    postings = await fetch_postings(start_date, end_date)
    daily_agg = aggregate_postings(postings, date_from=start_date, date_to=end_date)
    monthly = [0.0] * 12
    for date_str, vals in daily_agg.items():
        try:
            dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            month_idx = dt.month - 1
            monthly[month_idx] += vals.get("delivered_sum", 0.0)
        except:
            continue
    return monthly

async def generate_sales_chart(years_list):
    if not years_list:
        return None
    data = {}
    for year in years_list:
        data[year] = await get_monthly_delivered_sum(year)
    fig, ax = plt.subplots(figsize=(10, 6))
    months = [datetime.date(2000, m, 1) for m in range(1, 13)]
    for year, values in data.items():
        ax.plot(months, values, marker='o', label=str(year), linewidth=2)
    ax.set_title("Динамика доставленных заказов (сумма, руб.)", fontsize=14)
    ax.set_xlabel("Месяц")
    ax.set_ylabel("Сумма доставленных заказов, ₽")
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    ax.grid(True, linestyle='--', alpha=0.7)
    ax.legend()
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'{int(x):,}'.replace(',', ' ')))
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100)
    buf.seek(0)
    plt.close(fig)
    return buf

# ---------- ФОРМАТИРОВАНИЕ ОТЧЁТОВ ----------
def format_expense_block(expenses_by_type, title):
    if not expenses_by_type:
        return f"🔹 *{title}*\nНет данных о расходах.\n"
    total = sum(expenses_by_type.values())
    lines = [f"🔹 *{title}*", f"  *Итого расходов:* {total:,.2f} ₽"]
    name_map = {
        "Комиссия Ozon": "Комиссия",
        "Оплата эквайринга": "Эквайринг",
        "Доставка покупателю": "Доставка покупателю",
        "Доставка и обработка возврата, отмены, невыкупа": "Доставка/возвраты",
        "Кросс-докинг": "Кросс-докинг",
        "Страхование товара от массовых повреждений": "Страхование",
        "Обеспечение материалами для упаковки товара": "Обеспечение упаковкой",
        "Упаковка товара партнёрами": "Упаковка",
        "Подписка Управление отзывами": "Подписка",
        "Оплата за клик": "Оплата за клик",
        "Получение возврата, отмены, невыкупа от покупателя": "Получение возвратов",
        "MarketplaceServiceItemDirectFlowLogistic": "Логистика прямая",
        "MarketplaceServiceItemRedistributionLastMileCourier": "Логистика последняя миля",
        "MarketplaceServiceItemReturnFlowLogistic": "Логистика возврат",
        "MarketplaceServiceItemDeliveryToHandoverPlaceOzon": "Доставка до ПВЗ",
        "MarketplaceRedistributionOfAcquiringOperation": "Эквайринг",
        "MarketplaceServiceItemRedistributionReturnsPVZ": "Обработка возвратов (ПВЗ)",
        "MarketplaceServiceItemPackageRedistribution": "Переупаковка",
        "MarketplaceServiceItemPackageMaterialsProvision": "Обеспечение упаковкой",
        "MarketplaceServiceItemProductReviewsManagementSubscription": "Подписка",
        "MarketplaceServiceItemRedistributionLastMilePVZ": "Логистика последняя миля (ПВЗ)",
        "MarketplaceServiceItemDirectFlowLogisticFBS": "Логистика прямая (FBS)",
        "MarketplaceServiceItemReturnFlowLogisticFBS": "Логистика возврат (FBS)",
        "ItemAgentServiceStarsMembership": "Звёздные товары",
        "MarketplaceServiceSellerReturnsCargoAssortment": "Обработка возвратов партнёрами",
        "MarketplaceServiceItemTemporaryStorageRedistribution": "Временное размещение",
        "MarketplaceServiceProductMovementFromWarehouse": "Вывоз до ПВЗ",
        "MarketplaceServiceItemDisposalDetailed": "Утилизация",
        "Звёздные товары": "Звёздные товары",
        "Временное размещение товара партнерами": "Временное размещение",
        "Обработка товара в составе грузоместа: Поштучная приёмка": "Поштучная приёмка",
        "Обработка товара в составе грузоместа на FBO": "Поштучная приёмка",
        "Подготовка товара к вывозу: Брак": "Подготовка к вывозу (брак)",
        "Вывоз товара со склада силами Ozon: Доставка до ПВЗ": "Вывоз до ПВЗ",
        "Вывоз товара со Склада силами Ozon: Доставка до ПВЗ": "Вывоз до ПВЗ",
        "Бронирование места и персонала для поставки с неполным составом в составе грузоместа": "Бронирование места",
        "Услуга по бронированию места и персонала для поставки с неполным составом в составе ГМ": "Бронирование места",
        "Обработка опознанных излишков в составе грузоместа": "Обработка излишков",
        "Услуга по обработке опознанных излишков в составе ГМ": "Обработка излишков",
        "Утилизация товара: Пролились/просыпались из-за упаковки": "Утилизация",
        "Потеря по вине Ozon на складе": "Потеря (склад)",
        "Потеря по вине Ozon в логистике": "Потеря (логистика)",
        "Вознаграждение за продажу": "Вознаграждение",
        "Возврат вознаграждения": "Возврат вознаграждения",
        "Программы партнёров": "Программы партнёров",
        "Баллы за скидки": "Баллы за скидки",
        "Выручка": "Выручка",
        "Возврат выручки": "Возврат выручки",
    }
    sorted_items = sorted(expenses_by_type.items(), key=lambda x: x[1], reverse=True)
    for category, amount in sorted_items:
        short_name = name_map.get(category, category[:40])
        lines.append(f"    {short_name}: {amount:,.2f} ₽")
    return "\n".join(lines)

def format_single_metrics(metrics, title):
    if not metrics:
        return f"📊 *{title}*\n\n❌ Нет данных за указанный период."
    has_data = False
    for key, val in metrics.items():
        if key in ["drr", "effective_drr", "ad_expense", "expenses"]:
            continue
        if isinstance(val, (int, float)) and val != 0:
            has_data = True
            break
    if not has_data:
        return f"📊 *{title}*\n\n❌ Нет данных за указанный период."
    ad_expense = metrics.get("ad_expense", 0)
    drr = metrics.get("drr")
    eff_drr = metrics.get("effective_drr")
    drr_text = f"{drr:.2f}%" if drr is not None else "∞"
    eff_drr_text = f"{eff_drr:.2f}%" if eff_drr is not None else "∞"
    canceled_units = metrics.get('canceled_units', 0)
    delivered_units = metrics.get('delivered_units', 0)
    cancel_rate = (canceled_units / delivered_units * 100) if delivered_units > 0 else None
    cancel_rate_text = f"{cancel_rate:.2f}%" if cancel_rate is not None else "∞"
    main_text = (f"📊 *{title}*\n\n"
                 f"🛒 *Заказано*\n  На сумму: {metrics.get('ordered_sum', 0):,.2f} ₽\n  Штук: {metrics.get('ordered_units', 0)}\n\n"
                 f"📦 *Доставлено*\n  На сумму: {metrics.get('delivered_sum', 0):,.2f} ₽\n  Штук: {metrics.get('delivered_units', 0)}\n\n"
                 f"❌ *Отменено*\n  На сумму: {metrics.get('canceled_sum', 0):,.2f} ₽\n  Штук: {metrics.get('canceled_units', 0)}\n  Доля отмен: {cancel_rate_text}\n\n"
                 f"📢 *Реклама*\n  Расходы: {ad_expense:,.2f} ₽\n  ДРР (общий): {drr_text}\n  ДРР (по доставленным): {eff_drr_text}")
    expenses = metrics.get("expenses", {})
    if expenses:
        expense_block = format_expense_block(expenses, "Расходы за период")
        main_text += "\n\n" + expense_block
    return main_text

async def get_metrics_for_date(date_str, progress_callback=None):
    today = get_moscow_today()
    start = (today - datetime.timedelta(days=183)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    postings_task = fetch_postings(start, end, progress_callback)
    ad_task = fetch_advertising_expense(date_str, date_str, progress_callback)
    fin_task = fetch_finance_transactions(date_str, date_str, progress_callback)
    postings, ad_expense, transactions = await asyncio.gather(postings_task, ad_task, fin_task)
    if progress_callback:
        await progress_callback("Агрегируем данные...", 80)
    agg = aggregate_postings(postings, date_from=date_str, date_to=date_str)
    metrics = agg.get(date_str, {})
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
    expenses = aggregate_finance_expenses(transactions)
    metrics["expenses"] = expenses
    if progress_callback:
        await progress_callback("Готово", 100)
    return metrics

async def get_metrics_for_period(date_from, date_to, progress_callback=None):
    return await fetch_metrics_for_period_parallel(date_from, date_to, progress_callback)

# ---------- КОМБИНИРОВАННЫЙ ОТЧЁТ ----------
async def format_combined_metrics_with_deltas(include_yesterday=False, progress_callback=None):
    now = get_current_time_msk()
    today_date = now.date()
    current_time = now.time()
    today_str = today_date.isoformat()
    yesterday_date = today_date - datetime.timedelta(days=1)
    yesterday_str = yesterday_date.isoformat()

    current_month_start = today_date.replace(day=1)
    current_month_start_str = current_month_start.isoformat()
    current_month_end_str = today_str

    previous_month_start = (current_month_start - datetime.timedelta(days=1)).replace(day=1)
    previous_month_start_str = previous_month_start.isoformat()
    previous_month_end = current_month_start - datetime.timedelta(days=1)
    previous_month_end_str = previous_month_end.isoformat()

    days_passed = (today_date - current_month_start).days + 1
    prev_period_end = previous_month_start + datetime.timedelta(days=days_passed - 1)
    prev_period_end_str = prev_period_end.isoformat()

    if progress_callback:
        await progress_callback("Загрузка отгрузок за текущий месяц...", 10)
    postings_current_task = fetch_postings(current_month_start_str, current_month_end_str)
    postings_prev_task = fetch_postings(previous_month_start_str, previous_month_end_str)
    postings_current, postings_prev = await asyncio.gather(postings_current_task, postings_prev_task)
    if progress_callback:
        await progress_callback("Отгрузки загружены, агрегируем...", 30)

    ranges_current = [
        ("today", today_str, today_str, current_time, today_str),
        ("yesterday", yesterday_str, yesterday_str, current_time, yesterday_str),
        ("yesterday_full", yesterday_str, yesterday_str, None, None),
        ("month", current_month_start_str, current_month_end_str, current_time, today_str),
    ]
    ranges_prev = [
        ("prev_month", previous_month_start_str, prev_period_end_str, current_time, prev_period_end_str),
    ]

    agg_results_current = aggregate_postings_multi(postings_current, ranges_current)
    agg_results_prev = aggregate_postings_multi(postings_prev, ranges_prev)

    today_metrics = agg_results_current.get("today", {})
    yesterday_metrics = agg_results_current.get("yesterday", {})
    yesterday_full_metrics = agg_results_current.get("yesterday_full", {})
    month_metrics = agg_results_current.get("month", {})
    prev_month_metrics = agg_results_prev.get("prev_month", {})

    if progress_callback:
        await progress_callback("Загрузка рекламы и финансов...", 50)

    ad_today_task = fetch_advertising_expense(today_str, today_str)
    ad_yesterday_task = fetch_advertising_expense(yesterday_str, yesterday_str)
    ad_month_task = fetch_advertising_expense(current_month_start_str, today_str)
    ad_prev_task = fetch_advertising_expense(previous_month_start_str, prev_period_end_str)
    fin_today_task = fetch_finance_transactions(today_str, today_str)
    fin_month_task = fetch_finance_transactions(current_month_start_str, today_str)

    ad_today, ad_yesterday, ad_month, ad_prev_period, fin_today, fin_month = await asyncio.gather(
        ad_today_task, ad_yesterday_task, ad_month_task, ad_prev_task,
        fin_today_task, fin_month_task
    )
    if progress_callback:
        await progress_callback("Данные загружены, формируем отчёт...", 80)

    expenses_today = aggregate_finance_expenses(fin_today)
    expenses_month = aggregate_finance_expenses(fin_month)

    if ad_today > 0:
        expenses_today["Оплата за клик"] = ad_today
        expenses_today.pop("Реклама", None)
    if ad_month > 0:
        expenses_month["Оплата за клик"] = ad_month
        expenses_month.pop("Реклама", None)

    def calc_delta(current, previous):
        if previous == 0:
            return None
        try:
            return ((current - previous) / abs(previous)) * 100
        except:
            return None

    d_ord_sum = calc_delta(today_metrics.get("ordered_sum", 0), yesterday_metrics.get("ordered_sum", 0))
    d_ord_units = calc_delta(today_metrics.get("ordered_units", 0), yesterday_metrics.get("ordered_units", 0))
    d_ad = calc_delta(ad_today, ad_yesterday)

    d_ord_sum_m = calc_delta(month_metrics.get("ordered_sum", 0), prev_month_metrics.get("ordered_sum", 0))
    d_ord_units_m = calc_delta(month_metrics.get("ordered_units", 0), prev_month_metrics.get("ordered_units", 0))
    d_del_sum_m = calc_delta(month_metrics.get("delivered_sum", 0), prev_month_metrics.get("delivered_sum", 0))
    d_del_units_m = calc_delta(month_metrics.get("delivered_units", 0), prev_month_metrics.get("delivered_units", 0))
    d_can_sum_m = calc_delta(month_metrics.get("canceled_sum", 0), prev_month_metrics.get("canceled_sum", 0))
    d_can_units_m = calc_delta(month_metrics.get("canceled_units", 0), prev_month_metrics.get("canceled_units", 0))
    d_ad_m = calc_delta(ad_month, ad_prev_period)

    cancel_rate_today = (today_metrics.get("canceled_units", 0) / today_metrics.get("delivered_units", 0) * 100) if today_metrics.get("delivered_units", 0) > 0 else None
    cancel_rate_month = (month_metrics["canceled_units"] / month_metrics["delivered_units"] * 100) if month_metrics["delivered_units"] > 0 else None
    cancel_rate_prev = (prev_month_metrics["canceled_units"] / prev_month_metrics["delivered_units"] * 100) if prev_month_metrics["delivered_units"] > 0 else None

    def format_today_block():
        ordered_sum = fmt_num(today_metrics.get("ordered_sum", 0))
        ordered_units = fmt_int(today_metrics.get("ordered_units", 0))
        canceled_sum = fmt_num(today_metrics.get("canceled_sum", 0))
        canceled_units = fmt_int(today_metrics.get("canceled_units", 0))

        delta_ord_sum = fmt_pct(d_ord_sum)
        delta_ord_units = fmt_pct(d_ord_units)
        delta_can_sum = fmt_pct(calc_delta(today_metrics.get("canceled_sum", 0), yesterday_metrics.get("canceled_sum", 0)))
        delta_can_units = fmt_pct(calc_delta(today_metrics.get("canceled_units", 0), yesterday_metrics.get("canceled_units", 0)))

        cancel_rate_text = f"{cancel_rate_today:.2f}%" if cancel_rate_today is not None else "∞"

        return (
            f"🔹 *Сегодня (на {now.strftime('%H:%M')} МСК)*\n"
            f"  🛒 Заказано: \n  {ordered_sum} ₽ / {ordered_units} шт.\n"
            f"    vs Вчера: \n  {delta_ord_sum} ₽ / {delta_ord_units} шт.\n\n"
            f"  ❌ Отменено: \n  {canceled_sum} ₽ / {canceled_units} шт.\n"
            f"    vs Вчера: \n  {delta_can_sum} ₽ / {delta_can_units} шт.\n"
            f"  Доля отмен: {cancel_rate_text}\n"
        )

    def format_month_block():
        ordered_sum = fmt_num(month_metrics.get("ordered_sum", 0))
        ordered_units = fmt_int(month_metrics.get("ordered_units", 0))
        delivered_sum = fmt_num(month_metrics.get("delivered_sum", 0))
        delivered_units = fmt_int(month_metrics.get("delivered_units", 0))
        canceled_sum = fmt_num(month_metrics.get("canceled_sum", 0))
        canceled_units = fmt_int(month_metrics.get("canceled_units", 0))
        ad_expense = fmt_num(ad_month)
        ad_prev = fmt_num(ad_prev_period)

        revenue = month_metrics.get("ordered_sum", 0)
        drr = (ad_month / revenue * 100) if revenue > 0 else None
        delivered_revenue = month_metrics.get("delivered_sum", 0)
        eff_drr = (ad_month / delivered_revenue * 100) if delivered_revenue > 0 else None

        prev_rev = prev_month_metrics.get("ordered_sum", 0)
        prev_del_rev = prev_month_metrics.get("delivered_sum", 0)
        prev_drr_val = (ad_prev_period / prev_rev * 100) if prev_rev > 0 else None
        prev_eff_drr_val = (ad_prev_period / prev_del_rev * 100) if prev_del_rev > 0 else None

        drr_str = f"{drr:.2f}%" if drr is not None else "∞"
        eff_drr_str = f"{eff_drr:.2f}%" if eff_drr is not None else "∞"
        prev_drr_str = f"{prev_drr_val:.2f}%" if prev_drr_val is not None else "∞"
        prev_eff_drr_str = f"{prev_eff_drr_val:.2f}%" if prev_eff_drr_val is not None else "∞"

        delta_ord_sum_m = fmt_pct(d_ord_sum_m)
        delta_ord_units_m = fmt_pct(d_ord_units_m)
        delta_del_sum_m = fmt_pct(d_del_sum_m)
        delta_del_units_m = fmt_pct(d_del_units_m)
        delta_can_sum_m = fmt_pct(d_can_sum_m)
        delta_can_units_m = fmt_pct(d_can_units_m)

        cancel_rate_month_text = f"{cancel_rate_month:.2f}%" if cancel_rate_month is not None else "∞"
        cancel_rate_prev_text = f"{cancel_rate_prev:.2f}%" if cancel_rate_prev is not None else "∞"
        cancel_rate_delta = calc_delta(cancel_rate_month if cancel_rate_month is not None else 0,
                                       cancel_rate_prev if cancel_rate_prev is not None else 0)
        cancel_rate_delta_text = fmt_pct(cancel_rate_delta)

        return (
            f"🔹 *Текущий месяц*\n"
            f"  🛒 Заказано: \n  {ordered_sum} ₽ / {ordered_units} шт.\n"
            f"    vs предыдущий месяц: \n  {delta_ord_sum_m} ₽ / {delta_ord_units_m} шт.\n\n"
            f"  📦 Доставлено: \n  {delivered_sum} ₽ / {delivered_units} шт.\n"
            f"    vs предыдущий месяц: \n  {delta_del_sum_m} ₽ / {delta_del_units_m} шт.\n\n"
            f"  ❌ Отменено: \n  {canceled_sum} ₽ / {canceled_units} шт.\n"
            f"    vs предыдущий месяц: \n  {delta_can_sum_m} ₽ / {delta_can_units_m} шт.\n"
            f"  Доля отмен: {cancel_rate_month_text} | vs предыдущий месяц: {cancel_rate_prev_text} ({cancel_rate_delta_text})\n\n"
            f"  📢 Реклама: \n  {ad_expense} ₽ | vs предыдущий месяц: {ad_prev} ₽\n"
            f"  ДРР (общий): {drr_str} | vs предыдущий месяц: {prev_drr_str}\n"
            f"  ДРР (по доставленным): {eff_drr_str} | vs предыдущий месяц: {prev_eff_drr_str}"
        )

    parts = []
    parts.append(format_today_block())
    parts.append(format_month_block())

    if include_yesterday:
        def format_yesterday_block():
            yesterday_full = agg_results_current.get("yesterday_full", {})
            ad_yesterday_full = ad_yesterday
            ordered_sum = fmt_num(yesterday_full.get("ordered_sum", 0))
            ordered_units = fmt_int(yesterday_full.get("ordered_units", 0))
            delivered_sum = fmt_num(yesterday_full.get("delivered_sum", 0))
            delivered_units = fmt_int(yesterday_full.get("delivered_units", 0))
            canceled_sum = fmt_num(yesterday_full.get("canceled_sum", 0))
            canceled_units = fmt_int(yesterday_full.get("canceled_units", 0))
            ad_expense = fmt_num(ad_yesterday_full)
            revenue = yesterday_full.get("ordered_sum", 0)
            drr = (ad_yesterday_full / revenue * 100) if revenue > 0 else None
            drr_str = f"{drr:.2f}%" if drr is not None else "∞"
            delivered_revenue = yesterday_full.get("delivered_sum", 0)
            eff_drr = (ad_yesterday_full / delivered_revenue * 100) if delivered_revenue > 0 else None
            eff_drr_str = f"{eff_drr:.2f}%" if eff_drr is not None else "∞"
            cancel_rate = (yesterday_full.get("canceled_units", 0) / yesterday_full.get("delivered_units", 0) * 100) if yesterday_full.get("delivered_units", 0) > 0 else None
            cancel_rate_text = f"{cancel_rate:.2f}%" if cancel_rate is not None else "∞"
            return (
                f"🔹 *Вчера (полный день)*\n"
                f"  🛒 Заказано: {ordered_sum} ₽ / {ordered_units} шт.\n"
                f"  📦 Доставлено: {delivered_sum} ₽ / {delivered_units} шт.\n"
                f"  ❌ Отменено: {canceled_sum} ₽ / {canceled_units} шт., доля: {cancel_rate_text}\n"
                f"  📢 Реклама: {ad_expense} ₽, ДРР (общий): {drr_str}, ДРР (по доставленным): {eff_drr_str}"
            )
        parts.append(format_yesterday_block())

    parts.append(format_expense_block(expenses_today, "Расходы сегодня"))
    parts.append(format_expense_block(expenses_month, "Расходы за текущий месяц"))

    if progress_callback:
        await progress_callback("Готово", 100)
    return "📊 *Продажи за сегодня*\n\n\n" + "\n\n".join(parts)

# ---------- ТОВАРНЫЕ ОТЧЁТЫ (асинхронные) ----------
async def get_product_data_for_date(date_str):
    today = get_moscow_today()
    start = (today - datetime.timedelta(days=183)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    postings = await fetch_postings(start, end)
    products = aggregate_products(postings, date_from=date_str, date_to=date_str)
    return products

async def get_product_data_for_period(date_from, date_to):
    postings = await fetch_postings(date_from, date_to)
    products = aggregate_products(postings, date_from=date_from, date_to=date_to)
    return products

async def get_product_data_today():
    now = get_current_time_msk()
    today_str = now.date().isoformat()
    postings = await fetch_postings(today_str, today_str)
    products = aggregate_products(postings, date_from=today_str, date_to=today_str,
                                  time_limit=now.time(), apply_limit_on_day=today_str)
    return products

async def get_product_data_month():
    now = get_current_time_msk()
    today_date = now.date()
    current_month_start = today_date.replace(day=1).isoformat()
    today_str = today_date.isoformat()
    postings = await fetch_postings(current_month_start, today_str)
    products = aggregate_products(postings, date_from=current_month_start, date_to=today_str,
                                  time_limit=now.time(), apply_limit_on_day=today_str)
    return products

async def get_product_data_prev_month():
    now = get_current_time_msk()
    today_date = now.date()
    current_month_start = today_date.replace(day=1)
    previous_month_start = (current_month_start - datetime.timedelta(days=1)).replace(day=1)
    days_passed = (today_date - current_month_start).days + 1
    prev_period_end = previous_month_start + datetime.timedelta(days=days_passed - 1)
    prev_start_str = previous_month_start.isoformat()
    prev_end_str = prev_period_end.isoformat()
    postings = await fetch_postings(prev_start_str, prev_end_str)
    products = aggregate_products(postings, date_from=prev_start_str, date_to=prev_end_str,
                                  time_limit=now.time(), apply_limit_on_day=prev_end_str)
    return products

async def format_product_combined():
    products_today, products_month, products_prev_month = await asyncio.gather(
        get_product_data_today(),
        get_product_data_month(),
        get_product_data_prev_month()
    )

    parts = []
    parts.append(format_top_products(products_today, "Топ товаров за сегодня", limit=15))
    parts.append("")
    parts.append(format_top_products(products_month, "Топ товаров за текущий месяц (аналог. период)", limit=15))
    if products_prev_month:
        parts.append("")
        parts.append("Сравнение с предыдущим месяцем (аналог. период)")
        total_rev_current = sum(p["ordered_sum"] for p in products_month.values())
        total_rev_prev = sum(p["ordered_sum"] for p in products_prev_month.values())
        total_units_current = sum(p["ordered_units"] for p in products_month.values())
        total_units_prev = sum(p["ordered_units"] for p in products_prev_month.values())
        delta_rev = ((total_rev_current - total_rev_prev) / total_rev_prev * 100) if total_rev_prev > 0 else None
        delta_units = ((total_units_current - total_units_prev) / total_units_prev * 100) if total_units_prev > 0 else None
        parts.append(f"  Выручка: {total_rev_current:,.2f} ₽ vs {total_rev_prev:,.2f} ₽ (Δ {delta_rev:.1f}%)" if delta_rev is not None else "  Выручка: нет данных")
        parts.append(f"  Единиц: {total_units_current} vs {total_units_prev} (Δ {delta_units:.1f}%)" if delta_units is not None else "  Единиц: нет данных")

    return "📦 Отчёт по товарам\n\n\n" + "\n\n".join(parts)

# ---------- КОМАНДЫ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    if is_admin(chat_id):
        name = user.first_name if user.first_name else ""
        greeting = get_greeting(name)
        await update.message.reply_text(
            f"{greeting}\n\n🤖 Версия бота: {VERSION}",
            reply_markup=main_admin_keyboard()
        )
    elif is_manager(chat_id):
        manager = get_manager_info(chat_id)
        name = manager.get("first_name") if manager and manager.get("first_name") else user.first_name or ""
        greeting = get_greeting(name)
        await update.message.reply_text(
            f"{greeting}\n\n🤖 Версия бота: {VERSION}",
            reply_markup=main_user_keyboard()
        )
    else:
        await update.message.reply_text("❌ Нет доступа! Обратитесь к администратору.", reply_markup=ReplyKeyboardRemove())

async def top_products_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not has_access(chat_id):
        await update.message.reply_text("❌ Нет доступа! Обратитесь к администратору.")
        return
    now = get_current_time_msk()
    today_date = now.date()
    month_start = today_date.replace(day=1).isoformat()
    today_str = today_date.isoformat()
    postings = await fetch_postings(month_start, today_str)
    products = aggregate_products(postings, date_from=month_start, date_to=today_str,
                                  time_limit=now.time(), apply_limit_on_day=today_str)
    if not products:
        await update.message.reply_text("❌ Нет данных о товарах за текущий месяц.")
        return
    sorted_items = sorted(products.items(), key=lambda x: x[1]["ordered_sum"], reverse=True)[:10]
    lines = ["🏆 <b>ТОП-10 ТОВАРОВ ЗА ТЕКУЩИЙ МЕСЯЦ</b>\n"]
    for i, (sku, stats) in enumerate(sorted_items, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        name = stats.get("name", "Без названия")[:40]
        offer_id = stats.get("offer_id", "")
        revenue = stats["ordered_sum"]
        units = stats["ordered_units"]
        line = f"{medal} <b>{name}</b>"
        if offer_id:
            line += f" (Арт: {offer_id})"
        line += f"\n   Выручка: {revenue:,.0f} ₽, шт: {units}\n"
        lines.append(line)
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')

async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not has_access(chat_id):
        await update.message.reply_text("❌ Нет доступа.")
        return
    await update.message.reply_text(f"🤖 Версия бота: {VERSION}")

# ---------- КЛАВИАТУРЫ ДЛЯ МЕНЮ ----------
def main_admin_keyboard():
    buttons = [
        [KeyboardButton("📊 Отчёт по продажам")],
        [KeyboardButton("📦 Отчёт по товарам")],
        [KeyboardButton("🔔 Автоматические рассылки")],
        [KeyboardButton("⚙️ Администрирование")],
        [KeyboardButton("📖 Справка")]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def main_user_keyboard():
    buttons = [
        [KeyboardButton("📊 Отчёт по продажам")],
        [KeyboardButton("📦 Отчёт по товарам")],
        [KeyboardButton("🔔 Автоматические рассылки")],
        [KeyboardButton("📖 Справка")]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def sales_reports_keyboard():
    buttons = [
        [KeyboardButton("📅 Продажи за сегодня")],
        [KeyboardButton("📆 Выбрать дату")],
        [KeyboardButton("📊 Выбрать период")],
        [KeyboardButton("📈 Динамика продаж")],
        [KeyboardButton("🔙 Назад")]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def products_reports_keyboard():
    buttons = [
        [KeyboardButton("📅 Топ товаров за сегодня")],
        [KeyboardButton("📆 Выбрать дату (товары)")],
        [KeyboardButton("📊 Выбрать период (товары)")],
        [KeyboardButton("📈 Динамика по товару")],
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

# ---------- ОБРАБОТЧИКИ МЕНЮ ----------
async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if text == "📊 Отчёт по продажам":
        if not has_access(chat_id):
            await update.message.reply_text("❌ Нет доступа! Обратитесь к администратору.")
            return
        await update.message.reply_text("Выберите тип отчёта по продажам:", reply_markup=sales_reports_keyboard())
        return

    if text == "📦 Отчёт по товарам":
        if not has_access(chat_id):
            await update.message.reply_text("❌ Нет доступа! Обратитесь к администратору.")
            return
        await update.message.reply_text("Выберите тип отчёта по товарам:", reply_markup=products_reports_keyboard())
        return

    if text == "🔔 Автоматические рассылки":
        if not has_access(chat_id):
            await update.message.reply_text("❌ Нет доступа! Обратитесь к администратору.")
            return
        await auto_reports_menu(update, context)
        return

    if text == "⚙️ Администрирование":
        if not is_admin(chat_id):
            await update.message.reply_text("⛔ Только для администратора.")
            return
        await update.message.reply_text("Управление менеджерами:", reply_markup=admin_keyboard())
        return

    if text == "📖 Справка":
        if is_admin(chat_id):
            help_text = (
                "📖 *Справка для администратора*\n\n"
                "🔹 *Основные функции*\n"
                "• 📊 Отчёт по продажам – актуальная сводка по продажам за сегодня и текущий месяц.\n"
                "• 📦 Отчёт по товарам – топ товаров по выручке за сегодня и текущий месяц.\n"
                "• 📆 Выбрать дату – просмотр данных за конкретный день (продажи или товары).\n"
                "• 📊 Выбрать период – гибкий выбор отчётного периода (месяц, квартал, год, произвольный).\n"
                "• 📈 Динамика продаж – график доставленных заказов по месяцам за выбранный год (или несколько лет).\n"
                "• 📈 Динамика по товару – график продаж конкретного товара по месяцам.\n"
                "• ⚙️ Администрирование – управление доступом менеджеров.\n"
                "• 🔔 Автоматические рассылки – настройка времени и содержания автоматических отчётов.\n\n"
                "🔹 *Управление менеджерами*\n"
                "• ➕ Добавить менеджера – введите Telegram ID или @username пользователя, затем номер телефона (или '-' для пропуска).\n"
                "• ➖ Удалить менеджера – введите Telegram ID пользователя.\n"
                "• 📋 Список менеджеров – просмотр всех добавленных пользователей (ID, username, имя, телефон).\n\n"
                "🔹 *Автоматические отчёты*\n"
                "• Настройка времени рассылок – задаются часы (01:00..24:00), в которые разрешена отправка.\n"
                "• Добавление отчёта за Вчера – из разрешённых часов выбираются те, в которые будет отправляться отчёт с блоком «Вчера».\n"
                "• Режим тишины – задаётся интервал, в течение которого рассылки не отправляются.\n"
                "• Отправить сейчас – принудительная отправка отчёта за вчера всем пользователям.\n\n"
                "🔹 *Метрики*\n"
                "• 🛒 Заказано – сумма и количество всех заказов.\n"
                "• 📦 Доставлено – сумма и количество доставленных заказов.\n"
                "• ❌ Отменено – сумма и количество отменённых заказов.\n"
                "• 📢 Реклама – расходы на рекламу, ДРР (общий) и ДРР (по доставленным).\n"
                "• 💰 Расходы (финансовые) – детальная разбивка: комиссии, логистика, эквайринг, кросс-докинг, хранение, возвраты и др.\n\n"
                "🔹 *Сравнение динамики*\n"
                "• Для «Сегодня» – сравнение с аналогичным временем вчера.\n"
                "• Для «Текущий месяц» – сравнение с аналогичным периодом предыдущего месяца (с учётом времени).\n\n"
                "🔹 *Часовой пояс*\n"
                "• Все расчёты ведутся по московскому времени (МСК, UTC+3).\n\n"
                f"🤖 Версия бота: {VERSION}"
            )
        else:
            help_text = (
                "📖 *Справка для менеджера*\n\n"
                "🔹 *Основные функции*\n"
                "• 📊 Отчёт по продажам – актуальная сводка по продажам за сегодня и текущий месяц.\n"
                "• 📦 Отчёт по товарам – топ товаров по выручке за сегодня и текущий месяц.\n"
                "• 📆 Выбрать дату – просмотр данных за конкретный день (продажи или товары).\n"
                "• 📊 Выбрать период – гибкий выбор отчётного периода (месяц, квартал, год, произвольный).\n"
                "• 📈 Динамика продаж – график доставленных заказов по месяцам за выбранный год (или несколько лет).\n"
                "• 📈 Динамика по товару – график продаж конкретного товара по месяцам.\n"
                "• 🔔 Автоматические рассылки – настройка времени и содержания автоматических отчётов.\n\n"
                "🔹 *Автоматические отчёты*\n"
                "• Настройка времени рассылок – задаются часы (01:00..24:00), в которые разрешена отправка.\n"
                "• Добавление отчёта за Вчера – из разрешённых часов выбираются те, в которые будет отправляться отчёт с блоком «Вчера».\n"
                "• Режим тишины – задаётся интервал, в течение которого рассылки не отправляются.\n"
                "• Отправить сейчас – принудительная отправка отчёта за вчера всем пользователям.\n\n"
                "🔹 *Метрики*\n"
                "• 🛒 Заказано – сумма и количество всех заказов.\n"
                "• 📦 Доставлено – сумма и количество доставленных заказов.\n"
                "• ❌ Отменено – сумма и количество отменённых заказов.\n"
                "• 📢 Реклама – расходы на рекламу, ДРР (общий) и ДРР (по доставленным).\n"
                "• 💰 Расходы (финансовые) – детальная разбивка: комиссии, логистика, эквайринг, кросс-докинг, хранение, возвраты и др.\n\n"
                "🔹 *Сравнение динамики*\n"
                "• Для «Сегодня» – сравнение с аналогичным временем вчера.\n"
                "• Для «Текущий месяц» – сравнение с аналогичным периодом предыдущего месяца (с учётом времени).\n\n"
                "🔹 *Часовой пояс*\n"
                "• Все расчёты ведутся по московскому времени (МСК, UTC+3).\n\n"
                f"🤖 Версия бота: {VERSION}"
            )
        await update.message.reply_text(help_text, parse_mode="Markdown")
        return

    await update.message.reply_text("Используйте кнопки меню.")

async def handle_sales_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if text == "🔙 Назад":
        if is_admin(chat_id):
            await update.message.reply_text(
                f"Главное меню\n\n🤖 Версия бота: {VERSION}",
                reply_markup=main_admin_keyboard()
            )
        else:
            await update.message.reply_text(
                f"Главное меню\n\n🤖 Версия бота: {VERSION}",
                reply_markup=main_user_keyboard()
            )
        return

    if not has_access(chat_id):
        await update.message.reply_text("❌ Нет доступа! Обратитесь к администратору.")
        return

    if text == "📅 Продажи за сегодня":
        progress_msg = await update.message.reply_text("⏳ Загружаю данные за сегодня...")
        report = await format_combined_metrics_with_deltas(include_yesterday=False, progress_callback=None)
        await progress_msg.delete()
        await update.message.reply_text(report, parse_mode="Markdown")
    elif text == "📆 Выбрать дату":
        now = get_moscow_today()
        keyboard = create_calendar(now.year, now.month, "date_")
        await update.message.reply_text("Выберите дату (продажи):", reply_markup=keyboard)
        return WAITING_DATE_SINGLE
    elif text == "📊 Выбрать период":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗓️ По месяцам", callback_data="period_month")],
            [InlineKeyboardButton("📅 По кварталам", callback_data="period_quarter")],
            [InlineKeyboardButton("📆 По годам", callback_data="period_year")],
            [InlineKeyboardButton("📊 Произвольный период", callback_data="period_custom")],
            [InlineKeyboardButton("🔙 Назад", callback_data="period_cancel")]
        ])
        await update.message.reply_text("Выберите тип периода:", reply_markup=keyboard)
        return WAITING_PERIOD_TYPE
    elif text == "📈 Динамика продаж":
        current_year = get_moscow_today().year
        buttons = [
            [InlineKeyboardButton("📅 Текущий год", callback_data="dynamics_current")],
            [InlineKeyboardButton("📆 Выбрать год", callback_data="dynamics_select")],
            [InlineKeyboardButton("📊 Диапазон лет", callback_data="dynamics_range")],
            [InlineKeyboardButton("🔙 Назад", callback_data="dynamics_cancel")]
        ]
        await update.message.reply_text(
            "Выберите вариант для построения графика:\n"
            "• Текущий год – сразу покажет динамику за текущий год.\n"
            "• Выбрать год – покажет список годов (последние 10).\n"
            "• Диапазон лет – введите начальный и конечный год.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return WAITING_DYNAMICS_SELECT
    else:
        await update.message.reply_text("Неизвестная команда.")

async def handle_products_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if text == "🔙 Назад":
        if is_admin(chat_id):
            await update.message.reply_text(
                f"Главное меню\n\n🤖 Версия бота: {VERSION}",
                reply_markup=main_admin_keyboard()
            )
        else:
            await update.message.reply_text(
                f"Главное меню\n\n🤖 Версия бота: {VERSION}",
                reply_markup=main_user_keyboard()
            )
        return

    if not has_access(chat_id):
        await update.message.reply_text("❌ Нет доступа! Обратитесь к администратору.")
        return

    if text == "📅 Топ товаров за сегодня":
        progress_msg = await update.message.reply_text("⏳ Загружаю данные...")
        report = await format_product_combined()
        await progress_msg.delete()
        await update.message.reply_text(report)
    elif text == "📆 Выбрать дату (товары)":
        now = get_moscow_today()
        keyboard = create_calendar(now.year, now.month, "pdate_")
        await update.message.reply_text("Выберите дату (товары):", reply_markup=keyboard)
        return WAITING_PRODUCT_DATE
    elif text == "📊 Выбрать период (товары)":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗓️ По месяцам", callback_data="pmonth")],
            [InlineKeyboardButton("📅 По кварталам", callback_data="pquarter")],
            [InlineKeyboardButton("📆 По годам", callback_data="pyear")],
            [InlineKeyboardButton("📊 Произвольный период", callback_data="pcustom")],
            [InlineKeyboardButton("🔙 Назад", callback_data="pcancel")]
        ])
        await update.message.reply_text("Выберите тип периода для товаров:", reply_markup=keyboard)
        return WAITING_PRODUCT_PERIOD_TYPE
    elif text == "📈 Динамика по товару":
        progress_msg = await update.message.reply_text("⏳ Загружаю список товаров за последние 30 дней...")
        top_products = await get_top_products_for_select(days=30)
        await progress_msg.delete()
        if not top_products:
            await update.message.reply_text("❌ Нет данных о товарах за последние 30 дней.")
            return ConversationHandler.END
        context.user_data['product_list'] = top_products
        keyboard = []
        for idx, (sku, stats) in enumerate(top_products, 1):
            name = stats['name']
            short_name = name[:12] + "..." if len(name) > 12 else name
            offer_id = stats.get('offer_id', '')
            if offer_id:
                button_text = f"{offer_id} {sku} {short_name}"
            else:
                button_text = f"{sku} {short_name}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"prod_{sku}")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="prod_cancel")])
        await update.message.reply_text(
            "Выберите товар, нажав на соответствующую кнопку:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAITING_PRODUCT_SELECT
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
        await update.message.reply_text(
            f"Главное меню\n\n🤖 Версия бота: {VERSION}",
            reply_markup=main_admin_keyboard()
        )
    else:
        await update.message.reply_text("Неизвестная команда.")

# ---------- ДИАЛОГИ АДМИНИСТРИРОВАНИЯ ----------
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
    if is_admin(chat_id):
        keyboard = main_admin_keyboard()
        text = f"Главное меню\n\n🤖 Версия бота: {VERSION}"
    else:
        keyboard = main_user_keyboard()
        text = f"Главное меню\n\n🤖 Версия бота: {VERSION}"
    await update.message.reply_text(text, reply_markup=keyboard)
    return ConversationHandler.END

# ---------- ОБРАБОТЧИКИ ДИНАМИКИ ПО ТОВАРУ ----------
async def product_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "prod_cancel":
        await query.edit_message_text("Выбор товара отменён.")
        await query.message.reply_text("Выберите действие:", reply_markup=products_reports_keyboard())
        return ConversationHandler.END

    if data.startswith("prod_"):
        sku = data[5:]
        context.user_data['product_sku'] = sku
        product_list = context.user_data.get('product_list', [])
        product_name = "Товар"
        for p_sku, stats in product_list:
            if p_sku == sku:
                product_name = stats['name'][:40]
                break
        context.user_data['product_name'] = product_name

        keyboard = [
            [InlineKeyboardButton("Заказано (₽)", callback_data="metric_ordered_sum")],
            [InlineKeyboardButton("Заказано (шт.)", callback_data="metric_ordered_units")],
            [InlineKeyboardButton("Доставлено (₽)", callback_data="metric_delivered_sum")],
            [InlineKeyboardButton("Доставлено (шт.)", callback_data="metric_delivered_units")],
            [InlineKeyboardButton("Отменено (₽)", callback_data="metric_canceled_sum")],
            [InlineKeyboardButton("Отменено (шт.)", callback_data="metric_canceled_units")],
            [InlineKeyboardButton("Средний чек (₽)", callback_data="metric_avg_check")],
            [InlineKeyboardButton("🔙 Назад", callback_data="metric_cancel")]
        ]
        await query.edit_message_text(
            f"Выбран товар: {product_name} (SKU: {sku})\nТеперь выберите метрику для графика:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAITING_PRODUCT_METRIC

async def product_metric_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "metric_cancel":
        await query.edit_message_text("Выбор метрики отменён.")
        await query.message.reply_text("Выберите действие:", reply_markup=products_reports_keyboard())
        return ConversationHandler.END

    if data.startswith("metric_"):
        metric = data[7:]
        context.user_data['product_metric'] = metric

        current_year = get_moscow_today().year
        keyboard = [
            [InlineKeyboardButton("📅 Текущий год", callback_data="period_current")],
            [InlineKeyboardButton("📆 Выбрать год", callback_data="period_select_year")],
            [InlineKeyboardButton("📊 Диапазон лет", callback_data="period_range")],
            [InlineKeyboardButton("🔙 Назад", callback_data="period_cancel")]
        ]
        await query.edit_message_text(
            "Выберите период для построения графика:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAITING_PRODUCT_PERIOD_CHOICE

async def product_period_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "period_cancel":
        await query.edit_message_text("Построение графика отменено.")
        await query.message.reply_text("Выберите действие:", reply_markup=products_reports_keyboard())
        return ConversationHandler.END

    if data == "period_current":
        current_year = get_moscow_today().year
        sku = context.user_data.get('product_sku')
        metric = context.user_data.get('product_metric')
        if not sku or not metric:
            await query.edit_message_text("❌ Ошибка: потеряны данные. Начните заново.")
            return ConversationHandler.END
        await query.edit_message_text("⏳ Строю график...")
        chart_buf = await generate_product_chart_by_metric(sku, metric, [current_year])
        if chart_buf:
            context.user_data['product_year'] = current_year
            caption = f"Динамика по товару (SKU: {sku}) за {current_year} год"
            keyboard = [
                [InlineKeyboardButton("Другая метрика", callback_data=f"change_metric_{sku}")],
                [InlineKeyboardButton("Другой год", callback_data=f"change_year_{sku}")],
                [InlineKeyboardButton("🔙 Назад", callback_data="product_chart_back")]
            ]
            await query.message.reply_photo(photo=chart_buf, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard))
            await query.delete_message()
        else:
            await query.edit_message_text("❌ Нет данных для построения графика.")
        return ConversationHandler.END

    elif data == "period_select_year":
        current_year = get_moscow_today().year
        years = list(range(current_year - 9, current_year + 1))
        buttons = [[InlineKeyboardButton(str(y), callback_data=f"year_{y}")] for y in years]
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="period_cancel")])
        await query.edit_message_text("Выберите год:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PRODUCT_SINGLE_YEAR

    elif data == "period_range":
        await query.edit_message_text("Введите начальный год (например, 2020):")
        return WAITING_PRODUCT_RANGE_START

async def product_chart_interactive_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "product_chart_back":
        await query.message.delete()
        await query.message.reply_text("Выберите действие:", reply_markup=products_reports_keyboard())
        return ConversationHandler.END

    if data.startswith("change_metric_"):
        sku = data.split("_")[-1]
        await query.message.delete()
        keyboard = [
            [InlineKeyboardButton("Заказано (₽)", callback_data=f"metric_ordered_sum")],
            [InlineKeyboardButton("Заказано (шт.)", callback_data=f"metric_ordered_units")],
            [InlineKeyboardButton("Доставлено (₽)", callback_data=f"metric_delivered_sum")],
            [InlineKeyboardButton("Доставлено (шт.)", callback_data=f"metric_delivered_units")],
            [InlineKeyboardButton("Отменено (₽)", callback_data=f"metric_canceled_sum")],
            [InlineKeyboardButton("Отменено (шт.)", callback_data=f"metric_canceled_units")],
            [InlineKeyboardButton("Средний чек (₽)", callback_data=f"metric_avg_check")],
            [InlineKeyboardButton("🔙 Назад", callback_data="product_chart_back")]
        ]
        await query.message.reply_text(f"Выберите метрику для товара SKU:{sku}:", reply_markup=InlineKeyboardMarkup(keyboard))
        return WAITING_PRODUCT_METRIC

    if data.startswith("change_year_"):
        sku = data.split("_")[-1]
        await query.message.delete()
        current_year = get_moscow_today().year
        years = list(range(current_year - 9, current_year + 1))
        buttons = [[InlineKeyboardButton(str(y), callback_data=f"year_{y}")] for y in years]
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="product_chart_back")])
        await query.message.reply_text(f"Выберите год для товара SKU:{sku}:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PRODUCT_SINGLE_YEAR

    if data.startswith("year_"):
        year = int(data.split("_")[-1])
        sku = context.user_data.get('product_sku')
        metric = context.user_data.get('product_metric')
        if not sku or not metric:
            await query.message.reply_text("❌ Ошибка: потеряны данные.")
            return ConversationHandler.END
        await query.message.delete()
        progress_msg = await query.message.reply_text("⏳ Строю график...")
        chart_buf = await generate_product_chart_by_metric(sku, metric, [year])
        await progress_msg.delete()
        if chart_buf:
            context.user_data['product_year'] = year
            caption = f"Динамика по товару (SKU: {sku}) за {year} год"
            keyboard = [
                [InlineKeyboardButton("Другая метрика", callback_data=f"change_metric_{sku}")],
                [InlineKeyboardButton("Другой год", callback_data=f"change_year_{sku}")],
                [InlineKeyboardButton("🔙 Назад", callback_data="product_chart_back")]
            ]
            await query.message.reply_photo(photo=chart_buf, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.message.reply_text("❌ Нет данных для построения графика.")
        return ConversationHandler.END

async def product_year_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await product_chart_interactive_callback(update, context)

async def product_range_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Пожалуйста, введите число (год).")
        return WAITING_PRODUCT_RANGE_START
    year = int(text)
    if year < 2000 or year > get_moscow_today().year + 1:
        await update.message.reply_text("❌ Некорректный год. Введите год от 2000 до текущего.")
        return WAITING_PRODUCT_RANGE_START
    context.user_data['product_range_start'] = year
    await update.message.reply_text("Введите конечный год (включительно):")
    return WAITING_PRODUCT_RANGE_END

async def product_range_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Пожалуйста, введите число (год).")
        return WAITING_PRODUCT_RANGE_END
    year_end = int(text)
    year_start = context.user_data.get('product_range_start')
    if year_start is None:
        await update.message.reply_text("❌ Ошибка: начальный год не найден. Начните заново.")
        return ConversationHandler.END
    if year_end < year_start:
        await update.message.reply_text("❌ Конечный год должен быть не меньше начального.")
        return WAITING_PRODUCT_RANGE_END
    years = list(range(year_start, year_end + 1))
    if len(years) > 10:
        await update.message.reply_text("⚠️ Слишком много лет (максимум 10). Пожалуйста, выберите меньший диапазон.")
        return ConversationHandler.END

    sku = context.user_data.get('product_sku')
    metric = context.user_data.get('product_metric')
    if not sku or not metric:
        await update.message.reply_text("❌ Ошибка: потеряны данные. Начните заново.")
        return ConversationHandler.END
    progress_msg = await update.message.reply_text("⏳ Строю график...")
    chart_buf = await generate_product_chart_by_metric(sku, metric, years)
    await progress_msg.delete()
    if chart_buf:
        caption = f"Динамика по товару (SKU: {sku}) за {year_start}-{year_end} гг."
        keyboard = [
            [InlineKeyboardButton("Другая метрика", callback_data=f"change_metric_{sku}")],
            [InlineKeyboardButton("Другой год", callback_data=f"change_year_{sku}")],
            [InlineKeyboardButton("🔙 Назад", callback_data="product_chart_back")]
        ]
        await update.message.reply_photo(photo=chart_buf, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text("❌ Нет данных для построения графика.")
    await update.message.reply_text("Выберите действие:", reply_markup=products_reports_keyboard())
    context.user_data.pop('product_sku', None)
    context.user_data.pop('product_metric', None)
    context.user_data.pop('product_name', None)
    context.user_data.pop('product_range_start', None)
    return ConversationHandler.END

# ---------- ДИНАМИКА ПРОДАЖ (диапазон) ----------
async def dynamics_range_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Пожалуйста, введите число (год).")
        return WAITING_DYNAMICS_RANGE_START
    year = int(text)
    if year < 2000 or year > get_moscow_today().year + 1:
        await update.message.reply_text("❌ Некорректный год. Введите год от 2000 до текущего.")
        return WAITING_DYNAMICS_RANGE_START
    context.user_data['dynamics_range_start'] = year
    await update.message.reply_text("Введите конечный год (включительно):")
    return WAITING_DYNAMICS_RANGE_END

async def dynamics_range_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Пожалуйста, введите число (год).")
        return WAITING_DYNAMICS_RANGE_END
    year_end = int(text)
    year_start = context.user_data.get('dynamics_range_start')
    if year_start is None:
        await update.message.reply_text("❌ Ошибка: начальный год не найден. Начните заново.")
        return ConversationHandler.END
    if year_end < year_start:
        await update.message.reply_text("❌ Конечный год должен быть не меньше начального.")
        return WAITING_DYNAMICS_RANGE_END
    years = list(range(year_start, year_end + 1))
    if len(years) > 10:
        await update.message.reply_text("⚠️ Слишком много лет (максимум 10). Пожалуйста, выберите меньший диапазон.")
        return ConversationHandler.END
    progress_msg = await update.message.reply_text("⏳ Загружаю данные...")
    chart_buf = await generate_sales_chart(years)
    await progress_msg.delete()
    if chart_buf:
        caption = f"Динамика доставленных заказов за {year_start}-{year_end} гг."
        await update.message.reply_photo(photo=chart_buf, caption=caption)
    else:
        await update.message.reply_text("❌ Не удалось построить график.")
    await update.message.reply_text("Выберите действие:", reply_markup=sales_reports_keyboard())
    context.user_data.pop('dynamics_range_start', None)
    return ConversationHandler.END

# ==================== РАЗДЕЛ "АВТОМАТИЧЕСКИЕ РАССЫЛКИ" ====================

def auto_reports_keyboard():
    buttons = [
        [KeyboardButton("🕒 Выбор времени рассылок")],
        [KeyboardButton("📅 Добавление отчета за Вчера")],
        [KeyboardButton("🔕 Режим тишины")],
        [KeyboardButton("📤 Отправить отчет за Вчера сейчас")],
        [KeyboardButton("🔙 Назад")]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def build_hours_keyboard(selected_hours: List[int], prefix: str) -> InlineKeyboardMarkup:
    keyboard = []
    for h in range(1, 25):
        hour = h % 24
        label = f"{h:02d}:00"
        checked = "✅" if hour in selected_hours else "⬜"
        callback = f"{prefix}{hour}"
        keyboard.append([InlineKeyboardButton(f"{label} {checked}", callback_data=callback)])
    keyboard.append([
        InlineKeyboardButton("💾 Сохранить", callback_data=f"{prefix}save"),
        InlineKeyboardButton("🔙 Назад", callback_data=f"{prefix}back")
    ])
    return InlineKeyboardMarkup(keyboard)

def build_hours_keyboard_with_filter(selected: List[int], available: List[int], prefix: str) -> InlineKeyboardMarkup:
    keyboard = []
    for h in sorted(available):
        hour = h % 24
        label = f"{h:02d}:00"
        checked = "✅" if h in selected else "⬜"
        callback = f"{prefix}{h}"
        keyboard.append([InlineKeyboardButton(f"{label} {checked}", callback_data=callback)])
    keyboard.append([
        InlineKeyboardButton("💾 Сохранить", callback_data=f"{prefix}save"),
        InlineKeyboardButton("🔙 Назад", callback_data=f"{prefix}back")
    ])
    return InlineKeyboardMarkup(keyboard)

def build_hours_keyboard_for_silence(prefix: str) -> InlineKeyboardMarkup:
    keyboard = []
    for h in range(1, 25):
        hour = h % 24
        label = f"{h:02d}:00"
        callback = f"{prefix}{hour}"
        keyboard.append([InlineKeyboardButton(label, callback_data=callback)])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f"{prefix}back")])
    return InlineKeyboardMarkup(keyboard)

async def auto_reports_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not has_access(chat_id):
        await update.message.reply_text("❌ Нет доступа.")
        return
    write_log(f"Пользователь {chat_id} открыл меню 'Автоматические рассылки'")
    await update.message.reply_text("🔔 *Автоматические рассылки*\n\nВыберите действие:", reply_markup=auto_reports_keyboard(), parse_mode="Markdown")

# ---------- 1. ВЫБОР ВРЕМЕНИ РАССЫЛОК ----------
async def auto_schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not has_access(chat_id):
        await update.message.reply_text("❌ Нет доступа.")
        return
    write_log(f"Пользователь {chat_id} открыл настройку времени рассылок")
    settings = await load_settings()
    current_hours = settings.get("schedule_hours", [])
    if current_hours:
        text = f"Текущие часы рассылок: {', '.join(f'{h:02d}:00' for h in sorted(current_hours))}\n\nНажмите «Изменить» для редактирования."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить", callback_data="sch_edit")],
            [InlineKeyboardButton("🔙 Назад", callback_data="sch_back")]
        ])
        await update.message.reply_text(text, reply_markup=keyboard)
    else:
        text = "Часы рассылок не настроены.\nНажмите «Выбрать время» для настройки."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🕒 Выбрать время", callback_data="sch_edit")],
            [InlineKeyboardButton("🔙 Назад", callback_data="sch_back")]
        ])
        await update.message.reply_text(text, reply_markup=keyboard)

async def schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    write_log(f"Callback schedule: {data} от {chat_id}")

    if data == "sch_back":
        await query.edit_message_text("🔔 *Автоматические рассылки*\n\nВыберите действие:", reply_markup=auto_reports_keyboard(), parse_mode="Markdown")
        return ConversationHandler.END

    if data == "sch_edit":
        settings = await load_settings()
        current = settings.get("schedule_hours", [])
        context.user_data['temp_schedule_hours'] = current.copy()
        keyboard = build_hours_keyboard(current, "sch_")
        await query.edit_message_text("Выберите часы для рассылок (нажмите на час, чтобы включить/выключить):", reply_markup=keyboard)
        return WAITING_AUTO_SCHEDULE

    if data.startswith("sch_") and data[4:].isdigit():
        hour = int(data[4:])
        temp = context.user_data.get('temp_schedule_hours', [])
        if hour in temp:
            temp.remove(hour)
            write_log(f"Час {hour}:00 удалён из временного списка")
        else:
            temp.append(hour)
            write_log(f"Час {hour}:00 добавлен во временный список")
        context.user_data['temp_schedule_hours'] = temp
        keyboard = build_hours_keyboard(temp, "sch_")
        await query.edit_message_reply_markup(reply_markup=keyboard)
        return WAITING_AUTO_SCHEDULE

    if data == "sch_save":
        temp = context.user_data.get('temp_schedule_hours', [])
        settings = await load_settings()
        settings["schedule_hours"] = temp
        if "yesterday_report_hours" in settings:
            settings["yesterday_report_hours"] = [h for h in settings["yesterday_report_hours"] if h in temp]
        await save_settings(settings)
        write_log(f"Сохранены часы рассылок: {temp}")
        await query.edit_message_text(f"✅ Настройки сохранены. Часы рассылок: {', '.join(f'{h:02d}:00' for h in sorted(temp)) if temp else 'не выбраны'}", reply_markup=None)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔕 Настроить режим тишины", callback_data="sch_go_silence")],
            [InlineKeyboardButton("🔙 В меню", callback_data="sch_go_back")]
        ])
        await query.message.reply_text("Хотите настроить режим тишины?", reply_markup=keyboard)
        return ConversationHandler.END

    if data == "sch_go_silence":
        await query.edit_message_text("Переход к настройке режима тишины...")
        return await auto_silence_start(update, context)

    if data == "sch_go_back":
        await query.edit_message_text("🔔 *Автоматические рассылки*\n\nВыберите действие:", reply_markup=auto_reports_keyboard(), parse_mode="Markdown")
        return ConversationHandler.END

# ---------- 2. ДОБАВЛЕНИЕ ОТЧЕТА ЗА ВЧЕРА ----------
async def auto_yesterday_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not has_access(chat_id):
        await update.message.reply_text("❌ Нет доступа.")
        return
    write_log(f"Пользователь {chat_id} открыл настройку отчёта за вчера")
    settings = await load_settings()
    schedule = settings.get("schedule_hours", [])
    if not schedule:
        await update.message.reply_text("❌ Сначала необходимо настроить «Выбрать время для рассылок».")
        return
    yesterday_hours = settings.get("yesterday_report_hours", [])
    context.user_data['temp_yesterday_hours'] = yesterday_hours.copy()
    keyboard = build_hours_keyboard_with_filter(yesterday_hours, schedule, "yest_")
    await update.message.reply_text("Выберите часы, в которые будет отправляться отчёт за Вчера (из уже выбранных часов рассылок):", reply_markup=keyboard)

async def yesterday_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    write_log(f"Callback yesterday: {data} от {chat_id}")

    if data == "yest_back":
        await query.edit_message_text("🔔 *Автоматические рассылки*\n\nВыберите действие:", reply_markup=auto_reports_keyboard(), parse_mode="Markdown")
        return ConversationHandler.END

    if data.startswith("yest_") and data[5:].isdigit():
        hour = int(data[5:])
        temp = context.user_data.get('temp_yesterday_hours', [])
        if hour in temp:
            temp.remove(hour)
            write_log(f"Час {hour}:00 удалён из списка отчёта за вчера")
        else:
            temp.append(hour)
            write_log(f"Час {hour}:00 добавлен в список отчёта за вчера")
        context.user_data['temp_yesterday_hours'] = temp
        settings = await load_settings()
        schedule = settings.get("schedule_hours", [])
        keyboard = build_hours_keyboard_with_filter(temp, schedule, "yest_")
        await query.edit_message_reply_markup(reply_markup=keyboard)
        return WAITING_AUTO_YESTERDAY

    if data == "yest_save":
        temp = context.user_data.get('temp_yesterday_hours', [])
        settings = await load_settings()
        settings["yesterday_report_hours"] = temp
        await save_settings(settings)
        write_log(f"Сохранены часы для отчёта за вчера: {temp}")
        await query.edit_message_text(f"✅ Настройки сохранены. Отчёт за Вчера будет отправляться в часы: {', '.join(f'{h:02d}:00' for h in sorted(temp)) if temp else 'не выбраны'}", reply_markup=None)
        await query.message.reply_text("🔔 *Автоматические рассылки*\n\nВыберите действие:", reply_markup=auto_reports_keyboard(), parse_mode="Markdown")
        return ConversationHandler.END

# ---------- 3. РЕЖИМ ТИШИНЫ ----------
async def auto_silence_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not has_access(chat_id):
        await update.message.reply_text("❌ Нет доступа.")
        return
    write_log(f"Пользователь {chat_id} открыл настройку режима тишины")
    settings = await load_settings()
    start = settings.get("silence_start")
    end = settings.get("silence_end")
    if start is not None and end is not None:
        text = f"Текущий режим тишины: с {start:02d}:00 до {end:02d}:00"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить", callback_data="sil_edit")],
            [InlineKeyboardButton("❌ Отключить", callback_data="sil_disable")],
            [InlineKeyboardButton("🔙 Назад", callback_data="sil_back")]
        ])
        await update.message.reply_text(text, reply_markup=keyboard)
    else:
        text = "Режим тишины не настроен."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔕 Установить", callback_data="sil_edit")],
            [InlineKeyboardButton("🔙 Назад", callback_data="sil_back")]
        ])
        await update.message.reply_text(text, reply_markup=keyboard)

async def silence_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    write_log(f"Callback silence: {data} от {chat_id}")

    if data == "sil_back":
        await query.edit_message_text("🔔 *Автоматические рассылки*\n\nВыберите действие:", reply_markup=auto_reports_keyboard(), parse_mode="Markdown")
        return ConversationHandler.END

    if data == "sil_disable":
        settings = await load_settings()
        settings["silence_start"] = None
        settings["silence_end"] = None
        await save_settings(settings)
        write_log("Режим тишины отключён")
        await query.edit_message_text("✅ Режим тишины отключён.")
        await query.message.reply_text("🔔 *Автоматические рассылки*\n\nВыберите действие:", reply_markup=auto_reports_keyboard(), parse_mode="Markdown")
        return ConversationHandler.END

    if data == "sil_edit":
        context.user_data['silence_step'] = 'start'
        keyboard = build_hours_keyboard_for_silence("sil_start_")
        await query.edit_message_text("Выберите час начала режима тишины:", reply_markup=keyboard)
        return WAITING_AUTO_SILENCE_START

async def silence_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    write_log(f"Callback silence_start: {data} от {chat_id}")

    if data == "sil_start_back":
        await query.edit_message_text("🔔 *Автоматические рассылки*\n\nВыберите действие:", reply_markup=auto_reports_keyboard(), parse_mode="Markdown")
        return ConversationHandler.END

    if data.startswith("sil_start_") and data[10:].isdigit():
        hour = int(data[10:])
        context.user_data['temp_silence_start'] = hour
        write_log(f"Выбран час начала тишины: {hour}:00")
        keyboard = build_hours_keyboard_for_silence("sil_end_")
        await query.edit_message_text(f"Начало тишины: {hour:02d}:00\nТеперь выберите час окончания:", reply_markup=keyboard)
        return WAITING_AUTO_SILENCE_END

async def silence_end_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    write_log(f"Callback silence_end: {data} от {chat_id}")

    if data == "sil_end_back":
        keyboard = build_hours_keyboard_for_silence("sil_start_")
        await query.edit_message_text("Выберите час начала режима тишины:", reply_markup=keyboard)
        return WAITING_AUTO_SILENCE_START

    if data.startswith("sil_end_") and data[8:].isdigit():
        hour = int(data[8:])
        start = context.user_data.get('temp_silence_start')
        if start is None:
            await query.edit_message_text("Ошибка: не выбран начальный час. Попробуйте снова.")
            return ConversationHandler.END
        settings = await load_settings()
        settings["silence_start"] = start
        settings["silence_end"] = hour
        await save_settings(settings)
        write_log(f"Режим тишины установлен: с {start}:00 до {hour}:00")
        await query.edit_message_text(f"✅ Режим тишины установлен: с {start:02d}:00 до {hour:02d}:00")
        await query.message.reply_text("🔔 *Автоматические рассылки*\n\nВыберите действие:", reply_markup=auto_reports_keyboard(), parse_mode="Markdown")
        context.user_data.pop('temp_silence_start', None)
        return ConversationHandler.END

# ---------- 4. ОТПРАВИТЬ ОТЧЕТ ЗА ВЧЕРА СЕЙЧАС ----------
async def auto_send_yesterday_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not has_access(chat_id):
        await update.message.reply_text("❌ Нет доступа.")
        return
    write_log(f"Пользователь {chat_id} инициировал принудительную отправку отчёта за вчера")
    await update.message.reply_text("⏳ Формирую отчёт за вчера...")
    try:
        report = await format_combined_metrics_with_deltas(include_yesterday=True, progress_callback=None)
        await send_report_to_all_users(context.bot, report)
        write_log("Принудительный отчёт за вчера успешно отправлен всем пользователям")
        await update.message.reply_text("✅ Отчёт отправлен всем пользователям.")
    except Exception as e:
        error_msg = f"Ошибка при отправке отчёта за вчера сейчас: {e}"
        write_log(f"❌ {error_msg}")
        await update.message.reply_text(f"❌ Произошла ошибка: {e}")

async def send_report_to_all_users(bot, report: str):
    recipients = []
    if ADMIN_CHAT_ID:
        recipients.append(ADMIN_CHAT_ID)
    managers = load_managers()
    for m in managers:
        recipients.append(m['id'])
    recipients = list(set(recipients))
    write_log(f"Отправка отчёта получателям: {recipients}")
    for chat_id in recipients:
        try:
            await bot.send_message(chat_id=chat_id, text=report, parse_mode="Markdown")
            write_log(f"Отчёт отправлен пользователю {chat_id}")
        except Exception as e:
            write_log(f"Не удалось отправить сообщение {chat_id}: {e}")

# ---------- ПЛАНИРОВЩИК ----------
async def check_auto_reports(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.datetime.now(MOSCOW_TZ)
    current_hour = now.hour
    current_date_str = now.strftime("%Y-%m-%d")
    write_log(f"⏰ Проверка автоматических рассылок в {current_hour:02d}:00")
    settings = await load_settings()
    yesterday_hours = settings.get("yesterday_report_hours", [])
    if not yesterday_hours:
        write_log("ℹ️ Нет настроенных часов для отчёта за вчера")
        return
    if current_hour not in yesterday_hours:
        write_log(f"ℹ️ Час {current_hour}:00 не входит в расписание")
        return

    silence_start = settings.get("silence_start")
    silence_end = settings.get("silence_end")
    if silence_start is not None and silence_end is not None:
        if silence_start < silence_end:
            if silence_start <= current_hour < silence_end:
                write_log(f"🔕 Режим тишины: {current_hour}:00 в интервале {silence_start}:00-{silence_end}:00, пропускаем")
                return
        else:
            if current_hour >= silence_start or current_hour < silence_end:
                write_log(f"🔕 Режим тишины: {current_hour}:00 в интервале {silence_start}:00-{silence_end}:00 (через полночь), пропускаем")
                return

    last_sent = settings.get("last_yesterday_report_sent")
    if last_sent:
        try:
            last_date_str, last_hour_str = last_sent.split()
            if last_date_str == current_date_str and int(last_hour_str) == current_hour:
                write_log(f"ℹ️ Отчёт за вчера уже отправлен в {current_hour}:00 сегодня")
                return
        except Exception as e:
            write_log(f"⚠️ Ошибка разбора last_sent: {e}")

    write_log(f"📤 Отправка автоматического отчёта за вчера в {current_hour:02d}:00")
    try:
        report = await format_combined_metrics_with_deltas(include_yesterday=True, progress_callback=None)
        await send_report_to_all_users(context.bot, report)
        settings["last_yesterday_report_sent"] = f"{current_date_str} {current_hour}"
        await save_settings(settings)
        write_log("✅ Автоматический отчёт успешно отправлен")
    except Exception as e:
        write_log(f"❌ Ошибка при автоматической отправке отчёта: {e}")

# ==================== ПОЛНЫЙ ОБРАБОТЧИК CALLBACK ====================
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id

    if not has_access(chat_id):
        await query.edit_message_text("❌ Нет доступа! Обратитесь к администратору.")
        return ConversationHandler.END

    # ---------- Автоматические рассылки ----------
    if data.startswith("sch_") or data.startswith("yest_") or data.startswith("sil_") or data.startswith("sil_start_") or data.startswith("sil_end_"):
        if data.startswith("sch_"):
            return await schedule_callback(update, context)
        elif data.startswith("yest_"):
            return await yesterday_callback(update, context)
        elif data.startswith("sil_") and not data.startswith("sil_start_") and not data.startswith("sil_end_"):
            return await silence_callback(update, context)
        elif data.startswith("sil_start_"):
            return await silence_start_callback(update, context)
        elif data.startswith("sil_end_"):
            return await silence_end_callback(update, context)

    # ---------- Интерактивные графики товаров ----------
    if data.startswith("change_metric_") or data.startswith("change_year_") or data.startswith("year_") or data == "product_chart_back":
        return await product_chart_interactive_callback(update, context)

    # ---------- Выбор даты (продажи) ----------
    if data.startswith("date_"):
        if data == "date_cancel":
            await query.edit_message_text("Выбор даты отменён.")
            await query.message.reply_text("Выберите действие:", reply_markup=sales_reports_keyboard())
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
            valid, result = validate_date(date_str)
            if not valid:
                await query.edit_message_text(result)
                return WAITING_DATE_SINGLE
            progress_msg = await query.message.reply_text("⏳ Загружаю данные...")
            metrics = await get_metrics_for_date(date_str, progress_callback=None)
            await progress_msg.delete()
            msg = format_single_metrics(metrics, f"Продажи за {date_str}")
            await query.edit_message_text(msg, parse_mode="Markdown")
            await query.message.reply_text("Выберите действие:", reply_markup=sales_reports_keyboard())
            return ConversationHandler.END
        else:
            await query.edit_message_text("❌ Ошибка формата даты.")
            return WAITING_DATE_SINGLE

    # ---------- Выбор периода (продажи) ----------
    if data == "period_month":
        current_year = get_moscow_today().year
        years = list(range(current_year - 9, current_year + 1))
        buttons = [[InlineKeyboardButton(str(y), callback_data=f"period_year_month_{y}")] for y in years]
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="period_cancel")])
        await query.edit_message_text("Выберите год:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PERIOD_YEAR
    if data == "period_quarter":
        current_year = get_moscow_today().year
        years = list(range(current_year - 9, current_year + 1))
        buttons = [[InlineKeyboardButton(str(y), callback_data=f"period_year_quarter_{y}")] for y in years]
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="period_cancel")])
        await query.edit_message_text("Выберите год:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PERIOD_YEAR
    if data == "period_year":
        current_year = get_moscow_today().year
        years = list(range(current_year - 9, current_year + 1))
        buttons = [[InlineKeyboardButton(str(y), callback_data=f"period_year_only_{y}")] for y in years]
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="period_cancel")])
        await query.edit_message_text("Выберите год:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_YEAR_SELECT
    if data == "period_custom":
        now = get_moscow_today()
        keyboard = create_calendar(now.year, now.month, "start_")
        await query.edit_message_text("Выберите начальную дату:", reply_markup=keyboard)
        return WAITING_PERIOD_START
    if data == "period_cancel":
        await query.edit_message_text("Выбор периода отменён.")
        await query.message.reply_text("Выберите действие:", reply_markup=sales_reports_keyboard())
        return ConversationHandler.END

    if data.startswith("period_year_month_"):
        year = int(data.split("_")[-1])
        context.user_data['period_year'] = year
        months = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
        buttons = [[InlineKeyboardButton(name, callback_data=f"period_month_{i}_{year}")] for i, name in enumerate(months, 1)]
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="period_cancel")])
        await query.edit_message_text(f"Выберите месяц {year}:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PERIOD_MONTH

    if data.startswith("period_year_quarter_"):
        year = int(data.split("_")[-1])
        context.user_data['period_year'] = year
        quarters = ["1 квартал (янв-мар)", "2 квартал (апр-июн)", "3 квартал (июл-сен)", "4 квартал (окт-дек)"]
        buttons = [[InlineKeyboardButton(name, callback_data=f"period_quarter_{i}_{year}")] for i, name in enumerate(quarters, 1)]
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="period_cancel")])
        await query.edit_message_text(f"Выберите квартал {year}:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PERIOD_QUARTER

    if data.startswith("period_year_only_"):
        year = int(data.split("_")[-1])
        first_day = datetime.date(year, 1, 1)
        last_day = datetime.date(year, 12, 31)
        progress_msg = await query.message.reply_text("⏳ Загружаю данные...")
        metrics_current = await get_metrics_for_period(first_day.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d"), progress_callback=None)
        prev_year = year - 1
        prev_first_day = datetime.date(prev_year, 1, 1)
        prev_last_day = datetime.date(prev_year, 12, 31)
        metrics_prev = await get_metrics_for_period(prev_first_day.strftime("%Y-%m-%d"), prev_last_day.strftime("%Y-%m-%d"), progress_callback=None)
        await progress_msg.delete()
        period_name = str(year)
        report = format_period_comparison_metrics(metrics_current, metrics_prev, period_name)
        await query.edit_message_text(report, parse_mode="Markdown")
        await query.message.reply_text("Выберите действие:", reply_markup=sales_reports_keyboard())
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
        progress_msg = await query.message.reply_text("⏳ Загружаю данные...")
        metrics_current = await get_metrics_for_period(date_from, date_to, progress_callback=None)
        if month_num == 1:
            prev_month_num = 12
            prev_year = year - 1
        else:
            prev_month_num = month_num - 1
            prev_year = year
        prev_first_day = datetime.date(prev_year, prev_month_num, 1)
        if prev_month_num == 12:
            prev_last_day = datetime.date(prev_year, 12, 31)
        else:
            prev_last_day = datetime.date(prev_year, prev_month_num+1, 1) - datetime.timedelta(days=1)
        metrics_prev = await get_metrics_for_period(prev_first_day.strftime("%Y-%m-%d"), prev_last_day.strftime("%Y-%m-%d"), progress_callback=None)
        await progress_msg.delete()
        month_names = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                       "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
        period_name = f"{month_names[month_num-1]} {year}"
        report = format_period_comparison_metrics(metrics_current, metrics_prev, period_name)
        await query.edit_message_text(report, parse_mode="Markdown")
        await query.message.reply_text("Выберите действие:", reply_markup=sales_reports_keyboard())
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
        progress_msg = await query.message.reply_text("⏳ Загружаю данные...")
        metrics_current = await get_metrics_for_period(date_from, date_to, progress_callback=None)
        if q == 1:
            prev_q = 4
            prev_year = year - 1
            prev_start_month = (prev_q-1)*3 + 1
            prev_end_month = prev_q*3
        else:
            prev_q = q - 1
            prev_year = year
            prev_start_month = (prev_q-1)*3 + 1
            prev_end_month = prev_q*3
        prev_first_day = datetime.date(prev_year, prev_start_month, 1)
        if prev_end_month == 12:
            prev_last_day = datetime.date(prev_year, 12, 31)
        else:
            prev_last_day = datetime.date(prev_year, prev_end_month+1, 1) - datetime.timedelta(days=1)
        metrics_prev = await get_metrics_for_period(prev_first_day.strftime("%Y-%m-%d"), prev_last_day.strftime("%Y-%m-%d"), progress_callback=None)
        await progress_msg.delete()
        period_name = f"{q} квартал {year}"
        report = format_period_comparison_metrics(metrics_current, metrics_prev, period_name)
        await query.edit_message_text(report, parse_mode="Markdown")
        await query.message.reply_text("Выберите действие:", reply_markup=sales_reports_keyboard())
        return ConversationHandler.END

    if data.startswith("start_"):
        if data == "start_cancel":
            await query.edit_message_text("Выбор периода отменён.")
            await query.message.reply_text("Выберите действие:", reply_markup=sales_reports_keyboard())
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
            valid, result = validate_date(date_str)
            if not valid:
                await query.edit_message_text(result)
                return WAITING_PERIOD_START
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
            await query.message.reply_text("Выберите действие:", reply_markup=sales_reports_keyboard())
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
            valid, result = validate_date(end_date_str)
            if not valid:
                await query.edit_message_text(result)
                return WAITING_PERIOD_END
            start_date = context.user_data.get('period_start_date')
            if not start_date:
                await query.edit_message_text("❌ Ошибка: начальная дата не найдена. Попробуйте снова.")
                return ConversationHandler.END
            valid_period, msg = validate_period(start_date, end_date_str)
            if not valid_period:
                await query.edit_message_text(msg)
                now = get_moscow_today()
                keyboard = create_calendar(now.year, now.month, "start_")
                await query.message.reply_text("Выберите начальную дату заново:", reply_markup=keyboard)
                return WAITING_PERIOD_START
            progress_msg = await query.message.reply_text("⏳ Загружаю данные...")
            metrics = await get_metrics_for_period(start_date, end_date_str, progress_callback=None)
            await progress_msg.delete()
            msg = format_single_metrics(metrics, f"Продажи за период {start_date} – {end_date_str}")
            await query.edit_message_text(msg, parse_mode="Markdown")
            await query.message.reply_text("Выберите действие:", reply_markup=sales_reports_keyboard())
            context.user_data.pop('period_start_date', None)
            return ConversationHandler.END
        else:
            await query.edit_message_text("❌ Ошибка формата даты.")
            return WAITING_PERIOD_END

    # ---------- Динамика продаж ----------
    if data == "dynamics_current":
        await query.edit_message_text("⏳ Загружаю данные...")
        current_year = get_moscow_today().year
        chart_buf = await generate_sales_chart([current_year])
        if chart_buf:
            await query.message.reply_photo(photo=chart_buf, caption=f"Динамика доставленных заказов за {current_year} год")
        else:
            await query.message.reply_text("❌ Не удалось построить график.")
        await query.edit_message_text("Выберите действие:", reply_markup=sales_reports_keyboard())
        return ConversationHandler.END

    if data == "dynamics_select":
        current_year = get_moscow_today().year
        years = list(range(current_year - 9, current_year + 1))
        buttons = [[InlineKeyboardButton(str(y), callback_data=f"dynamics_year_{y}")] for y in years]
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="dynamics_cancel")])
        await query.edit_message_text("Выберите год для отображения графика:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_DYNAMICS_SELECT

    if data == "dynamics_range":
        await query.edit_message_text("Введите начальный год (например, 2020):")
        return WAITING_DYNAMICS_RANGE_START

    if data == "dynamics_cancel":
        await query.edit_message_text("Построение графика отменено.")
        await query.message.reply_text("Выберите действие:", reply_markup=sales_reports_keyboard())
        return ConversationHandler.END

    if data.startswith("dynamics_year_"):
        year = int(data.split("_")[-1])
        await query.edit_message_text("⏳ Загружаю данные...")
        chart_buf = await generate_sales_chart([year])
        if chart_buf:
            await query.message.reply_photo(photo=chart_buf, caption=f"Динамика доставленных заказов за {year} год")
        else:
            await query.message.reply_text("❌ Не удалось построить график.")
        await query.edit_message_text("Выберите действие:", reply_markup=sales_reports_keyboard())
        return ConversationHandler.END

    # ---------- Товарные отчёты (даты, периоды) ----------
    if data.startswith("pdate_"):
        if data == "pdate_cancel":
            await query.edit_message_text("Выбор даты отменён.")
            await query.message.reply_text("Выберите действие:", reply_markup=products_reports_keyboard())
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
                return WAITING_PRODUCT_DATE
            if action == "prev_month":
                month -= 1
                if month == 0: month = 12; year -= 1
            else:
                month += 1
                if month == 13: month = 1; year += 1
            keyboard = create_calendar(year, month, "pdate_")
            await query.edit_message_reply_markup(reply_markup=keyboard)
            return WAITING_PRODUCT_DATE
        date_str = data[6:]
        if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            valid, result = validate_date(date_str)
            if not valid:
                await query.edit_message_text(result)
                return WAITING_PRODUCT_DATE
            progress_msg = await query.message.reply_text("⏳ Загружаю данные...")
            products = await get_product_data_for_date(date_str)
            await progress_msg.delete()
            msg = format_top_products(products, f"Товары за {date_str}", limit=20)
            summary = format_products_summary(products)
            await query.edit_message_text(msg + "\n\n" + summary)
            await query.message.reply_text("Выберите действие:", reply_markup=products_reports_keyboard())
            return ConversationHandler.END
        else:
            await query.edit_message_text("❌ Ошибка формата даты.")
            return WAITING_PRODUCT_DATE

    if data == "pmonth":
        current_year = get_moscow_today().year
        years = list(range(current_year - 9, current_year + 1))
        buttons = [[InlineKeyboardButton(str(y), callback_data=f"pyear_month_{y}")] for y in years]
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="pcancel")])
        await query.edit_message_text("Выберите год:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PRODUCT_YEAR
    if data == "pquarter":
        current_year = get_moscow_today().year
        years = list(range(current_year - 9, current_year + 1))
        buttons = [[InlineKeyboardButton(str(y), callback_data=f"pyear_quarter_{y}")] for y in years]
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="pcancel")])
        await query.edit_message_text("Выберите год:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PRODUCT_YEAR
    if data == "pyear":
        current_year = get_moscow_today().year
        years = list(range(current_year - 9, current_year + 1))
        buttons = [[InlineKeyboardButton(str(y), callback_data=f"pyear_only_{y}")] for y in years]
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="pcancel")])
        await query.edit_message_text("Выберите год:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PRODUCT_YEAR_SELECT
    if data == "pcustom":
        now = get_moscow_today()
        keyboard = create_calendar(now.year, now.month, "pstart_")
        await query.edit_message_text("Выберите начальную дату:", reply_markup=keyboard)
        return WAITING_PRODUCT_PERIOD_START
    if data == "pcancel":
        await query.edit_message_text("Выбор периода отменён.")
        await query.message.reply_text("Выберите действие:", reply_markup=products_reports_keyboard())
        return ConversationHandler.END

    if data.startswith("pyear_month_"):
        year = int(data.split("_")[-1])
        context.user_data['p_year'] = year
        months = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
        buttons = [[InlineKeyboardButton(name, callback_data=f"pmonth_{i}_{year}")] for i, name in enumerate(months, 1)]
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="pcancel")])
        await query.edit_message_text(f"Выберите месяц {year}:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PRODUCT_MONTH
    if data.startswith("pyear_quarter_"):
        year = int(data.split("_")[-1])
        context.user_data['p_year'] = year
        quarters = ["1 квартал (янв-мар)", "2 квартал (апр-июн)", "3 квартал (июл-сен)", "4 квартал (окт-дек)"]
        buttons = [[InlineKeyboardButton(name, callback_data=f"pquarter_{i}_{year}")] for i, name in enumerate(quarters, 1)]
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="pcancel")])
        await query.edit_message_text(f"Выберите квартал {year}:", reply_markup=InlineKeyboardMarkup(buttons))
        return WAITING_PRODUCT_QUARTER

    if data.startswith("pyear_only_"):
        year = int(data.split("_")[-1])
        first_day = datetime.date(year, 1, 1)
        last_day = datetime.date(year, 12, 31)
        progress_msg = await query.message.reply_text("⏳ Загружаю данные...")
        products = await get_product_data_for_period(first_day.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d"))
        await progress_msg.delete()
        msg = format_top_products(products, f"Товары за {year} год", limit=20)
        summary = format_products_summary(products)
        await query.edit_message_text(msg + "\n\n" + summary)
        await query.message.reply_text("Выберите действие:", reply_markup=products_reports_keyboard())
        return ConversationHandler.END

    if data.startswith("pmonth_"):
        parts = data.split("_")
        month_num, year = int(parts[1]), int(parts[2])
        first_day = datetime.date(year, month_num, 1)
        if month_num == 12:
            last_day = datetime.date(year, 12, 31)
        else:
            last_day = datetime.date(year, month_num+1, 1) - datetime.timedelta(days=1)
        date_from, date_to = first_day.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d")
        progress_msg = await query.message.reply_text("⏳ Загружаю данные...")
        products = await get_product_data_for_period(date_from, date_to)
        await progress_msg.delete()
        msg = format_top_products(products, f"Товары за {first_day.strftime('%B %Y')}", limit=20)
        summary = format_products_summary(products)
        await query.edit_message_text(msg + "\n\n" + summary)
        await query.message.reply_text("Выберите действие:", reply_markup=products_reports_keyboard())
        return ConversationHandler.END

    if data.startswith("pquarter_"):
        parts = data.split("_")
        q, year = int(parts[1]), int(parts[2])
        start_month = (q-1)*3 + 1
        end_month = q*3
        first_day = datetime.date(year, start_month, 1)
        if end_month == 12:
            last_day = datetime.date(year, 12, 31)
        else:
            last_day = datetime.date(year, end_month+1, 1) - datetime.timedelta(days=1)
        date_from, date_to = first_day.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d")
        progress_msg = await query.message.reply_text("⏳ Загружаю данные...")
        products = await get_product_data_for_period(date_from, date_to)
        await progress_msg.delete()
        msg = format_top_products(products, f"Товары за {q} квартал {year}", limit=20)
        summary = format_products_summary(products)
        await query.edit_message_text(msg + "\n\n" + summary)
        await query.message.reply_text("Выберите действие:", reply_markup=products_reports_keyboard())
        return ConversationHandler.END

    if data.startswith("pstart_"):
        if data == "pstart_cancel":
            await query.edit_message_text("Выбор периода отменён.")
            await query.message.reply_text("Выберите действие:", reply_markup=products_reports_keyboard())
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
                return WAITING_PRODUCT_PERIOD_START
            if action == "prev_month":
                month -= 1
                if month == 0: month = 12; year -= 1
            else:
                month += 1
                if month == 13: month = 1; year += 1
            keyboard = create_calendar(year, month, "pstart_")
            await query.edit_message_reply_markup(reply_markup=keyboard)
            return WAITING_PRODUCT_PERIOD_START
        date_str = data[7:]
        if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            valid, result = validate_date(date_str)
            if not valid:
                await query.edit_message_text(result)
                return WAITING_PRODUCT_PERIOD_START
            context.user_data['p_start_date'] = date_str
            now = get_moscow_today()
            keyboard = create_calendar(now.year, now.month, "pend_")
            await query.edit_message_text(f"Начало: {date_str}\nТеперь выберите конечную дату:", reply_markup=keyboard)
            return WAITING_PRODUCT_PERIOD_END
        else:
            await query.edit_message_text("❌ Ошибка формата даты.")
            return WAITING_PRODUCT_PERIOD_START

    if data.startswith("pend_"):
        if data == "pend_cancel":
            await query.edit_message_text("Выбор периода отменён.")
            await query.message.reply_text("Выберите действие:", reply_markup=products_reports_keyboard())
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
                return WAITING_PRODUCT_PERIOD_END
            if action == "prev_month":
                month -= 1
                if month == 0: month = 12; year -= 1
            else:
                month += 1
                if month == 13: month = 1; year += 1
            keyboard = create_calendar(year, month, "pend_")
            await query.edit_message_reply_markup(reply_markup=keyboard)
            return WAITING_PRODUCT_PERIOD_END
        end_date_str = data[5:]
        if re.match(r"\d{4}-\d{2}-\d{2}", end_date_str):
            valid, result = validate_date(end_date_str)
            if not valid:
                await query.edit_message_text(result)
                return WAITING_PRODUCT_PERIOD_END
            start_date = context.user_data.get('p_start_date')
            if not start_date:
                await query.edit_message_text("❌ Ошибка: начальная дата не найдена. Попробуйте снова.")
                return ConversationHandler.END
            valid_period, msg = validate_period(start_date, end_date_str)
            if not valid_period:
                await query.edit_message_text(msg)
                now = get_moscow_today()
                keyboard = create_calendar(now.year, now.month, "pstart_")
                await query.message.reply_text("Выберите начальную дату заново:", reply_markup=keyboard)
                return WAITING_PRODUCT_PERIOD_START
            progress_msg = await query.message.reply_text("⏳ Загружаю данные...")
            products = await get_product_data_for_period(start_date, end_date_str)
            await progress_msg.delete()
            msg = format_top_products(products, f"Товары за период {start_date} – {end_date_str}", limit=20)
            summary = format_products_summary(products)
            await query.edit_message_text(msg + "\n\n" + summary)
            await query.message.reply_text("Выберите действие:", reply_markup=products_reports_keyboard())
            context.user_data.pop('p_start_date', None)
            return ConversationHandler.END
        else:
            await query.edit_message_text("❌ Ошибка формата даты.")
            return WAITING_PRODUCT_PERIOD_END

    # ---------- Динамика по товару ----------
    if data.startswith("prod_"):
        return await product_select_callback(update, context)
    if data.startswith("metric_"):
        return await product_metric_callback(update, context)
    if data.startswith("period_") or data.startswith("year_") or data == "period_cancel" or data == "period_current" or data == "period_select_year" or data == "period_range":
        return await product_period_callback(update, context)

    # ---------- Если ничего не подошло ----------
    await query.edit_message_text("❌ Неизвестная команда.")
    return ConversationHandler.END

# ==================== ЗАПУСК ====================
def main():
    if not validate_env_vars():
        sys.exit(1)
    write_log(f"🚀 Бот запускается (версия {VERSION})")
    write_log(f"✅ OZON_CLIENT_ID: {mask_secret(OZON_CLIENT_ID)}")
    write_log(f"✅ OZON_API_KEY: {mask_secret(OZON_API_KEY)}")
    write_log(f"✅ TELEGRAM_BOT_TOKEN: {mask_secret(TELEGRAM_BOT_TOKEN)}")
    write_log(f"✅ ADMIN_CHAT_ID: {ADMIN_CHAT_ID}")
    if OZON_COMPANY_ID:
        write_log(f"ℹ️ OZON_COMPANY_ID: {mask_secret(OZON_COMPANY_ID)} (будет передан в финансовый API)")
    else:
        write_log("ℹ️ OZON_COMPANY_ID не задан (необязательный параметр)")
    if not OZON_PERFORMANCE_CLIENT_ID or not OZON_PERFORMANCE_CLIENT_SECRET:
        write_log("⚠️ ВНИМАНИЕ: OZON_PERFORMANCE_CLIENT_ID или CLIENT_SECRET не заданы. Рекламные расходы не будут отображаться.")
    else:
        write_log(f"✅ OZON_PERFORMANCE_CLIENT_ID: {mask_secret(OZON_PERFORMANCE_CLIENT_ID)}")
    update_version_history(VERSION, CHANGELOG_MESSAGE)

    application = (Application.builder()
                   .token(TELEGRAM_BOT_TOKEN)
                   .connect_timeout(30.0)
                   .read_timeout(30.0)
                   .write_timeout(30.0)
                   .post_init(init_http_session)
                   .post_shutdown(close_http_session)
                   .build())

    write_log("🚀 Запуск бота...")
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("top", top_products_command))
    application.add_handler(CommandHandler("version", version_command))

    async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if is_admin(chat_id):
            help_text = (
                "📖 *Справка для администратора*\n\n"
                "🔹 *Основные функции*\n"
                "• 📊 Отчёт по продажам – актуальная сводка по продажам за сегодня и текущий месяц.\n"
                "• 📦 Отчёт по товарам – топ товаров по выручке за сегодня и текущий месяц.\n"
                "• 📆 Выбрать дату – просмотр данных за конкретный день (продажи или товары).\n"
                "• 📊 Выбрать период – гибкий выбор отчётного периода (месяц, квартал, год, произвольный).\n"
                "• 📈 Динамика продаж – график доставленных заказов по месяцам за выбранный год (или несколько лет).\n"
                "• 📈 Динамика по товару – график продаж конкретного товара по месяцам.\n"
                "• ⚙️ Администрирование – управление доступом менеджеров.\n"
                "• 🔔 Автоматические рассылки – настройка времени и содержания автоматических отчётов.\n\n"
                "🔹 *Управление менеджерами*\n"
                "• ➕ Добавить менеджера – введите Telegram ID или @username пользователя, затем номер телефона (или '-' для пропуска).\n"
                "• ➖ Удалить менеджера – введите Telegram ID пользователя.\n"
                "• 📋 Список менеджеров – просмотр всех добавленных пользователей (ID, username, имя, телефон).\n\n"
                "🔹 *Автоматические отчёты*\n"
                "• Настройка времени рассылок – задаются часы (01:00..24:00), в которые разрешена отправка.\n"
                "• Добавление отчёта за Вчера – из разрешённых часов выбираются те, в которые будет отправляться отчёт с блоком «Вчера».\n"
                "• Режим тишины – задаётся интервал, в течение которого рассылки не отправляются.\n"
                "• Отправить сейчас – принудительная отправка отчёта за вчера всем пользователям.\n\n"
                "🔹 *Метрики*\n"
                "• 🛒 Заказано – сумма и количество всех заказов.\n"
                "• 📦 Доставлено – сумма и количество доставленных заказов.\n"
                "• ❌ Отменено – сумма и количество отменённых заказов.\n"
                "• 📢 Реклама – расходы на рекламу, ДРР (общий) и ДРР (по доставленным).\n"
                "• 💰 Расходы (финансовые) – детальная разбивка: комиссии, логистика, эквайринг, кросс-докинг, хранение, возвраты и др.\n\n"
                "🔹 *Сравнение динамики*\n"
                "• Для «Сегодня» – сравнение с аналогичным временем вчера.\n"
                "• Для «Текущий месяц» – сравнение с аналогичным периодом предыдущего месяца (с учётом времени).\n\n"
                "🔹 *Часовой пояс*\n"
                "• Все расчёты ведутся по московскому времени (МСК, UTC+3).\n\n"
                f"🤖 Версия бота: {VERSION}"
            )
        else:
            help_text = (
                "📖 *Справка для менеджера*\n\n"
                "🔹 *Основные функции*\n"
                "• 📊 Отчёт по продажам – актуальная сводка по продажам за сегодня и текущий месяц.\n"
                "• 📦 Отчёт по товарам – топ товаров по выручке за сегодня и текущий месяц.\n"
                "• 📆 Выбрать дату – просмотр данных за конкретный день (продажи или товары).\n"
                "• 📊 Выбрать период – гибкий выбор отчётного периода (месяц, квартал, год, произвольный).\n"
                "• 📈 Динамика продаж – график доставленных заказов по месяцам за выбранный год (или несколько лет).\n"
                "• 📈 Динамика по товару – график продаж конкретного товара по месяцам.\n"
                "• 🔔 Автоматические рассылки – настройка времени и содержания автоматических отчётов.\n\n"
                "🔹 *Автоматические отчёты*\n"
                "• Настройка времени рассылок – задаются часы (01:00..24:00), в которые разрешена отправка.\n"
                "• Добавление отчёта за Вчера – из разрешённых часов выбираются те, в которые будет отправляться отчёт с блоком «Вчера».\n"
                "• Режим тишины – задаётся интервал, в течение которого рассылки не отправляются.\n"
                "• Отправить сейчас – принудительная отправка отчёта за вчера всем пользователям.\n\n"
                "🔹 *Метрики*\n"
                "• 🛒 Заказано – сумма и количество всех заказов.\n"
                "• 📦 Доставлено – сумма и количество доставленных заказов.\n"
                "• ❌ Отменено – сумма и количество отменённых заказов.\n"
                "• 📢 Реклама – расходы на рекламу, ДРР (общий) и ДРР (по доставленным).\n"
                "• 💰 Расходы (финансовые) – детальная разбивка: комиссии, логистика, эквайринг, кросс-докинг, хранение, возвраты и др.\n\n"
                "🔹 *Сравнение динамики*\n"
                "• Для «Сегодня» – сравнение с аналогичным временем вчера.\n"
                "• Для «Текущий месяц» – сравнение с аналогичным периодом предыдущего месяца (с учётом времени).\n\n"
                "🔹 *Часовой пояс*\n"
                "• Все расчёты ведутся по московскому времени (МСК, UTC+3).\n\n"
                f"🤖 Версия бота: {VERSION}"
            )
        await update.message.reply_text(help_text, parse_mode="Markdown")
    application.add_handler(CommandHandler("help", help_command))

    # Обработчики меню
    application.add_handler(MessageHandler(filters.Text(["📊 Отчёт по продажам", "📦 Отчёт по товарам", "🔔 Автоматические рассылки", "⚙️ Администрирование", "📖 Справка"]), handle_main_menu))

    application.add_handler(MessageHandler(filters.Text(["📅 Продажи за сегодня", "📆 Выбрать дату", "📊 Выбрать период", "📈 Динамика продаж", "🔙 Назад"]), handle_sales_reports))
    application.add_handler(MessageHandler(filters.Text(["📅 Топ товаров за сегодня", "📆 Выбрать дату (товары)", "📊 Выбрать период (товары)", "📈 Динамика по товару", "🔙 Назад"]), handle_products_reports))
    application.add_handler(MessageHandler(filters.Text(["📋 Список менеджеров", "🔙 Назад"]), handle_admin_menu))

    # Диалоги продаж
    conv_date = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📆 Выбрать дату"), handle_sales_reports)],
        states={WAITING_DATE_SINGLE: [CallbackQueryHandler(handle_callback_query)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    conv_period = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📊 Выбрать период"), handle_sales_reports)],
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
    conv_dynamics = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📈 Динамика продаж"), handle_sales_reports)],
        states={
            WAITING_DYNAMICS_SELECT: [CallbackQueryHandler(handle_callback_query)],
            WAITING_DYNAMICS_RANGE_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, dynamics_range_start)],
            WAITING_DYNAMICS_RANGE_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, dynamics_range_end)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Диалоги товаров
    conv_product_date = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📆 Выбрать дату (товары)"), handle_products_reports)],
        states={WAITING_PRODUCT_DATE: [CallbackQueryHandler(handle_callback_query)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    conv_product_period = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📊 Выбрать период (товары)"), handle_products_reports)],
        states={
            WAITING_PRODUCT_PERIOD_TYPE: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PRODUCT_PERIOD_START: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PRODUCT_PERIOD_END: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PRODUCT_YEAR: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PRODUCT_MONTH: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PRODUCT_QUARTER: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PRODUCT_YEAR_SELECT: [CallbackQueryHandler(handle_callback_query)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    conv_product_chart = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📈 Динамика по товару"), handle_products_reports)],
        states={
            WAITING_PRODUCT_SELECT: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PRODUCT_METRIC: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PRODUCT_PERIOD_CHOICE: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PRODUCT_SINGLE_YEAR: [CallbackQueryHandler(handle_callback_query)],
            WAITING_PRODUCT_RANGE_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_range_start)],
            WAITING_PRODUCT_RANGE_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_range_end)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Администрирование
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
    application.add_handler(conv_dynamics)
    application.add_handler(conv_product_date)
    application.add_handler(conv_product_period)
    application.add_handler(conv_product_chart)
    application.add_handler(conv_add)
    application.add_handler(conv_remove)

    # Обработчик для автоматических рассылок
    async def auto_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if text == "🕒 Выбор времени рассылок":
            await auto_schedule_start(update, context)
        elif text == "📅 Добавление отчета за Вчера":
            await auto_yesterday_start(update, context)
        elif text == "🔕 Режим тишины":
            await auto_silence_start(update, context)
        elif text == "📤 Отправить отчет за Вчера сейчас":
            await auto_send_yesterday_now(update, context)
        elif text == "🔙 Назад":
            if is_admin(update.effective_chat.id):
                await update.message.reply_text(f"Главное меню\n\n🤖 Версия бота: {VERSION}", reply_markup=main_admin_keyboard())
            else:
                await update.message.reply_text(f"Главное меню\n\n🤖 Версия бота: {VERSION}", reply_markup=main_user_keyboard())
    application.add_handler(MessageHandler(filters.Regex("^(🕒 Выбор времени рассылок|📅 Добавление отчета за Вчера|🔕 Режим тишины|📤 Отправить отчет за Вчера сейчас|🔙 Назад)$"), auto_menu_router))

    # Единый обработчик всех callback-запросов
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Планировщик
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(check_auto_reports, interval=900, first=0)
        write_log("✅ Планировщик автоматических рассылок запущен (интервал 15 минут).")
    else:
        write_log("⚠️ JobQueue недоступен.")

    write_log("🚀 Бот готов.")
    application.run_polling(allowed_updates=Update.ALL_TYPES, timeout=30)

if __name__ == "__main__":
    main()
