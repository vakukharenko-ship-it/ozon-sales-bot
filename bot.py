import datetime
import json
import os
import time
import re
import hashlib
import calendar as cal_mod
import asyncio
import aiohttp
import warnings
import sys
import io
from typing import Optional, List, Dict

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters,
    ConversationHandler, CallbackQueryHandler
)
from telegram.warnings import PTBUserWarning

warnings.filterwarnings("ignore", category=PTBUserWarning)

VERSION = "2.7.0"
CHANGELOG_MESSAGE = "Отчёт по рекламе: 'Реклама за период' + 'Динамика по рекламе'. Кэш рекламы. SKU/артикул через слэш."

# ==================== КОНСТАНТЫ ====================
API_TIMEOUT = 60
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY = 10
POSTINGS_RATE = 0.5
FINANCE_RATE = 0.5
PERFORMANCE_RATE = 1.0
CACHE_TTL_SECONDS = 3600
DATA_DIR = "/app/data"
DISK_CACHE_DIR = "/app/data/cache"
VERSION_HISTORY_FILE = "/app/data/version_history.json"
SETTINGS_FILE = "/app/data/settings.json"
MANAGERS_FILE = "/app/data/managers.json"
EXPENSE_TYPES_FILE = "/app/data/expense_types.json"
LOG_FILE = "/app/data/ozon_log.txt"

AD_CHUNK_DAYS = 60

WAITING_DATE_SINGLE = 1
WAITING_PERIOD_TYPE = 2
WAITING_PERIOD_START = 3
WAITING_PERIOD_END = 4
WAITING_PERIOD_YEAR = 5
WAITING_PERIOD_MONTH = 6
WAITING_PERIOD_QUARTER = 7
WAITING_YEAR_SELECT = 8

WAITING_PRODUCT_PERIOD_TYPE = 21
WAITING_PRODUCT_PERIOD_START = 22
WAITING_PRODUCT_PERIOD_END = 23
WAITING_PRODUCT_YEAR = 24
WAITING_PRODUCT_MONTH = 25
WAITING_PRODUCT_QUARTER = 26
WAITING_PRODUCT_YEAR_SELECT = 27

WAITING_DYN_SELECT = 40
WAITING_DYN_YEAR = 41
WAITING_DYN_RANGE_START = 42
WAITING_DYN_RANGE_END = 43

WAITING_PC_LIST = 50
WAITING_PC_METRIC = 51
WAITING_PC_PERIOD = 52
WAITING_PC_YEAR = 53
WAITING_PC_RANGE_START = 54
WAITING_PC_RANGE_END = 55

WAITING_ADD_MANAGER = 100
WAITING_MANAGER_PHONE = 101
WAITING_REMOVE_MANAGER = 102

WAITING_AUTO_SCHEDULE = 110
WAITING_AUTO_YESTERDAY = 111
WAITING_AUTO_SILENCE_START = 112
WAITING_AUTO_SILENCE_END = 113

WAITING_AD_REPORT_PERIOD_TYPE = 200
WAITING_AD_REPORT_PERIOD_YEAR = 201
WAITING_AD_REPORT_PERIOD_MONTH = 202
WAITING_AD_REPORT_PERIOD_QUARTER = 203
WAITING_AD_REPORT_PERIOD_START = 204
WAITING_AD_REPORT_PERIOD_END = 205

WAITING_AD_DYN_PERIOD_TYPE = 210
WAITING_AD_DYN_YEAR = 211
WAITING_AD_DYN_RANGE_START = 212
WAITING_AD_DYN_RANGE_END = 213
WAITING_AD_DYN_CAMPAIGN_SELECT = 214
WAITING_AD_DYN_METRIC = 215

# ==================== ГЛОБАЛЬНЫЕ ====================
_http_session = None
_postings_sem = None
_finance_sem = None
_perf_sem = None
_postings_rate = None
_finance_rate = None
_perf_rate = None
_cache_lock = asyncio.Lock()
_api_cache = {}
_cache_timestamps = {}
_settings_lock = asyncio.Lock()
_expense_types_cache = None

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
OZON_PERFORMANCE_CLIENT_ID = os.getenv("OZON_PERFORMANCE_CLIENT_ID")
OZON_PERFORMANCE_CLIENT_SECRET = os.getenv("OZON_PERFORMANCE_CLIENT_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID_STR = os.getenv("ADMIN_CHAT_ID")
ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_STR) if ADMIN_CHAT_ID_STR and ADMIN_CHAT_ID_STR.isdigit() else 0

OZON_POSTING_FBO_URL = "https://api-seller.ozon.ru/v3/posting/fbo/list"
OZON_FINANCE_ACCRUAL_BY_DAY_URL = "https://api-seller.ozon.ru/v1/finance/accrual/by-day"

MOSCOW_TZ = datetime.timezone(datetime.timedelta(hours=3))

DEFAULT_EXPENSE_TYPES = {
    "1": "Обработка товара", "12": "Прочие услуги Ozon", "29": "Последняя миля",
    "32": "Логистика", "76": "Внешние услуги Ozon", "98": "Доп. услуги отправления",
}
CATEGORY_FALLBACK = {"ITEM": "Услуги по товарам", "NON_ITEM": "Внешние услуги Ozon",
                     "POSTING": "Услуги отправления", "CONTAINER": "Контейнеры"}
METRIC_LABELS = {
    "ordered_sum": "Заказано (₽)", "ordered_units": "Заказано (шт.)",
    "delivered_sum": "Доставлено (₽)", "delivered_units": "Доставлено (шт.)",
    "canceled_sum": "Отменено (₽)", "canceled_units": "Отменено (шт.)",
    "avg_check": "Средний чек (₽)",
}

AD_DYN_METRICS = [
    ("avg_bid", "Ваша ставка, руб.", False),
    ("avg_cpc", "Средняя стоимость клика, руб.", False),
    ("ad_sales", "Продано товаров в рекламе, руб.", True),
    ("total_sales", "Продано товаров ВСЕГО, руб.", True),
    ("expense", "Расход, руб.", False),
    ("drr_ad", "ДРР в рекламной компании, %", False),
    ("drr_total", "ДРР ОБЩИЙ, %", False),
    ("impressions", "Показы, кол-во.", True),
    ("clicks", "Клики, кол-во.", True),
    ("carts", "Добавления в корзину, кол-во.", True),
    ("ctr", "CTR, %", True),
]

# ==================== ЛОГИ ====================
def write_log(message):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = f"[{ts}] {message}"
    print(msg, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception as e:
        print(f"⚠️ Ошибка лога: {e}")

def mask_secret(value, visible=4):
    if not value or len(value) <= visible: return "***"
    return f"{value[:visible]}***"

def validate_env_vars() -> bool:
    req = {"OZON_CLIENT_ID": OZON_CLIENT_ID, "OZON_API_KEY": OZON_API_KEY,
           "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN, "ADMIN_CHAT_ID": ADMIN_CHAT_ID_STR}
    missing = [k for k, v in req.items() if not v]
    if missing:
        write_log(f"❌ Missing env vars: {', '.join(missing)}"); return False
    if not ADMIN_CHAT_ID_STR.isdigit():
        write_log("❌ ADMIN_CHAT_ID должен быть числом"); return False
    return True

def update_version_history(version, message):
    try:
        os.makedirs(os.path.dirname(VERSION_HISTORY_FILE), exist_ok=True)
    except Exception as e:
        write_log(f"⚠️ Папка истории: {e}")
    history = []
    if os.path.exists(VERSION_HISTORY_FILE):
        try:
            with open(VERSION_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception as e:
            write_log(f"⚠️ Чтение истории: {e}"); history = []
    for e in history:
        if e.get("version") == version:
            write_log(f"ℹ️ Версия {version} уже в истории."); return
    now = datetime.datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S")
    history.append({"version": version, "date": now, "message": message})
    try:
        with open(VERSION_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        write_log(f"✅ История: {version} (записей: {len(history)})")
    except Exception as e:
        write_log(f"❌ Запись истории: {e}")

# ==================== СПРАВОЧНИК РАСХОДОВ ====================
def _init_expense_types_file():
    if os.path.exists(EXPENSE_TYPES_FILE): return
    try:
        os.makedirs(os.path.dirname(EXPENSE_TYPES_FILE), exist_ok=True)
        data = {tid: {"name": name, "count": 0, "sum": 0.0}
                for tid, name in DEFAULT_EXPENSE_TYPES.items()}
        with open(EXPENSE_TYPES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        write_log(f"❌ Ошибка создания справочника: {e}")

def _load_expense_types_sync():
    if not os.path.exists(EXPENSE_TYPES_FILE): return {}
    try:
        with open(EXPENSE_TYPES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        write_log(f"⚠️ Чтение справочника: {e}"); return {}

def _save_expense_types_sync(data):
    try:
        os.makedirs(os.path.dirname(EXPENSE_TYPES_FILE), exist_ok=True)
        with open(EXPENSE_TYPES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        write_log(f"❌ Запись справочника: {e}")

def get_expense_types():
    global _expense_types_cache
    if _expense_types_cache is None:
        _expense_types_cache = _load_expense_types_sync()
    return _expense_types_cache

def type_name(type_id, category_code):
    if type_id is None:
        return CATEGORY_FALLBACK.get(category_code, "Услуги")
    tid = str(type_id)
    data = get_expense_types()
    if tid in data:
        entry = data[tid]
        if isinstance(entry, dict):
            return entry.get("name", f"❓ Неизвестно (type {tid})")
        return entry
    fallback = CATEGORY_FALLBACK.get(category_code, "Услуги")
    placeholder = f"{fallback} (type {tid})"
    data[tid] = {"name": placeholder, "count": 0, "sum": 0.0}
    _save_expense_types_sync(data)
    return placeholder

def register_expense_hit(type_id, amount):
    if type_id is None: return
    tid = str(type_id)
    data = get_expense_types()
    if tid not in data: return
    entry = data[tid]
    if not isinstance(entry, dict):
        entry = {"name": entry, "count": 0, "sum": 0.0}
        data[tid] = entry
    entry["count"] = entry.get("count", 0) + 1
    entry["sum"] = round(entry.get("sum", 0.0) + amount, 2)

def flush_expense_types():
    global _expense_types_cache
    if _expense_types_cache is not None:
        _save_expense_types_sync(_expense_types_cache)

# ==================== DISK CACHE ====================
def _disk_path(key):
    h = hashlib.md5(key.encode()).hexdigest()[:16]
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', key)[:40]
    return os.path.join(DISK_CACHE_DIR, f"{safe}_{h}.json")

def disk_cache_get(key):
    path = _disk_path(key)
    if not os.path.exists(path): return None
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except: return None

def disk_cache_set(key, value):
    try:
        os.makedirs(DISK_CACHE_DIR, exist_ok=True)
        with open(_disk_path(key), "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
    except Exception as e:
        write_log(f"⚠️ disk-cache: {e}")

async def get_from_cache(key):
    async with _cache_lock:
        if key in _api_cache:
            ts = _cache_timestamps.get(key)
            if ts and (time.time() - ts) < CACHE_TTL_SECONDS:
                return _api_cache[key]
    val = disk_cache_get(key)
    if val is not None:
        async with _cache_lock:
            _api_cache[key] = val; _cache_timestamps[key] = time.time()
        write_log(f"💾 Кэш-хит: {key[:50]}")
    return val

async def save_to_cache(key, value):
    async with _cache_lock:
        _api_cache[key] = value; _cache_timestamps[key] = time.time()
    disk_cache_set(key, value)

# ==================== НАСТРОЙКИ / МЕНЕДЖЕРЫ ====================
def _ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(DISK_CACHE_DIR, exist_ok=True)
    except Exception as e:
        write_log(f"⚠️ Создание {DATA_DIR}: {e}")

async def load_settings() -> Dict:
    async with _settings_lock:
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                write_log(f"⚠️ settings: {e}"); return {}
        return {}

async def save_settings(settings: Dict):
    async with _settings_lock:
        try:
            os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            write_log(f"❌ settings: {e}")

def get_user_settings(settings: Dict, chat_id: int) -> Dict:
    sid = str(chat_id)
    if sid not in settings:
        settings[sid] = {"schedule_hours": [], "yesterday_report_hours": [],
                         "silence_start": None, "silence_end": None,
                         "last_sent_regular": None, "last_sent_yesterday": None,
                         "last_reminder": None}
    us = settings[sid]
    if "last_sent" in us:
        us.setdefault("last_sent_regular", us.get("last_sent"))
        us.setdefault("last_sent_yesterday", None)
        us.pop("last_sent", None)
    us.setdefault("last_sent_regular", None)
    us.setdefault("last_sent_yesterday", None)
    return us

def user_has_schedule(chat_id: int) -> bool:
    if not os.path.exists(SETTINGS_FILE): return False
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        us = s.get(str(chat_id), {})
        return bool(us.get("schedule_hours"))
    except: return False

def load_managers():
    if os.path.exists(MANAGERS_FILE):
        try:
            with open(MANAGERS_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return []
    return []

def save_managers(managers):
    try:
        os.makedirs(os.path.dirname(MANAGERS_FILE), exist_ok=True)
        with open(MANAGERS_FILE, "w", encoding="utf-8") as f:
            json.dump(managers, f, ensure_ascii=False, indent=2)
    except Exception as e:
        write_log(f"❌ managers: {e}")

def is_manager(chat_id): return any(m.get("id") == chat_id for m in load_managers())
def is_admin(chat_id): return chat_id == ADMIN_CHAT_ID
def has_access(chat_id): return is_admin(chat_id) or is_manager(chat_id)

def add_manager(chat_id, username=None, first_name=None, last_name=None, phone=None):
    managers = load_managers()
    for m in managers:
        if m.get("id") == chat_id: return False
    managers.append({"id": chat_id, "username": username or "", "first_name": first_name or "",
                     "last_name": last_name or "", "phone": phone or ""})
    save_managers(managers); return True

def remove_manager(chat_id):
    managers = load_managers()
    new_m = [m for m in managers if m.get("id") != chat_id]
    if len(new_m) == len(managers): return False
    save_managers(new_m); return True

# ==================== УТИЛИТЫ ====================
def get_moscow_today(): return datetime.datetime.now(MOSCOW_TZ).date()
def get_current_time_msk(): return datetime.datetime.now(MOSCOW_TZ)
def fmt_num(val): return f"{val:,.2f}".replace(",", " ") if val else "0.00"
def fmt_int(val): return str(val) if val else "0"

def parse_money(obj) -> float:
    if obj is None: return 0.0
    if isinstance(obj, dict):
        val = obj.get("amount", obj.get("value", 0))
    else: val = obj
    try: return float(str(val).replace(",", "."))
    except (TypeError, ValueError): return 0.0

def parse_price(product) -> float: return parse_money(product.get("price"))

def calc_delta(cur, prev):
    if prev == 0: return None
    try: return ((cur - prev) / abs(prev)) * 100
    except: return None

def fmt_pct(val):
    if val is None: return "∞"
    return f"+{val:.1f}%" if val > 0 else f"{val:.1f}%"

def indicator(value_current, value_prev, better_is_higher):
    if value_prev is None or value_current is None: return ""
    if value_prev == 0:
        return "🟢" if value_current > 0 else ""
    delta = calc_delta(value_current, value_prev)
    if delta is None: return ""
    if better_is_higher:
        return "🟢" if delta > 0 else ("🔴" if delta < 0 else "")
    return "🟢" if delta < 0 else ("🔴" if delta > 0 else "")

def validate_date(date_str):
    try:
        d = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        today = get_moscow_today()
        if d > today: return False, "❌ Дата не может быть в будущем"
        if d < today - datetime.timedelta(days=730):
            return False, "❌ Дата слишком старая"
        return True, d
    except ValueError: return False, "❌ Неверный формат даты"

def validate_period(df, dt):
    ok1, r1 = validate_date(df)
    if not ok1: return False, r1
    ok2, r2 = validate_date(dt)
    if not ok2: return False, r2
    if r1 > r2: return False, "❌ Начальная дата позже конечной"
    if (r2 - r1).days > 365: return False, "❌ Период больше года"
    return True, (r1, r2)

def prev_period(df, dt):
    d1 = datetime.datetime.strptime(df, "%Y-%m-%d").date()
    d2 = datetime.datetime.strptime(dt, "%Y-%m-%d").date()
    length = (d2 - d1).days + 1
    prev_end = d1 - datetime.timedelta(days=1)
    prev_start = prev_end - datetime.timedelta(days=length - 1)
    return prev_start.isoformat(), prev_end.isoformat()

# ==================== RATE LIMITER ====================
class RateLimiter:
    def __init__(self, rate):
        self.rate = rate; self.lock = asyncio.Lock(); self.last = 0
    async def acquire(self):
        async with self.lock:
            now = time.time()
            w = (1.0 / self.rate) - (now - self.last)
            if w > 0: await asyncio.sleep(w)
            self.last = time.time()

# ==================== INIT ====================
async def init_http_session(app):
    global _http_session, _postings_sem, _finance_sem, _perf_sem
    global _postings_rate, _finance_rate, _perf_rate
    _postings_sem = asyncio.Semaphore(1)
    _finance_sem = asyncio.Semaphore(1)
    _perf_sem = asyncio.Semaphore(1)
    _postings_rate = RateLimiter(POSTINGS_RATE)
    _finance_rate = RateLimiter(FINANCE_RATE)
    _perf_rate = RateLimiter(PERFORMANCE_RATE)
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    connector = aiohttp.TCPConnector(limit=50, limit_per_host=10, ttl_dns_cache=300)
    _http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    write_log(f"✅ HTTP-сессия (v{VERSION})")

async def close_http_session(app):
    global _http_session
    if _http_session:
        await _http_session.close()

# ==================== API ====================
async def api_request_with_retry(url, headers, payload=None, method='POST', kind='finance', silent_404=False):
    global _http_session
    if kind == 'perf': sem, rate = _perf_sem, _perf_rate
    elif kind == 'postings': sem, rate = _postings_sem, _postings_rate
    else: sem, rate = _finance_sem, _finance_rate
    async with sem:
        for attempt in range(API_RETRY_ATTEMPTS):
            try:
                await rate.acquire()
                if method == 'POST':
                    async with _http_session.post(url, headers=headers, json=payload) as resp:
                        body = await resp.text()
                        if resp.status == 404 and silent_404: return None
                        if resp.status == 429:
                            w = API_RETRY_DELAY * (2 ** attempt)
                            await asyncio.sleep(w); continue
                        if resp.status >= 400:
                            if not silent_404: write_log(f"❌ API {resp.status}: {body[:300]}")
                            raise aiohttp.ClientResponseError(resp.request_info, resp.history,
                                status=resp.status, message=body[:200])
                        return json.loads(body)
                else:
                    async with _http_session.get(url, headers=headers, params=payload) as resp:
                        body = await resp.text()
                        if resp.status == 404 and silent_404: return None
                        if resp.status == 429:
                            w = API_RETRY_DELAY * (2 ** attempt)
                            await asyncio.sleep(w); continue
                        if resp.status >= 400:
                            if not silent_404: write_log(f"❌ API {resp.status}: {body[:300]}")
                            raise aiohttp.ClientResponseError(resp.request_info, resp.history,
                                status=resp.status, message=body[:200])
                        return json.loads(body)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == API_RETRY_ATTEMPTS - 1:
                    if not silent_404: write_log(f"❌ API failed: {e}")
                    raise
                await asyncio.sleep(API_RETRY_DELAY * (attempt + 1))
        raise Exception("API failed")

async def get_performance_token():
    if not OZON_PERFORMANCE_CLIENT_ID or not OZON_PERFORMANCE_CLIENT_SECRET: return None
    url = "https://api-performance.ozon.ru/api/client/token"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    payload = {"client_id": OZON_PERFORMANCE_CLIENT_ID,
               "client_secret": OZON_PERFORMANCE_CLIENT_SECRET,
               "grant_type": "client_credentials"}
    try:
        data = await api_request_with_retry(url, headers, payload, method='POST', kind='perf')
        return data.get("access_token")
    except Exception as e:
        write_log(f"❌ Токен: {e}"); return None

# ==================== ОТГРУЗКИ ====================
async def fetch_postings(date_from, date_to):
    cache_key = f"postings_{date_from}_{date_to}"
    cached = await get_from_cache(cache_key)
    if cached is not None: return cached
    headers = {"Client-Id": OZON_CLIENT_ID, "Api-Key": OZON_API_KEY, "Content-Type": "application/json"}
    since = f"{date_from}T00:00:00Z"; to = f"{date_to}T23:59:59Z"
    all_p = []; cursor = ""; LIMIT = 100; page = 0
    while True:
        page += 1
        payload = {"dir": "ASC", "filter": {"since": since, "to": to},
                   "limit": LIMIT, "translit": False,
                   "with": {"analytics_data": True, "financial_data": True}}
        if cursor: payload["cursor"] = cursor
        try:
            data = await api_request_with_retry(OZON_POSTING_FBO_URL, headers, payload,
                                                method='POST', kind='postings')
        except Exception as e:
            write_log(f"❌ FBO: {e}"); break
        postings = data.get("postings", [])
        if not postings: break
        all_p.extend(postings)
        if not data.get("has_next"): break
        cursor = data.get("cursor", "")
        if not cursor or len(postings) < LIMIT: break
    write_log(f"📦 Отгрузок: {len(all_p)} за {date_from}–{date_to}")
    await save_to_cache(cache_key, all_p)
    return all_p

# ==================== РЕКЛАМА (общая сумма расходов) ====================
async def _fetch_advertising_expense_single(date_from, date_to):
    token = await get_performance_token()
    if not token: return 0.0
    url = "https://api-performance.ozon.ru/api/client/statistics/expense/json"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    params = {"dateFrom": date_from, "dateTo": date_to}
    try:
        data = await api_request_with_retry(url, headers, params, method='GET', kind='perf')
        total = 0.0
        if isinstance(data, dict) and "rows" in data:
            for item in data["rows"]:
                d = item.get("date", "")[:10]
                if date_from <= d <= date_to:
                    ms = item.get("moneySpent")
                    if ms is not None:
                        try: total += float(str(ms).replace(",", "."))
                        except: pass
        return total
    except Exception as e:
        write_log(f"❌ Реклама ({date_from}–{date_to}): {e}")
        return 0.0

async def fetch_advertising_expense(date_from, date_to):
    cache_key = f"ad_{date_from}_{date_to}"
    cached = await get_from_cache(cache_key)
    if cached is not None: return cached
    start_dt = datetime.datetime.strptime(date_from, "%Y-%m-%d").date()
    end_dt = datetime.datetime.strptime(date_to, "%Y-%m-%d").date()
    today = get_moscow_today()
    if start_dt > today: return 0.0
    if end_dt > today: end_dt = today
    total_days = (end_dt - start_dt).days + 1
    if total_days <= AD_CHUNK_DAYS:
        result = await _fetch_advertising_expense_single(date_from, end_dt.isoformat())
        await save_to_cache(cache_key, result)
        return result
    total = 0.0
    cur = start_dt
    while cur <= end_dt:
        chunk_end = min(cur + datetime.timedelta(days=AD_CHUNK_DAYS - 1), end_dt)
        part = await _fetch_advertising_expense_single(cur.isoformat(), chunk_end.isoformat())
        total += part
        cur = chunk_end + datetime.timedelta(days=1)
    await save_to_cache(cache_key, total)
    return total

# ==================== ФИНАНСЫ ====================
async def fetch_finance_accruals_by_day(date_str):
    cache_key = f"fin_day_{date_str}"
    cached = await get_from_cache(cache_key)
    if cached is not None: return cached
    headers = {"Client-Id": OZON_CLIENT_ID, "Api-Key": OZON_API_KEY, "Content-Type": "application/json"}
    payload = {"date": date_str}; all_a = []
    while True:
        try:
            data = await api_request_with_retry(OZON_FINANCE_ACCRUAL_BY_DAY_URL, headers,
                                                payload, method='POST', kind='finance')
        except Exception as e:
            write_log(f"❌ Финансы {date_str}: {e}"); break
        accruals = data.get("accruals", [])
        if not accruals: break
        all_a.extend(accruals)
        last = data.get("last_id")
        if last: payload["last_id"] = last
        else: break
    await save_to_cache(cache_key, all_a)
    return all_a

async def fetch_finance_transactions(date_from, date_to, status_msg=None):
    cache_key = f"fin_{date_from}_{date_to}"
    cached = await get_from_cache(cache_key)
    if cached is not None: return cached
    start = datetime.datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.datetime.strptime(date_to, "%Y-%m-%d").date()
    today = get_moscow_today()
    if start > today: return []
    if end > today: end = today
    days = []; cur = start
    while cur <= end:
        days.append(cur.isoformat()); cur += datetime.timedelta(days=1)
    all_a = []; missing = []
    for d in days:
        c = disk_cache_get(f"fin_day_{d}")
        if c is not None: all_a.extend(c)
        else: missing.append(d)
    if missing:
        total = len(missing)
        for i, d in enumerate(missing):
            if status_msg and (i % 30 == 0 or i == total - 1):
                try:
                    await status_msg.edit_text(
                        f"⏳ Финансы: загружено {i+1}/{total} дней\n"
                        f"_(всего {len(days)} дн., {date_from} – {date_to})_",
                        parse_mode="Markdown")
                except Exception: pass
            day_acc = await fetch_finance_accruals_by_day(d)
            all_a.extend(day_acc)
    await save_to_cache(cache_key, all_a)
    return all_a

# ==================== АГРЕГАЦИЯ ФИНАНСОВ ====================
def aggregate_finance_expenses(accruals):
    result = {}
    def add(name, amount):
        if amount > 0: result[name] = result.get(name, 0) + amount
    for item in accruals:
        if not isinstance(item, dict): continue
        cat = item.get("accrued_category", "")
        if cat == "POSTING":
            posting = item.get("posting") or {}
            for product in posting.get("products", []) or []:
                comm = product.get("commission") or {}
                sc = parse_money(comm.get("sale_commission"))
                if sc < 0: add("Комиссия Ozon", abs(sc))
                delivery = product.get("delivery") or {}
                for svc in delivery.get("services", []) or []:
                    tid = svc.get("type_id"); amt = parse_money(svc.get("accrued"))
                    if amt < 0:
                        name = type_name(tid, cat); add(name, abs(amt)); register_expense_hit(tid, abs(amt))
            continue
        if cat == "ITEM":
            i_fees = item.get("item_fees") or {}
            for grp in i_fees.get("fees", []) or []:
                for fee in grp.get("fees", []) or []:
                    tid = fee.get("type_id"); amt = parse_money(fee.get("accrued"))
                    if amt < 0:
                        name = type_name(tid, cat); add(name, abs(amt)); register_expense_hit(tid, abs(amt))
            continue
        if cat == "NON_ITEM":
            non = item.get("non_item_fee") or {}
            tid = non.get("type_id"); amt = parse_money(non.get("accrued"))
            if amt < 0:
                name = type_name(tid, cat); add(name, abs(amt)); register_expense_hit(tid, abs(amt))
            continue
        if cat == "CONTAINER":
            cont = item.get("container_fees") or {}
            amt = parse_money(cont.get("accrued", cont))
            if amt < 0: add("Контейнеры", abs(amt))
            continue
        total = parse_money(item.get("total_amount"))
        if total < 0: add(cat or "Прочее", abs(total))
    flush_expense_types()
    return result

def aggregate_postings_range(postings, df, dt):
    r = {"ordered_units": 0, "ordered_sum": 0.0, "delivered_units": 0,
         "delivered_sum": 0.0, "canceled_units": 0, "canceled_sum": 0.0}
    for p in postings:
        if not isinstance(p, dict): continue
        ca = p.get("created_at", "")
        if not ca: continue
        try:
            dtx = datetime.datetime.fromisoformat(ca.replace('Z', '+00:00')).astimezone(MOSCOW_TZ)
        except: continue
        ds = dtx.date().isoformat()
        if df and ds < df: continue
        if dt and ds > dt: continue
        u, s = 0, 0.0
        for prod in p.get("products", []):
            if not isinstance(prod, dict): continue
            q = int(prod.get("quantity", 0))
            s += parse_price(prod) * q; u += q
        st = p.get("status", "")
        r["ordered_units"] += u; r["ordered_sum"] += s
        if st in ("cancelled", "canceled"):
            r["canceled_units"] += u; r["canceled_sum"] += s
        elif st in ("delivered", "completed"):
            r["delivered_units"] += u; r["delivered_sum"] += s
    return r

def aggregate_products(postings, df, dt, tl=None, ad=None):
    stats = {}
    for p in postings:
        if not isinstance(p, dict): continue
        ca = p.get("created_at", "")
        if not ca: continue
        try:
            dtx = datetime.datetime.fromisoformat(ca.replace('Z', '+00:00')).astimezone(MOSCOW_TZ)
        except: continue
        ds = dtx.date().isoformat()
        if df and ds < df: continue
        if dt and ds > dt: continue
        if tl is not None and ad is not None and ds == ad and dtx.time() > tl: continue
        st = p.get("status", "")
        for prod in p.get("products", []):
            if not isinstance(prod, dict): continue
            sku = str(prod.get("sku", "0")); name = prod.get("name", "—"); oid = prod.get("offer_id", "")
            q = int(prod.get("quantity", 0)); price = parse_price(prod)
            if sku not in stats:
                stats[sku] = {"name": name[:60], "offer_id": oid,
                              "ordered_units": 0, "ordered_sum": 0.0,
                              "delivered_units": 0, "delivered_sum": 0.0,
                              "canceled_units": 0, "canceled_sum": 0.0,
                              "order_count": 0}
            s = stats[sku]
            s["ordered_units"] += q; s["ordered_sum"] += price * q
            s["order_count"] += 1
            if st in ("delivered", "completed"):
                s["delivered_units"] += q; s["delivered_sum"] += price * q
            elif st in ("cancelled", "canceled"):
                s["canceled_units"] += q; s["canceled_sum"] += price * q
    return stats

def build_sku_offer_map(postings_list):
    m = {}
    for lst in postings_list:
        for p in lst:
            if not isinstance(p, dict): continue
            for prod in p.get("products", []):
                if not isinstance(prod, dict): continue
                sku = str(prod.get("sku", ""))
                oid = prod.get("offer_id", "")
                if sku and oid and sku not in m:
                    m[sku] = str(oid)
    return m

# ==================== ГРАФИКИ ПРОДАЖ ====================
async def get_monthly_delivered_sum(year):
    cache_key = f"monthly_delivered_{year}"
    cached = await get_from_cache(cache_key)
    if cached is not None: return cached
    start = datetime.date(year, 1, 1).isoformat()
    end = datetime.date(year, 12, 31).isoformat()
    postings = await fetch_postings(start, end)
    monthly = [0.0] * 12
    for p in postings:
        if not isinstance(p, dict): continue
        ca = p.get("created_at", "")
        if not ca: continue
        try:
            dtx = datetime.datetime.fromisoformat(ca.replace('Z', '+00:00')).astimezone(MOSCOW_TZ)
        except: continue
        if dtx.year != year: continue
        if p.get("status") not in ("delivered", "completed"): continue
        idx = dtx.month - 1
        for prod in p.get("products", []):
            if not isinstance(prod, dict): continue
            q = int(prod.get("quantity", 0))
            monthly[idx] += parse_price(prod) * q
    await save_to_cache(cache_key, monthly)
    return monthly

async def generate_sales_chart(years):
    data = {}
    for y in years: data[y] = await get_monthly_delivered_sum(y)
    fig, ax = plt.subplots(figsize=(10, 6))
    months = [datetime.date(2000, m, 1) for m in range(1, 13)]
    for y, vals in data.items():
        ax.plot(months, vals, marker='o', label=str(y), linewidth=2)
    ax.set_title("Динамика доставленных заказов (руб.)", fontsize=14)
    ax.set_xlabel("Месяц"); ax.set_ylabel("Сумма, ₽")
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    ax.grid(True, linestyle='--', alpha=0.7); ax.legend()
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'{int(x):,}'.replace(',', ' ')))
    plt.tight_layout()
    buf = io.BytesIO(); plt.savefig(buf, format='png', dpi=100); buf.seek(0); plt.close(fig)
    return buf

async def generate_product_chart(sku, metric, years):
    cache_key = f"product_chart_{sku}_{metric}_{'_'.join(str(y) for y in years)}"
    cached = await get_from_cache(cache_key)
    if cached is not None:
        data = {int(y): vals for y, vals in cached.items()}
    else:
        data = {}
        for y in years:
            start = datetime.date(y, 1, 1).isoformat()
            end = datetime.date(y, 12, 31).isoformat()
            postings = await fetch_postings(start, end)
            monthly = [0.0] * 12; counts = [0] * 12
            for p in postings:
                if not isinstance(p, dict): continue
                ca = p.get("created_at", "")
                if not ca: continue
                try:
                    dtx = datetime.datetime.fromisoformat(ca.replace('Z', '+00:00')).astimezone(MOSCOW_TZ)
                except: continue
                if dtx.year != y: continue
                idx = dtx.month - 1
                st = p.get("status", "")
                for prod in p.get("products", []):
                    if not isinstance(prod, dict): continue
                    if str(prod.get("sku", "0")) != sku: continue
                    q = int(prod.get("quantity", 0)); price = parse_price(prod)
                    if metric == "ordered_sum": monthly[idx] += price * q
                    elif metric == "ordered_units": monthly[idx] += q
                    elif metric == "delivered_sum" and st in ("delivered", "completed"): monthly[idx] += price * q
                    elif metric == "delivered_units" and st in ("delivered", "completed"): monthly[idx] += q
                    elif metric == "canceled_sum" and st in ("cancelled", "canceled"): monthly[idx] += price * q
                    elif metric == "canceled_units" and st in ("cancelled", "canceled"): monthly[idx] += q
                    elif metric == "avg_check":
                        monthly[idx] += price * q; counts[idx] += 1
            if metric == "avg_check":
                monthly = [(monthly[i] / counts[i]) if counts[i] > 0 else 0.0 for i in range(12)]
            data[y] = monthly
        await save_to_cache(cache_key, {str(y): v for y, v in data.items()})
    fig, ax = plt.subplots(figsize=(10, 6))
    months = [datetime.date(2000, m, 1) for m in range(1, 13)]
    ylabel = METRIC_LABELS.get(metric, "Значение")
    for y, vals in data.items():
        ax.plot(months, vals, marker='o', label=str(y), linewidth=2)
    ax.set_title(f"Динамика по товару (SKU: {sku})", fontsize=14)
    ax.set_xlabel("Месяц"); ax.set_ylabel(ylabel)
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    ax.grid(True, linestyle='--', alpha=0.7); ax.legend()
    if metric in ("ordered_sum", "delivered_sum", "canceled_sum", "avg_check"):
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'{int(x):,}'.replace(',', ' ')))
    plt.tight_layout()
    buf = io.BytesIO(); plt.savefig(buf, format='png', dpi=100); buf.seek(0); plt.close(fig)
    return buf

# ============================================================
# ОТЧЁТ ПО РЕКЛАМЕ
# ============================================================

def ad_to_float(val, default=0.0):
    if val is None: return default
    if isinstance(val, (int, float)): return float(val)
    if isinstance(val, dict): val = val.get("amount", val.get("value", 0))
    s = str(val).strip().replace(" ", "").replace(",", ".")
    try: return float(s)
    except (ValueError, TypeError): return default

def ad_to_int(val, default=0):
    try: return int(ad_to_float(val, 0))
    except Exception: return default

def ad_extract_price(product) -> float:
    if not isinstance(product, dict): return 0.0
    return ad_to_float(product.get("price", 0))

async def ad_fetch_campaigns(token) -> List[Dict]:
    cache_key = "perf_campaigns_list_v1"
    cached = await get_from_cache(cache_key)
    if cached is not None: return cached
    url = "https://api-performance.ozon.ru/api/client/campaign"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        data = await api_request_with_retry(url, headers, method='GET', kind='perf')
        if isinstance(data, list): campaigns = data
        elif isinstance(data, dict): campaigns = data.get("list", data.get("campaigns", []))
        else: campaigns = []
        await save_to_cache(cache_key, campaigns)
        return campaigns
    except Exception as e:
        write_log(f"❌ Кампании: {e}")
    return []

async def ad_fetch_campaign_objects(token, campaign_id: str) -> List[str]:
    cache_key = f"perf_objects_{campaign_id}"
    cached = await get_from_cache(cache_key)
    if cached is not None: return cached
    url = f"https://api-performance.ozon.ru/api/client/campaign/{campaign_id}/objects"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        data = await api_request_with_retry(url, headers, method='GET', kind='perf', silent_404=True)
        result = []
        if isinstance(data, dict):
            for item in data.get("list", []):
                if isinstance(item, dict) and item.get("id"):
                    result.append(str(item["id"]))
        await save_to_cache(cache_key, result)
        return result
    except Exception:
        await save_to_cache(cache_key, [])
        return []

async def ad_fetch_stats_chunk(token, campaign_ids: List[str], date_from: str, date_to: str) -> List[Dict]:
    if not campaign_ids: return []
    cache_key = f"perf_stats_{date_from}_{date_to}_{'_'.join(sorted(campaign_ids))[:80]}"
    cached = await get_from_cache(cache_key)
    if cached is not None: return cached
    headers = {"Authorization": f"Bearer {token}"}
    base = "https://api-performance.ozon.ru/api/client/statistics/campaign/product/json"
    parts = [f"dateFrom={date_from}", f"dateTo={date_to}"]
    for cid in campaign_ids:
        parts.append(f"campaignIds={cid}")
    url = base + "?" + "&".join(parts)
    try:
        data = await api_request_with_retry(url, headers, method='GET', kind='perf')
        rows = data.get("rows", []) if isinstance(data, dict) else []
        await save_to_cache(cache_key, rows)
        return rows
    except Exception as e:
        write_log(f"❌ Статистика рекламы ({date_from}–{date_to}): {e}")
        return []

async def ad_fetch_stats(token, campaign_ids: List[str], date_from: str, date_to: str,
                        status_msg=None) -> List[Dict]:
    """Собирает статистику за период, разбивая на части по 60 дней и суммируя по кампаниям."""
    start_dt = datetime.datetime.strptime(date_from, "%Y-%m-%d").date()
    end_dt = datetime.datetime.strptime(date_to, "%Y-%m-%d").date()
    today = get_moscow_today()
    if start_dt > today: return []
    if end_dt > today: end_dt = today
    total_days = (end_dt - start_dt).days + 1
    if total_days <= AD_CHUNK_DAYS:
        return await ad_fetch_stats_chunk(token, campaign_ids, date_from, end_dt.isoformat())
    # Разбиваем
    all_rows: Dict[str, Dict] = {}  # by campaign_id
    chunks = []
    cur = start_dt
    while cur <= end_dt:
        chunk_end = min(cur + datetime.timedelta(days=AD_CHUNK_DAYS - 1), end_dt)
        chunks.append((cur.isoformat(), chunk_end.isoformat()))
        cur = chunk_end + datetime.timedelta(days=1)
    for i, (c_from, c_to) in enumerate(chunks):
        if status_msg:
            try:
                await status_msg.edit_text(
                    f"⏳ Статистика рекламы: часть {i+1}/{len(chunks)} ({c_from}–{c_to})")
            except Exception: pass
        rows = await ad_fetch_stats_chunk(token, campaign_ids, c_from, c_to)
        for r in rows:
            cid = str(r.get("id", ""))
            if cid not in all_rows:
                all_rows[cid] = {"id": cid, "title": r.get("title", cid),
                                 "moneySpent": "0", "ordersMoney": "0", "views": "0",
                                 "clicks": "0", "toCart": "0", "orders": "0", "clickPrice": "0"}
            dst = all_rows[cid]
            dst["moneySpent"] = str(ad_to_float(dst["moneySpent"]) + ad_to_float(r.get("moneySpent")))
            dst["ordersMoney"] = str(ad_to_float(dst["ordersMoney"]) + ad_to_float(r.get("ordersMoney")))
            dst["views"] = str(ad_to_int(dst["views"]) + ad_to_int(r.get("views")))
            dst["clicks"] = str(ad_to_int(dst["clicks"]) + ad_to_int(r.get("clicks")))
            dst["toCart"] = str(ad_to_int(dst["toCart"]) + ad_to_int(r.get("toCart")))
            dst["orders"] = str(ad_to_int(dst["orders"]) + ad_to_int(r.get("orders")))
    # Пересчитываем clickPrice = moneySpent / clicks
    for cid, dst in all_rows.items():
        c = ad_to_int(dst["clicks"])
        dst["clickPrice"] = str(ad_to_float(dst["moneySpent"]) / c) if c > 0 else "0"
    return list(all_rows.values())

def ad_sum_sales_by_sku_set(postings: List[Dict], skus: set) -> float:
    if not skus: return 0.0
    total = 0.0
    for p in postings:
        for prod in p.get("products", []):
            if str(prod.get("sku", "")) in skus:
                qty = int(prod.get("quantity", 0))
                total += ad_extract_price(prod) * qty
    return total

def ad_aggregate_total(rows, skus_by_campaign, postings):
    total = {"avg_bid": 0.0, "avg_cpc": 0.0, "ad_sales": 0.0, "total_sales": 0.0,
             "expense": 0.0, "drr_ad": None, "drr_total": None,
             "impressions": 0, "clicks": 0, "carts": 0, "ctr": 0.0, "orders": 0}
    if not rows: return total
    total_bid = 0.0; bid_count = 0
    total_expense = 0.0; total_ad_sales = 0.0
    total_views = 0; total_clicks = 0; total_carts = 0; total_orders = 0
    all_skus = set()
    for r in rows:
        cid = str(r.get("id", ""))
        bid = ad_to_float(r.get("clickPrice"))
        if bid > 0: total_bid += bid; bid_count += 1
        total_expense += ad_to_float(r.get("moneySpent"))
        total_ad_sales += ad_to_float(r.get("ordersMoney"))
        total_views += ad_to_int(r.get("views"))
        total_clicks += ad_to_int(r.get("clicks"))
        total_carts += ad_to_int(r.get("toCart"))
        total_orders += ad_to_int(r.get("orders"))
        all_skus.update(skus_by_campaign.get(cid, []))
    total_sales_all = ad_sum_sales_by_sku_set(postings, all_skus)
    total["avg_bid"] = total_bid / bid_count if bid_count > 0 else 0.0
    total["avg_cpc"] = total_expense / total_clicks if total_clicks > 0 else 0.0
    total["ad_sales"] = total_ad_sales
    total["total_sales"] = total_sales_all
    total["expense"] = total_expense
    total["drr_ad"] = (total_expense / total_ad_sales * 100) if total_ad_sales > 0 else None
    total["drr_total"] = (total_expense / total_sales_all * 100) if total_sales_all > 0 else None
    total["impressions"] = total_views
    total["clicks"] = total_clicks
    total["carts"] = total_carts
    total["orders"] = total_orders
    total["ctr"] = (total_clicks / total_views * 100) if total_views > 0 else 0.0
    return total

def ad_aggregate_by_campaign(rows, skus_by_campaign, postings, sku_offer_map=None):
    result = []
    if sku_offer_map is None: sku_offer_map = {}
    for r in rows:
        cid = str(r.get("id", ""))
        expense = ad_to_float(r.get("moneySpent"))
        ad_sales = ad_to_float(r.get("ordersMoney"))
        views = ad_to_int(r.get("views"))
        clicks = ad_to_int(r.get("clicks"))
        carts = ad_to_int(r.get("toCart"))
        orders = ad_to_int(r.get("orders"))
        click_price = ad_to_float(r.get("clickPrice"))
        skus = skus_by_campaign.get(cid, [])
        total_sales = ad_sum_sales_by_sku_set(postings, set(skus))
        result.append({
            "campaign_id": cid, "campaign_name": r.get("title", cid), "skus": skus,
            "avg_bid": click_price,
            "avg_cpc": expense / clicks if clicks > 0 else 0.0,
            "ad_sales": ad_sales, "total_sales": total_sales, "expense": expense,
            "drr_ad": (expense / ad_sales * 100) if ad_sales > 0 else None,
            "drr_total": (expense / total_sales * 100) if total_sales > 0 else None,
            "impressions": views, "clicks": clicks, "carts": carts, "orders": orders,
            "ctr": (clicks / views * 100) if views > 0 else 0.0,
        })
    return result

def ad_indicator(cur, prev, better_is_higher=True):
    if cur is None or prev is None: return ""
    if prev == 0 and cur == 0: return ""
    if prev == 0: return "🟢" if cur > 0 else "🔴"
    delta = (cur - prev) / abs(prev)
    if delta == 0: return ""
    if better_is_higher: return "🟢" if delta > 0 else "🔴"
    return "🟢" if delta < 0 else "🔴"

def ad_fmt_f(val):
    if val is None: return "—"
    return f"{val:,.2f}".replace(",", " ")

def ad_fmt_i(val):
    if val is None: return "—"
    return f"{int(val):,}".replace(",", " ")

def ad_fmt_pct(val):
    if val is None: return "—"
    return f"{val:.2f}%"

def ad_line(label, cur, prev, fmt_func, better_is_higher=True, no_indicator=False, has_prev=True):
    cur_str = fmt_func(cur)
    if not has_prev:
        return f"   {label}: {cur_str} vs — (нет данных)"
    prev_str = fmt_func(prev)
    if no_indicator:
        return f"   {label}: {cur_str} vs {prev_str}"
    ind = ad_indicator(cur, prev, better_is_higher)
    prefix = f"{ind} " if ind else "   "
    return f"{prefix}{label}: {cur_str} vs {prev_str}"

def ad_format_sku_list(skus: List[str], sku_offer_map: dict, max_items: int = 5) -> str:
    if not skus: return "—"
    parts = []
    for sku in skus[:max_items]:
        oid = sku_offer_map.get(sku)
        if oid: parts.append(f"{sku}/{oid}")
        else: parts.append(sku)
    s = ", ".join(parts)
    if len(skus) > max_items: s += f" … (+{len(skus) - max_items})"
    return s

def ad_build_report(cur_total, prev_total, cur_camps, prev_camps,
                    period_label, has_prev, sku_offer_map=None,
                    header="📊 *Статистика за текущий месяц"):
    if sku_offer_map is None: sku_offer_map = {}
    lines = []
    if period_label:
        lines.append(f"{header} на {period_label}*")
    else:
        lines.append(f"{header}*")
    lines.append("Суммарные данные по всем рекламным кампаниям")
    lines.append("")

    def L(label, key, fmt_func, better_high=True, no_ind=False):
        return ad_line(label, cur_total.get(key), prev_total.get(key),
                       fmt_func, better_high, no_ind, has_prev)

    lines.append(L("Ваша ставка, руб.", "avg_bid", ad_fmt_f, False, no_ind=True))
    lines.append(L("Средняя стоимость клика, руб.", "avg_cpc", ad_fmt_f, False))
    lines.append(L("Продано товаров в рекламной компании, руб.", "ad_sales", ad_fmt_f, True))
    lines.append(L("Продано товаров ВСЕГО, руб.", "total_sales", ad_fmt_f, True))
    lines.append(L("Расход, руб.", "expense", ad_fmt_f, False))
    lines.append(L("ДРР в рекламной компании, %", "drr_ad", ad_fmt_pct, False))
    lines.append(L("ДРР ОБЩИЙ, %", "drr_total", ad_fmt_pct, False))
    lines.append(L("Показы, кол-во.", "impressions", ad_fmt_i, True))
    lines.append(L("Клики, кол-во.", "clicks", ad_fmt_i, True))
    lines.append(L("Добавления в корзину, кол-во.", "carts", ad_fmt_i, True))
    lines.append(L("CTR, %", "ctr", ad_fmt_pct, True))
    lines.append("")
    lines.append("*ПО КОМПАНИЯМ*")
    lines.append("")

    prev_map = {c["campaign_id"]: c for c in prev_camps}
    active = [c for c in cur_camps if c["impressions"] > 0 or c["expense"] > 0]
    active.sort(key=lambda c: c["expense"], reverse=True)

    if not active:
        lines.append("Нет активных кампаний в этом периоде.")
        return "\n".join(lines)

    for c in active:
        p = prev_map.get(c["campaign_id"], {})
        lines.append(f"*Название рекламной компании: {c['campaign_name']}*")
        sku_str = ad_format_sku_list(c["skus"], sku_offer_map)
        lines.append(f"СКЮ/АРТИКУЛ: {sku_str}")
        lines.append("")

        def Lc(label, key, fmt_func, better_high=True, no_ind=False):
            return ad_line(label, c.get(key), p.get(key), fmt_func,
                           better_high, no_ind, has_prev)

        lines.append(Lc("Ваша ставка, руб.", "avg_bid", ad_fmt_f, False, no_ind=True))
        lines.append(Lc("Средняя стоимость клика, руб.", "avg_cpc", ad_fmt_f, False))
        lines.append(Lc("Продано товаров в рекламной компании, руб.", "ad_sales", ad_fmt_f, True))
        lines.append(Lc("Продано товаров ВСЕГО, руб.", "total_sales", ad_fmt_f, True))
        lines.append(Lc("Расход, руб.", "expense", ad_fmt_f, False))
        lines.append(Lc("ДРР в рекламной компании, %", "drr_ad", ad_fmt_pct, False))
        lines.append(Lc("ДРР ОБЩИЙ, %", "drr_total", ad_fmt_pct, False))
        lines.append(Lc("Показы, кол-во.", "impressions", ad_fmt_i, True))
        lines.append(Lc("Клики, кол-во.", "clicks", ad_fmt_i, True))
        lines.append(Lc("Добавления в корзину, кол-во.", "carts", ad_fmt_i, True))
        lines.append(Lc("CTR, %", "ctr", ad_fmt_pct, True))
        lines.append("")

    text = "\n".join(lines)
    if len(text) > 4000: text = text[:3950] + "\n\n_…сообщение обрезано_"
    return text

async def _safe_edit(msg, text, parse_mode="Markdown"):
    try:
        await msg.edit_text(text, parse_mode=parse_mode)
    except Exception as e:
        write_log(f"⚠️ edit_text: {e}")

async def _get_all_campaign_data(token, status_msg=None):
    """Возвращает (campaigns, skus_by_campaign). С кэшем."""
    campaigns = await ad_fetch_campaigns(token)
    if not campaigns: return [], {}
    if status_msg:
        await _safe_edit(status_msg, f"⏳ Собираю SKU кампаний ({len(campaigns)})...")
    skus_by_campaign = {}
    for i, c in enumerate(campaigns):
        cid = str(c.get("id", ""))
        if not cid: continue
        if status_msg and (i % 5 == 0 or i == len(campaigns) - 1):
            await _safe_edit(status_msg, f"⏳ SKU кампаний: {i+1}/{len(campaigns)}...")
        objs = await ad_fetch_campaign_objects(token, cid)
        if objs: skus_by_campaign[cid] = objs
    return campaigns, skus_by_campaign

async def build_ad_report_month(status_msg=None):
    cache_key = f"ad_report_month_{get_moscow_today().isoformat()}_v2"
    cached = await get_from_cache(cache_key)
    if cached is not None: return cached
    today = get_moscow_today()
    yesterday = today - datetime.timedelta(days=1)
    cur_from = today.replace(day=1); cur_to = yesterday
    days_count = (cur_to - cur_from).days + 1
    prev_month_end = cur_from - datetime.timedelta(days=1)
    prev_from = prev_month_end.replace(day=1)
    prev_to = prev_from + datetime.timedelta(days=days_count - 1)
    if prev_to > prev_month_end: prev_to = prev_month_end
    if status_msg: await _safe_edit(status_msg, "⏳ Загружаю токен...")
    token = await get_performance_token()
    if not token: return "❌ Не удалось получить токен Performance API"
    campaigns, skus_by_campaign = await _get_all_campaign_data(token, status_msg)
    campaign_ids = [str(c.get("id")) for c in campaigns if c.get("id")]
    if status_msg: await _safe_edit(status_msg, "⏳ Загружаю статистику и заказы...")
    cur_rows, prev_rows, cur_postings, prev_postings = await asyncio.gather(
        ad_fetch_stats(token, campaign_ids, cur_from.isoformat(), cur_to.isoformat(), status_msg),
        ad_fetch_stats(token, campaign_ids, prev_from.isoformat(), prev_to.isoformat(), status_msg),
        fetch_postings(cur_from.isoformat(), cur_to.isoformat()),
        fetch_postings(prev_from.isoformat(), prev_to.isoformat()),
    )
    if status_msg: await _safe_edit(status_msg, "⏳ Формирую отчёт...")
    sku_offer_map = build_sku_offer_map([cur_postings, prev_postings])
    has_prev = bool(prev_rows) and any(ad_to_int(r.get("views")) > 0 for r in prev_rows)
    cur_total = ad_aggregate_total(cur_rows, skus_by_campaign, cur_postings)
    prev_total = ad_aggregate_total(prev_rows, skus_by_campaign, prev_postings)
    cur_camps = ad_aggregate_by_campaign(cur_rows, skus_by_campaign, cur_postings, sku_offer_map)
    prev_camps = ad_aggregate_by_campaign(prev_rows, skus_by_campaign, prev_postings, sku_offer_map)
    period_label = cur_to.strftime("%d.%m.%Y")
    report = ad_build_report(cur_total, prev_total, cur_camps, prev_camps, period_label, has_prev,
                            sku_offer_map, header="📊 *Статистика за текущий месяц")
    await save_to_cache(cache_key, report)
    return report

async def build_ad_report_period(date_from: str, date_to: str, period_name: str, status_msg=None):
    cache_key = f"ad_report_{date_from}_{date_to}_v2"
    cached = await get_from_cache(cache_key)
    if cached is not None: return cached
    if status_msg: await _safe_edit(status_msg, "⏳ Загружаю токен...")
    token = await get_performance_token()
    if not token: return "❌ Не удалось получить токен Performance API"
    campaigns, skus_by_campaign = await _get_all_campaign_data(token, status_msg)
    campaign_ids = [str(c.get("id")) for c in campaigns if c.get("id")]
    # Предыдущий период
    prev_from, prev_to = prev_period(date_from, date_to)
    if status_msg:
        await _safe_edit(status_msg, f"⏳ Текущий: {date_from}–{date_to}\nПредыдущий: {prev_from}–{prev_to}")
    cur_rows, prev_rows, cur_postings, prev_postings = await asyncio.gather(
        ad_fetch_stats(token, campaign_ids, date_from, date_to, status_msg),
        ad_fetch_stats(token, campaign_ids, prev_from, prev_to, status_msg),
        fetch_postings(date_from, date_to),
        fetch_postings(prev_from, prev_to),
    )
    if status_msg: await _safe_edit(status_msg, "⏳ Формирую отчёт...")
    sku_offer_map = build_sku_offer_map([cur_postings, prev_postings])
    has_prev = bool(prev_rows) and any(ad_to_int(r.get("views")) > 0 for r in prev_rows)
    cur_total = ad_aggregate_total(cur_rows, skus_by_campaign, cur_postings)
    prev_total = ad_aggregate_total(prev_rows, skus_by_campaign, prev_postings)
    cur_camps = ad_aggregate_by_campaign(cur_rows, skus_by_campaign, cur_postings, sku_offer_map)
    prev_camps = ad_aggregate_by_campaign(prev_rows, skus_by_campaign, prev_postings, sku_offer_map)
    report = ad_build_report(cur_total, prev_total, cur_camps, prev_camps, period_name, has_prev,
                            sku_offer_map, header="📊 *Реклама за период")
    await save_to_cache(cache_key, report)
    return report

# ========== Динамика по рекламе ==========

async def ad_dynamics_get_monthly(token, campaign_ids, year, skus_by_campaign, postings_by_month,
                                   metric_key, status_msg=None):
    """Возвращает [12 значений] метрики по месяцам за год."""
    data = [0.0] * 12
    # Разбиваем год на части по 60 дней
    year_start = datetime.date(year, 1, 1)
    year_end = datetime.date(year, 12, 31)
    today = get_moscow_today()
    if year_start > today: return data
    if year_end > today: year_end = today
    chunks = []
    cur = year_start
    while cur <= year_end:
        chunk_end = min(cur + datetime.timedelta(days=AD_CHUNK_DAYS - 1), year_end)
        chunks.append((cur, chunk_end))
        cur = chunk_end + datetime.timedelta(days=1)
    # Собираем статистику по каждому chunk
    monthly_total = {m: {"expense": 0.0, "ad_sales": 0.0, "impressions": 0,
                          "clicks": 0, "carts": 0, "bid_sum": 0.0, "bid_count": 0,
                          "total_sales": 0.0} for m in range(12)}
    for i, (c_from, c_to) in enumerate(chunks):
        if status_msg:
            try:
                await status_msg.edit_text(
                    f"⏳ Динамика рекламы: {c_from.strftime('%d.%m')}–{c_to.strftime('%d.%m')} "
                    f"({i+1}/{len(chunks)})")
            except: pass
        rows = await ad_fetch_stats_chunk(token, campaign_ids, c_from.isoformat(), c_to.isoformat())
        # Распределяем по месяцам внутри chunk — но API даёт агрегат по chunk, не по месяцам
        # Будем использовать только целые месяцы, а частичные примыкают к ближайшему
        # Проще: относим весь chunk к месяцу его начала
        # Не идеально, но достаточное приближение
        # Лучше: разрезать chunk по границам месяцев
        # Разобьём по месяцам:
        cur_m = c_from
        while cur_m <= c_to:
            month_first = cur_m.replace(day=1)
            if month_first.month == 12:
                month_last = datetime.date(month_first.year, 12, 31)
            else:
                month_last = datetime.date(month_first.year, month_first.month + 1, 1) - datetime.timedelta(days=1)
            sub_from = max(cur_m, c_from)
            sub_to = min(month_last, c_to)
            if sub_from <= sub_to:
                # запрашиваем только этот отрезок
                sub_rows = await ad_fetch_stats_chunk(token, campaign_ids,
                                                       sub_from.isoformat(), sub_to.isoformat())
                mi = month_first.month - 1
                # вычислим total_sales по postings за sub_from..sub_to
                sub_postings = postings_by_month.get(mi, [])
                sub_skus = set()
                for r in sub_rows:
                    cid = str(r.get("id", ""))
                    sub_skus.update(skus_by_campaign.get(cid, []))
                ts = ad_sum_sales_by_sku_set(sub_postings, sub_skus)
                for r in sub_rows:
                    monthly_total[mi]["expense"] += ad_to_float(r.get("moneySpent"))
                    monthly_total[mi]["ad_sales"] += ad_to_float(r.get("ordersMoney"))
                    monthly_total[mi]["impressions"] += ad_to_int(r.get("views"))
                    monthly_total[mi]["clicks"] += ad_to_int(r.get("clicks"))
                    monthly_total[mi]["carts"] += ad_to_int(r.get("toCart"))
                    b = ad_to_float(r.get("clickPrice"))
                    cl = ad_to_int(r.get("clicks"))
                    if b > 0 and cl > 0:
                        monthly_total[mi]["bid_sum"] += b * cl
                        monthly_total[mi]["bid_count"] += cl
                monthly_total[mi]["total_sales"] += ts
            cur_m = month_last + datetime.timedelta(days=1)
    # Извлекаем метрику
    for m in range(12):
        d = monthly_total[m]
        if metric_key == "avg_bid":
            data[m] = d["bid_sum"] / d["bid_count"] if d["bid_count"] > 0 else 0.0
        elif metric_key == "avg_cpc":
            data[m] = d["expense"] / d["clicks"] if d["clicks"] > 0 else 0.0
        elif metric_key == "ad_sales":
            data[m] = d["ad_sales"]
        elif metric_key == "total_sales":
            data[m] = d["total_sales"]
        elif metric_key == "expense":
            data[m] = d["expense"]
        elif metric_key == "drr_ad":
            data[m] = (d["expense"] / d["ad_sales"] * 100) if d["ad_sales"] > 0 else 0.0
        elif metric_key == "drr_total":
            data[m] = (d["expense"] / d["total_sales"] * 100) if d["total_sales"] > 0 else 0.0
        elif metric_key == "impressions":
            data[m] = d["impressions"]
        elif metric_key == "clicks":
            data[m] = d["clicks"]
        elif metric_key == "carts":
            data[m] = d["carts"]
        elif metric_key == "ctr":
            data[m] = (d["clicks"] / d["impressions"] * 100) if d["impressions"] > 0 else 0.0
    return data

async def generate_ad_dynamics_chart(token, campaign_ids, skus_by_campaign, years,
                                      metric_key, metric_label, postings_by_month_cache):
    # Загружаем postings за все годы
    postings_by_month_all = {}
    for y in years:
        p = await fetch_postings(f"{y}-01-01", f"{y}-12-31")
        postings_by_month_all[y] = p
    data = {}
    for y in years:
        # Собираем postings по месяцам
        monthly_postings = {m: [] for m in range(12)}
        for p in postings_by_month_all[y]:
            ca = p.get("created_at", "")
            if not ca: continue
            try:
                dtx = datetime.datetime.fromisoformat(ca.replace('Z', '+00:00')).astimezone(MOSCOW_TZ)
            except: continue
            if dtx.year != y: continue
            monthly_postings[dtx.month - 1].append(p)
        data[y] = await ad_dynamics_get_monthly(token, campaign_ids, y, skus_by_campaign,
                                                 monthly_postings, metric_key)
    # График
    fig, ax = plt.subplots(figsize=(10, 6))
    months = [datetime.date(2000, m, 1) for m in range(1, 13)]
    for y, vals in data.items():
        ax.plot(months, vals, marker='o', label=str(y), linewidth=2)
    ax.set_title(f"Динамика рекламы: {metric_label}", fontsize=14)
    ax.set_xlabel("Месяц"); ax.set_ylabel(metric_label)
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    ax.grid(True, linestyle='--', alpha=0.7); ax.legend()
    if metric_key in ("ad_sales", "total_sales", "expense"):
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'{int(x):,}'.replace(',', ' ')))
    plt.tight_layout()
    buf = io.BytesIO(); plt.savefig(buf, format='png', dpi=100); buf.seek(0); plt.close(fig)
    return buf

# ==================== ФОРМАТИРОВАНИЕ ПРОДАЖ ====================
def format_expense_block(exp, title, limit=25):
    if not exp: return f"🔹 *{title}*\nНет данных о расходах.\n"
    total = sum(exp.values())
    lines = [f"🔹 *{title}*", f"  *Итого расходов:* {total:,.2f} ₽"]
    for k, v in sorted(exp.items(), key=lambda x: x[1], reverse=True)[:limit]:
        lines.append(f"    {k}: {v:,.2f} ₽")
    return "\n".join(lines)

def format_top_products(products, title, limit=15):
    if not products: return f"📦 *{title}*\n\n❌ Нет данных за указанный период."
    sorted_items = sorted(products.items(), key=lambda x: x[1]["ordered_sum"], reverse=True)[:limit]
    lines = [f"📦 *{title}*", ""]
    for i, (sku, s) in enumerate(sorted_items, 1):
        name = s["name"][:40]; oid = s["offer_id"]
        avg = (s["ordered_sum"] / s["order_count"]) if s["order_count"] > 0 else 0
        line = f"{i}. SKU: {sku} | {name}"
        if oid: line += f" | Арт: {oid}"
        lines.append(line)
        lines.append(f"   🛒 Заказано: {fmt_num(s['ordered_sum'])} ₽ / {s['ordered_units']} шт.")
        lines.append(f"   📦 Доставлено: {fmt_num(s['delivered_sum'])} ₽ / {s['delivered_units']} шт.")
        lines.append(f"   ❌ Отменено: {fmt_num(s['canceled_sum'])} ₽ / {s['canceled_units']} шт.")
        lines.append(f"   💰 Средний чек: {fmt_num(avg)} ₽")
        lines.append("")
    return "\n".join(lines)

def format_products_summary(products):
    if not products: return "Нет данных"
    rev = sum(p["ordered_sum"] for p in products.values())
    units = sum(p["ordered_units"] for p in products.values())
    orders = sum(p["order_count"] for p in products.values())
    avg = (rev / orders) if orders > 0 else 0
    return (f"Сводка\n  Уникальных товаров: {len(products)}\n  Общая выручка: {rev:,.2f} ₽\n"
            f"  Всего единиц: {units}\n  Всего заказов: {orders}\n  Средний чек: {avg:,.2f} ₽")

def format_period_comparison(cur, prev, name):
    lines = [f"📊 *Продажи за {name}*", ""]
    cur_os = cur.get("ordered_sum", 0); prev_os = prev.get("ordered_sum", 0)
    cur_ou = cur.get("ordered_units", 0); prev_ou = prev.get("ordered_units", 0)
    lines.append("🛒 *Заказано*")
    lines.append(f"  На сумму: {fmt_num(cur_os)} ₽ {indicator(cur_os, prev_os, True)}")
    lines.append(f"  Штук: {fmt_int(cur_ou)} {indicator(cur_ou, prev_ou, True)}")
    lines.append("vs предыдущий период:")
    lines.append(f"  На сумму: {fmt_num(prev_os)} ₽")
    lines.append(f"  Штук: {fmt_int(prev_ou)}")
    lines.append("")
    cur_ds = cur.get("delivered_sum", 0); prev_ds = prev.get("delivered_sum", 0)
    cur_du = cur.get("delivered_units", 0); prev_du = prev.get("delivered_units", 0)
    lines.append("📦 *Доставлено*")
    lines.append(f"  На сумму: {fmt_num(cur_ds)} ₽ {indicator(cur_ds, prev_ds, True)}")
    lines.append(f"  Штук: {fmt_int(cur_du)} {indicator(cur_du, prev_du, True)}")
    lines.append("vs предыдущий период:")
    lines.append(f"  На сумму: {fmt_num(prev_ds)} ₽")
    lines.append(f"  Штук: {fmt_int(prev_du)}")
    lines.append("")
    cur_cs = cur.get("canceled_sum", 0); prev_cs = prev.get("canceled_sum", 0)
    cur_cu = cur.get("canceled_units", 0); prev_cu = prev.get("canceled_units", 0)
    cur_cr = (cur_cu / cur_du * 100) if cur_du > 0 else None
    prev_cr = (prev_cu / prev_du * 100) if prev_du > 0 else None
    cur_cr_txt = f"{cur_cr:.2f}%" if cur_cr is not None else "∞"
    prev_cr_txt = f"{prev_cr:.2f}%" if prev_cr is not None else "∞"
    lines.append("❌ *Отменено*")
    lines.append(f"  На сумму: {fmt_num(cur_cs)} ₽ {indicator(cur_cs, prev_cs, False)}")
    lines.append(f"  Штук: {fmt_int(cur_cu)} {indicator(cur_cu, prev_cu, False)}")
    lines.append(f"  Доля отмен: {cur_cr_txt} {indicator(cur_cr, prev_cr, False)}")
    lines.append("vs предыдущий период:")
    lines.append(f"  На сумму: {fmt_num(prev_cs)} ₽")
    lines.append(f"  Штук: {fmt_int(prev_cu)}")
    lines.append(f"  Доля отмен: {prev_cr_txt}")
    lines.append("")
    cur_ad = cur.get("ad_expense", 0); prev_ad = prev.get("ad_expense", 0)
    cur_drr = cur.get("drr"); prev_drr = prev.get("drr")
    cur_edrr = cur.get("effective_drr"); prev_edrr = prev.get("effective_drr")
    cur_drr_txt = f"{cur_drr:.2f}%" if cur_drr is not None else "∞"
    cur_edrr_txt = f"{cur_edrr:.2f}%" if cur_edrr is not None else "∞"
    prev_drr_txt = f"{prev_drr:.2f}%" if prev_drr is not None else "∞"
    prev_edrr_txt = f"{prev_edrr:.2f}%" if prev_edrr is not None else "∞"
    lines.append("📢 *Реклама*")
    lines.append(f"  Расходы: {fmt_num(cur_ad)} ₽ {indicator(cur_ad, prev_ad, False)}")
    lines.append(f"  ДРР (общий): {cur_drr_txt} {indicator(cur_drr, prev_drr, False)}")
    lines.append(f"  ДРР (по доставленным): {cur_edrr_txt} {indicator(cur_edrr, prev_edrr, False)}")
    lines.append("vs предыдущий период:")
    lines.append(f"  Расходы: {fmt_num(prev_ad)} ₽")
    lines.append(f"  ДРР (общий): {prev_drr_txt}")
    lines.append(f"  ДРР (по доставленным): {prev_edrr_txt}")
    lines.append("")
    lines.append(format_expense_block(cur.get("expenses", {}), "Расходы за период"))
    return "\n".join(lines)

# ==================== МЕТРИКИ ПЕРИОДА ====================
async def get_period_metrics(df, dt, status_msg=None):
    postings, ad, fin = await asyncio.gather(
        fetch_postings(df, dt), fetch_advertising_expense(df, dt),
        fetch_finance_transactions(df, dt, status_msg=status_msg))
    agg = aggregate_postings_range(postings, df, dt)
    exp = aggregate_finance_expenses(fin)
    osum = agg["ordered_sum"]; dsum = agg["delivered_sum"]
    return {**agg, "ad_expense": ad,
            "drr": (ad / osum * 100) if osum > 0 else None,
            "effective_drr": (ad / dsum * 100) if dsum > 0 else None,
            "expenses": exp}

# ==================== ОТЧЁТЫ ПРОДАЖ ====================
async def build_today_report(include_yesterday=False):
    now = get_current_time_msk()
    td = now.date(); today = td.isoformat()
    yest = (td - datetime.timedelta(days=1)).isoformat()
    cm = td.replace(day=1); cm_s = cm.isoformat()
    pm = (cm - datetime.timedelta(days=1)).replace(day=1); pm_s = pm.isoformat()
    dp = (td - cm).days + 1
    pm_e = pm + datetime.timedelta(days=dp - 1); pm_e_s = pm_e.isoformat()

    postings_cur, postings_prev, ad_t, ad_y, ad_m, ad_pm, fin_t, fin_m = await asyncio.gather(
        fetch_postings(cm_s, today), fetch_postings(pm_s, pm_e_s),
        fetch_advertising_expense(today, today), fetch_advertising_expense(yest, yest),
        fetch_advertising_expense(cm_s, today), fetch_advertising_expense(pm_s, pm_e_s),
        fetch_finance_transactions(today, today), fetch_finance_transactions(cm_s, today))

    t_m = aggregate_postings_range(postings_cur, today, today)
    y_m = aggregate_postings_range(postings_cur, yest, yest)
    m_m = aggregate_postings_range(postings_cur, cm_s, today)
    p_m = aggregate_postings_range(postings_prev, pm_s, pm_e_s)

    exp_t = aggregate_finance_expenses(fin_t)
    exp_m = aggregate_finance_expenses(fin_m)
    if ad_t > 0: exp_t["Оплата за клик"] = exp_t.get("Оплата за клик", 0) + ad_t
    if ad_m > 0: exp_m["Оплата за клик"] = exp_m.get("Оплата за клик", 0) + ad_m

    def blk_today():
        os_ = t_m["ordered_sum"]; ou_ = t_m["ordered_units"]
        cs_ = t_m["canceled_sum"]; cu_ = t_m["canceled_units"]
        ys_ = y_m["ordered_sum"]; yu_ = y_m["ordered_units"]
        ycs_ = y_m["canceled_sum"]; ycu_ = y_m["canceled_units"]
        d_os = fmt_pct(calc_delta(os_, ys_)); d_ou = fmt_pct(calc_delta(ou_, yu_))
        d_cs = fmt_pct(calc_delta(cs_, ycs_)); d_cu = fmt_pct(calc_delta(cu_, ycu_))
        du_ = t_m["delivered_units"]
        cr = (cu_ / du_ * 100) if du_ > 0 else None
        cr_txt = f"{cr:.2f}%" if cr is not None else "∞"
        return (
            f"🔹 *Сегодня (на {now.strftime('%H:%M')} МСК)*\n"
            f"  🛒 Заказано: \n  {fmt_num(os_)} ₽ / {fmt_int(ou_)} шт.\n"
            f"    vs Вчера: \n  {d_os} ₽ / {d_ou} шт.\n\n"
            f"  ❌ Отменено: \n  {fmt_num(cs_)} ₽ / {fmt_int(cu_)} шт.\n"
            f"    vs Вчера: \n  {d_cs} ₽ / {d_cu} шт.\n"
            f"  Доля отмен: {cr_txt}\n"
        )

    def blk_yesterday():
        os_ = y_m["ordered_sum"]; ou_ = y_m["ordered_units"]
        ds_ = y_m["delivered_sum"]; du_ = y_m["delivered_units"]
        cs_ = y_m["canceled_sum"]; cu_ = y_m["canceled_units"]
        cr = (cu_ / du_ * 100) if du_ > 0 else None
        cr_txt = f"{cr:.2f}%" if cr is not None else "∞"
        drr = (ad_y / os_ * 100) if os_ > 0 else None
        edrr = (ad_y / ds_ * 100) if ds_ > 0 else None
        drr_txt = f"{drr:.2f}%" if drr is not None else "∞"
        edrr_txt = f"{edrr:.2f}%" if edrr is not None else "∞"
        yest_dt = datetime.datetime.strptime(yest, "%Y-%m-%d").date()
        yest_fmt = yest_dt.strftime("%d.%m.%Y")
        return (
            f"🔹 *Вчера ({yest_fmt}, полный день)*\n"
            f"  🛒 Заказано: \n  {fmt_num(os_)} ₽ / {fmt_int(ou_)} шт.\n\n"
            f"  📦 Доставлено: \n  {fmt_num(ds_)} ₽ / {fmt_int(du_)} шт.\n\n"
            f"  ❌ Отменено: \n  {fmt_num(cs_)} ₽ / {fmt_int(cu_)} шт.\n"
            f"  Доля отмен: {cr_txt}\n\n"
            f"  📢 Реклама: {fmt_num(ad_y)} ₽\n"
            f"  ДРР (общий): {drr_txt} | ДРР (по доставленным): {edrr_txt}"
        )

    def blk_month():
        os_ = m_m["ordered_sum"]; ou_ = m_m["ordered_units"]
        ds_ = m_m["delivered_sum"]; du_ = m_m["delivered_units"]
        cs_ = m_m["canceled_sum"]; cu_ = m_m["canceled_units"]
        p_os = p_m["ordered_sum"]; p_ou = p_m["ordered_units"]
        p_ds = p_m["delivered_sum"]; p_du = p_m["delivered_units"]
        p_cs = p_m["canceled_sum"]; p_cu = p_m["canceled_units"]
        d_os = fmt_pct(calc_delta(os_, p_os)); d_ou = fmt_pct(calc_delta(ou_, p_ou))
        d_ds = fmt_pct(calc_delta(ds_, p_ds)); d_du = fmt_pct(calc_delta(du_, p_du))
        d_cs = fmt_pct(calc_delta(cs_, p_cs)); d_cu = fmt_pct(calc_delta(cu_, p_cu))
        cr = (cu_ / du_ * 100) if du_ > 0 else None
        p_cr = (p_cu / p_du * 100) if p_du > 0 else None
        cr_txt = f"{cr:.2f}%" if cr is not None else "∞"
        p_cr_txt = f"{p_cr:.2f}%" if p_cr is not None else "∞"
        d_cr = fmt_pct(calc_delta(cr if cr is not None else 0, p_cr if p_cr is not None else 0))
        drr = (ad_m / os_ * 100) if os_ > 0 else None
        edrr = (ad_m / ds_ * 100) if ds_ > 0 else None
        p_drr = (ad_pm / p_os * 100) if p_os > 0 else None
        p_edrr = (ad_pm / p_ds * 100) if p_ds > 0 else None
        drr_txt = f"{drr:.2f}%" if drr is not None else "∞"
        edrr_txt = f"{edrr:.2f}%" if edrr is not None else "∞"
        p_drr_txt = f"{p_drr:.2f}%" if p_drr is not None else "∞"
        p_edrr_txt = f"{p_edrr:.2f}%" if p_edrr is not None else "∞"
        return (
            f"🔹 *Текущий месяц*\n"
            f"  🛒 Заказано: \n  {fmt_num(os_)} ₽ / {fmt_int(ou_)} шт.\n"
            f"    vs предыдущий месяц: \n  {d_os} ₽ / {d_ou} шт.\n\n"
            f"  📦 Доставлено: \n  {fmt_num(ds_)} ₽ / {fmt_int(du_)} шт.\n"
            f"    vs предыдущий месяц: \n  {d_ds} ₽ / {d_du} шт.\n\n"
            f"  ❌ Отменено: \n  {fmt_num(cs_)} ₽ / {fmt_int(cu_)} шт.\n"
            f"    vs предыдущий месяц: \n  {d_cs} ₽ / {d_cu} шт.\n"
            f"  Доля отмен: {cr_txt} | vs предыдущий месяц: {p_cr_txt} ({d_cr})\n\n"
            f"  📢 Реклама: \n  {fmt_num(ad_m)} ₽ | vs предыдущий месяц: {fmt_num(ad_pm)} ₽\n"
            f"  ДРР (общий): {drr_txt} | vs предыдущий месяц: {p_drr_txt}\n"
            f"  ДРР (по доставленным): {edrr_txt} | vs предыдущий месяц: {p_edrr_txt}"
        )

    parts = []
    if include_yesterday:
        parts.append(blk_yesterday()); parts.append(blk_month())
        header = "📊 *Отчёт за Вчера*"
    else:
        parts.append(blk_today()); parts.append(blk_month())
        header = "📊 *Продажи за сегодня*"
    parts.append(format_expense_block(exp_t, "Расходы сегодня"))
    parts.append(format_expense_block(exp_m, "Расходы за текущий месяц"))
    return header + "\n\n\n" + "\n\n".join(parts)

async def build_period_report(df, dt, name, status_msg=None):
    pf, pt = prev_period(df, dt)
    missing_days = set()
    for d_from, d_to in [(df, dt), (pf, pt)]:
        cur_d = datetime.datetime.strptime(d_from, "%Y-%m-%d").date()
        end_d = datetime.datetime.strptime(d_to, "%Y-%m-%d").date()
        today = get_moscow_today()
        if cur_d > today: continue
        if end_d > today: end_d = today
        while cur_d <= end_d:
            if disk_cache_get(f"fin_day_{cur_d.isoformat()}") is None:
                missing_days.add(cur_d.isoformat())
            cur_d += datetime.timedelta(days=1)
    if missing_days and status_msg:
        est_low = len(missing_days) * 2 // 60
        est_high = len(missing_days) * 3 // 60
        await _safe_edit(status_msg,
            f"⏳ Загрузка финансов: {len(missing_days)} дней\n"
            f"_Ориентировочно {est_low}–{est_high} мин._")
    cur, prev = await asyncio.gather(
        get_period_metrics(df, dt, status_msg=status_msg),
        get_period_metrics(pf, pt, status_msg=status_msg))
    return format_period_comparison(cur, prev, name)

# ==================== КАЛЕНДАРЬ ====================
MONTH_NAMES = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
               "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

def create_calendar(year, month, prefix):
    kb = [[InlineKeyboardButton(f"{MONTH_NAMES[month-1]} {year}", callback_data="ignore")],
          [InlineKeyboardButton(d, callback_data="ignore") for d in ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]]]
    first, ndays = cal_mod.monthrange(year, month)
    row = [InlineKeyboardButton(" ", callback_data="ignore") for _ in range(first)]
    for d in range(1, ndays+1):
        row.append(InlineKeyboardButton(str(d), callback_data=f"{prefix}{year}-{month:02d}-{d:02d}"))
        if len(row) == 7: kb.append(row); row = []
    if row:
        while len(row) < 7: row.append(InlineKeyboardButton(" ", callback_data="ignore"))
        kb.append(row)
    kb.append([InlineKeyboardButton("◀️", callback_data=f"{prefix}prev_{year}_{month}"),
               InlineKeyboardButton(" ", callback_data="ignore"),
               InlineKeyboardButton("▶️", callback_data=f"{prefix}next_{year}_{month}")])
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data=f"{prefix}cancel")])
    return InlineKeyboardMarkup(kb)

# ==================== КЛАВИАТУРЫ ====================
def main_kb(chat_id):
    buttons = [
        [KeyboardButton("📊 Отчёт по продажам")],
        [KeyboardButton("📦 Отчёт по товарам")],
        [KeyboardButton("📈 Отчёт по рекламе")],
        [KeyboardButton("🔔 Автоматические рассылки")],
    ]
    if is_admin(chat_id):
        buttons.append([KeyboardButton("⚙️ Администрирование")])
        buttons.append([KeyboardButton("📚 Справочник расходов")])
    buttons.append([KeyboardButton("📖 Справка")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def access_check_kb():
    return ReplyKeyboardMarkup([[KeyboardButton("🔄 Проверить доступ")]], resize_keyboard=True)

def sales_reports_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📅 Продажи за сегодня")],
        [KeyboardButton("📆 Выбрать дату"), KeyboardButton("📊 Выбрать период")],
        [KeyboardButton("📈 Динамика продаж")],
        [KeyboardButton("🔙 Назад")]], resize_keyboard=True)

def products_reports_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📅 Топ товаров за сегодня")],
        [KeyboardButton("🏆 Товары за период")],
        [KeyboardButton("📈 Динамика по товару")],
        [KeyboardButton("🔙 Назад")]], resize_keyboard=True)

def ad_reports_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📅 Реклама за текущий месяц")],
        [KeyboardButton("📅 Реклама за период")],
        [KeyboardButton("📈 Динамика по рекламе")],
        [KeyboardButton("🔙 Назад")]], resize_keyboard=True)

def admin_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("➕ Добавить менеджера"), KeyboardButton("➖ Удалить менеджера")],
        [KeyboardButton("📋 Список менеджеров")],
        [KeyboardButton("🔙 Назад")]], resize_keyboard=True)

def auto_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🕒 Выбор времени рассылок")],
        [KeyboardButton("📅 Добавление отчета за Вчера")],
        [KeyboardButton("🔕 Режим тишины")],
        [KeyboardButton("📤 Отправить отчет за Вчера сейчас")],
        [KeyboardButton("🔙 Назад")]], resize_keyboard=True)

def products_period_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗓️ По месяцам", callback_data="tpm")],
        [InlineKeyboardButton("📅 По кварталам", callback_data="tpq")],
        [InlineKeyboardButton("📆 По годам", callback_data="tpy")],
        [InlineKeyboardButton("✏️ Произвольный период", callback_data="tpc")],
        [InlineKeyboardButton("🔙 Назад", callback_data="tpcancel")]])

def expense_types_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить", callback_data="et_refresh")],
        [InlineKeyboardButton("🔙 Назад", callback_data="et_back")]])

def hours_kb(selected, prefix, available=None):
    kb = []
    hrs = sorted(available) if available is not None else list(range(1, 25))
    for h in hrs:
        hour = h % 24
        checked = "✅" if hour in selected else "⬜"
        kb.append([InlineKeyboardButton(f"{h:02d}:00 {checked}", callback_data=f"{prefix}{hour}")])
    kb.append([InlineKeyboardButton("💾 Сохранить", callback_data=f"{prefix}save"),
               InlineKeyboardButton("🔙 Назад", callback_data=f"{prefix}back")])
    return InlineKeyboardMarkup(kb)

def hours_kb_silence(prefix):
    kb = []
    for h in range(1, 25):
        kb.append([InlineKeyboardButton(f"{h:02d}:00", callback_data=f"{prefix}{h%24}")])
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data=f"{prefix}back")])
    return InlineKeyboardMarkup(kb)

# ==================== ГЛАВНОЕ МЕНЮ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    if not has_access(chat_id):
        await update.message.reply_text(
            "❌ Нет доступа! Обратитесь к администратору.\n\n"
            "После получения доступа нажмите «🔄 Проверить доступ».",
            reply_markup=access_check_kb())
        return
    name = user.first_name or ""
    hour = datetime.datetime.now(MOSCOW_TZ).hour
    if 5 <= hour < 12: greet = "Доброе утро"
    elif 12 <= hour < 18: greet = "Добрый день"
    elif 18 <= hour < 24: greet = "Добрый вечер"
    else: greet = "Доброй ночи"
    greeting = f"{greet}, {name}!" if name else f"{greet}!"
    await update.message.reply_text(f"{greeting}\n\n🤖 Версия бота: {VERSION}",
                                    reply_markup=main_kb(chat_id))
    if not user_has_schedule(chat_id):
        await update.message.reply_text(
            "⚠️ *Необходимо настроить автоматические рассылки!*\n\n"
            "Зайдите в раздел «🔔 Автоматические рассылки» и выберите часы рассылок.",
            parse_mode="Markdown")

async def check_access_cmd(update, context):
    chat_id = update.effective_chat.id
    if not has_access(chat_id):
        await update.message.reply_text("❌ Доступа пока нет. Попробуйте позже.",
                                        reply_markup=access_check_kb())
        return
    await update.message.reply_text("✅ Доступ получен!", reply_markup=main_kb(chat_id))

async def version_command(update, context):
    await update.message.reply_text(f"🤖 Версия: {VERSION}")

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id
    if not has_access(chat_id):
        await update.message.reply_text("❌ Нет доступа.", reply_markup=access_check_kb()); return
    if text == "📊 Отчёт по продажам":
        await update.message.reply_text("Отчёты по продажам:", reply_markup=sales_reports_kb())
    elif text == "📦 Отчёт по товарам":
        await update.message.reply_text("Отчёты по товарам:", reply_markup=products_reports_kb())
    elif text == "📈 Отчёт по рекламе":
        await update.message.reply_text("📈 *Отчёт по рекламе*\n\nВыберите действие:",
                                        reply_markup=ad_reports_kb(), parse_mode="Markdown")
    elif text == "🔔 Автоматические рассылки":
        await update.message.reply_text("🔔 *Автоматические рассылки*\n\nВыберите действие:",
                                        reply_markup=auto_kb(), parse_mode="Markdown")
    elif text == "⚙️ Администрирование":
        if not is_admin(chat_id):
            await update.message.reply_text("⛔ Только для администратора."); return
        await update.message.reply_text("Управление менеджерами:", reply_markup=admin_kb())
    elif text == "📚 Справочник расходов":
        if not is_admin(chat_id):
            await update.message.reply_text("⛔ Только для администратора."); return
        await show_expense_types(update, context)
    elif text == "📖 Справка":
        await send_help(update, context)

# ==================== ОТЧЁТ ПО РЕКЛАМЕ — текущий месяц ====================
async def ad_report_month_handler(update, context):
    if not has_access(update.effective_chat.id): return
    status_msg = await update.message.reply_text("⏳ Загружаю отчёт по рекламе...")
    try:
        rpt = await build_ad_report_month(status_msg=status_msg)
        try:
            await status_msg.edit_text(rpt, parse_mode="Markdown")
        except Exception:
            await status_msg.edit_text(rpt)
        await update.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
    except Exception as e:
        write_log(f"❌ Ошибка отчёта по рекламе: {e}")
        try: await status_msg.edit_text(f"❌ Ошибка: {e}")
        except Exception: pass

# ==================== ОТЧЁТ ПО РЕКЛАМЕ — за период ====================
async def ad_period_menu(update, context):
    if not has_access(update.effective_chat.id): return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗓️ По месяцам", callback_data="apm")],
        [InlineKeyboardButton("📅 По кварталам", callback_data="apq")],
        [InlineKeyboardButton("📆 По годам", callback_data="apy")],
        [InlineKeyboardButton("✏️ Произвольный период", callback_data="apc")],
        [InlineKeyboardButton("🔙 Назад", callback_data="apcancel")]])
    await update.message.reply_text("Выберите период:", reply_markup=kb)
    return WAITING_AD_REPORT_PERIOD_TYPE

async def ad_period_type_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    cy = get_moscow_today().year; ys = list(range(cy-9, cy+1))
    if d == "apcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
        return ConversationHandler.END
    if d == "apm":
        btns = [[InlineKeyboardButton(str(y), callback_data=f"apmy_{y}")] for y in ys]
        btns.append([InlineKeyboardButton("🔙 Назад", callback_data="apcancel")])
        await q.edit_message_text("Год:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_AD_REPORT_PERIOD_YEAR
    if d == "apq":
        btns = [[InlineKeyboardButton(str(y), callback_data=f"apqy_{y}")] for y in ys]
        btns.append([InlineKeyboardButton("🔙 Назад", callback_data="apcancel")])
        await q.edit_message_text("Год:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_AD_REPORT_PERIOD_YEAR
    if d == "apy":
        btns = [[InlineKeyboardButton(str(y), callback_data=f"apyy_{y}")] for y in ys]
        btns.append([InlineKeyboardButton("🔙 Назад", callback_data="apcancel")])
        await q.edit_message_text("Год:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_AD_REPORT_PERIOD_YEAR
    if d == "apc":
        now = get_moscow_today()
        await q.edit_message_text("Начало:", reply_markup=create_calendar(now.year, now.month, "aps_"))
        return WAITING_AD_REPORT_PERIOD_START
    return ConversationHandler.END

async def ad_period_year_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "apcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
        return ConversationHandler.END
    if d.startswith("apmy_"):
        y = int(d.split("_")[1])
        btns = [[InlineKeyboardButton(MONTH_NAMES[i-1], callback_data=f"apmm_{i}_{y}")] for i in range(1,13)]
        btns.append([InlineKeyboardButton("🔙", callback_data="apcancel")])
        await q.edit_message_text(f"Месяц {y}:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_AD_REPORT_PERIOD_MONTH
    if d.startswith("apqy_"):
        y = int(d.split("_")[1])
        btns = [[InlineKeyboardButton(f"{qq} кв.", callback_data=f"apqq_{qq}_{y}")] for qq in range(1,5)]
        btns.append([InlineKeyboardButton("🔙", callback_data="apcancel")])
        await q.edit_message_text(f"Квартал {y}:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_AD_REPORT_PERIOD_QUARTER
    if d.startswith("apyy_"):
        y = int(d.split("_")[1])
        df = datetime.date(y,1,1).isoformat(); dt = datetime.date(y,12,31).isoformat()
        status_msg = await q.message.reply_text(f"⏳ Реклама за {y} год...")
        try:
            rpt = await build_ad_report_period(df, dt, f"{y} год", status_msg=status_msg)
            await _safe_edit(status_msg, rpt)
            await q.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await _safe_edit(status_msg, f"❌ {e}")
        return ConversationHandler.END
    return WAITING_AD_REPORT_PERIOD_YEAR

async def ad_period_month_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "apcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
        return ConversationHandler.END
    if d.startswith("apmm_"):
        pr = d.split("_"); m, y = int(pr[1]), int(pr[2])
        f = datetime.date(y,m,1)
        l = datetime.date(y,12,31) if m==12 else datetime.date(y,m+1,1)-datetime.timedelta(days=1)
        nm = f"{MONTH_NAMES[m-1]} {y}"
        status_msg = await q.message.reply_text(f"⏳ Реклама за {nm}...")
        try:
            rpt = await build_ad_report_period(f.isoformat(), l.isoformat(), nm, status_msg=status_msg)
            await _safe_edit(status_msg, rpt)
            await q.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await _safe_edit(status_msg, f"❌ {e}")
        return ConversationHandler.END
    return WAITING_AD_REPORT_PERIOD_MONTH

async def ad_period_quarter_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "apcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
        return ConversationHandler.END
    if d.startswith("apqq_"):
        pr = d.split("_"); qn, y = int(pr[1]), int(pr[2])
        sm = (qn-1)*3 + 1; em = qn*3
        f = datetime.date(y, sm, 1)
        l = datetime.date(y,12,31) if em==12 else datetime.date(y,em+1,1)-datetime.timedelta(days=1)
        nm = f"{qn} квартал {y}"
        status_msg = await q.message.reply_text(f"⏳ Реклама за {nm}...")
        try:
            rpt = await build_ad_report_period(f.isoformat(), l.isoformat(), nm, status_msg=status_msg)
            await _safe_edit(status_msg, rpt)
            await q.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await _safe_edit(status_msg, f"❌ {e}")
        return ConversationHandler.END
    return WAITING_AD_REPORT_PERIOD_QUARTER

async def ad_period_custom_start_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "aps_cancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
        return ConversationHandler.END
    if d.startswith("aps_prev_") or d.startswith("aps_next_"):
        m = re.search(r'(prev|next)_(\d+)_(\d+)', d)
        y, mo = int(m.group(2)), int(m.group(3))
        if m.group(1) == "prev":
            mo -= 1
            if mo == 0: mo, y = 12, y-1
        else:
            mo += 1
            if mo == 13: mo, y = 1, y+1
        await q.edit_message_reply_markup(reply_markup=create_calendar(y, mo, "aps_"))
        return WAITING_AD_REPORT_PERIOD_START
    if d.startswith("aps_"):
        ds = d[4:]
        if re.match(r"\d{4}-\d{2}-\d{2}$", ds):
            ok, r = validate_date(ds)
            if not ok:
                await q.edit_message_text(r); return WAITING_AD_REPORT_PERIOD_START
            context.user_data['ad_p_start'] = ds
            now = get_moscow_today()
            await q.edit_message_text(f"Начало: {ds}\nКонец:",
                reply_markup=create_calendar(now.year, now.month, "ape_"))
            return WAITING_AD_REPORT_PERIOD_END
    return WAITING_AD_REPORT_PERIOD_START

async def ad_period_custom_end_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "ape_cancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
        return ConversationHandler.END
    if d.startswith("ape_prev_") or d.startswith("ape_next_"):
        m = re.search(r'(prev|next)_(\d+)_(\d+)', d)
        y, mo = int(m.group(2)), int(m.group(3))
        if m.group(1) == "prev":
            mo -= 1
            if mo == 0: mo, y = 12, y-1
        else:
            mo += 1
            if mo == 13: mo, y = 1, y+1
        await q.edit_message_reply_markup(reply_markup=create_calendar(y, mo, "ape_"))
        return WAITING_AD_REPORT_PERIOD_END
    if d.startswith("ape_"):
        de = d[4:]
        if re.match(r"\d{4}-\d{2}-\d{2}$", de):
            ds = context.user_data.get('ad_p_start')
            if not ds:
                await q.edit_message_text("❌ потеряна дата"); return ConversationHandler.END
            ok, r = validate_period(ds, de)
            if not ok:
                await q.edit_message_text(r); return WAITING_AD_REPORT_PERIOD_END
            nm = f"{ds} – {de}"
            status_msg = await q.message.reply_text(f"⏳ Реклама за {nm}...")
            try:
                rpt = await build_ad_report_period(ds, de, nm, status_msg=status_msg)
                await _safe_edit(status_msg, rpt)
                await q.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
            except Exception as e:
                write_log(f"❌ {e}"); await _safe_edit(status_msg, f"❌ {e}")
            context.user_data.pop('ad_p_start', None)
            return ConversationHandler.END
    return WAITING_AD_REPORT_PERIOD_END

# ==================== ДИНАМИКА ПО РЕКЛАМЕ ====================
async def ad_dynamics_menu(update, context):
    if not has_access(update.effective_chat.id): return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Текущий год", callback_data="adc")],
        [InlineKeyboardButton("📆 Выбрать год", callback_data="ads")],
        [InlineKeyboardButton("📊 Диапазон лет", callback_data="adr")],
        [InlineKeyboardButton("🔙 Назад", callback_data="adx")]])
    await update.message.reply_text("Динамика по рекламе. Выберите период:", reply_markup=kb)
    return WAITING_AD_DYN_PERIOD_TYPE

async def ad_dynamics_period_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    cy = get_moscow_today().year; ys = list(range(cy-9, cy+1))
    if d == "adx":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
        return ConversationHandler.END
    if d == "adc":
        context.user_data['ad_dyn_years'] = [cy]
        # Переходим к выбору кампании
        return await ad_dynamics_show_campaigns(q, context, [cy])
    if d == "ads":
        btns = [[InlineKeyboardButton(str(y), callback_data=f"ady_{y}")] for y in ys]
        btns.append([InlineKeyboardButton("🔙", callback_data="adx")])
        await q.edit_message_text("Год:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_AD_DYN_YEAR
    if d == "adr":
        await q.edit_message_text("Введите начальный год (например, 2023):")
        return WAITING_AD_DYN_RANGE_START
    return ConversationHandler.END

async def ad_dynamics_year_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "adx":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
        return ConversationHandler.END
    if d.startswith("ady_"):
        y = int(d.split("_")[1])
        context.user_data['ad_dyn_years'] = [y]
        return await ad_dynamics_show_campaigns(q, context, [y])
    return WAITING_AD_DYN_YEAR

async def ad_dynamics_range_start(update, context):
    t = update.message.text.strip()
    if not t.isdigit():
        await update.message.reply_text("❌ Введите число (год)."); return WAITING_AD_DYN_RANGE_START
    y = int(t); cy = get_moscow_today().year
    if y < 2000 or y > cy:
        await update.message.reply_text(f"❌ Год от 2000 до {cy}."); return WAITING_AD_DYN_RANGE_START
    context.user_data['ad_dyn_rs'] = y
    await update.message.reply_text("Введите конечный год:")
    return WAITING_AD_DYN_RANGE_END

async def ad_dynamics_range_end(update, context):
    t = update.message.text.strip()
    if not t.isdigit():
        await update.message.reply_text("❌ Введите число."); return WAITING_AD_DYN_RANGE_END
    ye = int(t); ys_ = context.user_data.get('ad_dyn_rs')
    if ys_ is None:
        await update.message.reply_text("❌ Начните заново."); return ConversationHandler.END
    if ye < ys_: await update.message.reply_text("❌ Конец < начала."); return WAITING_AD_DYN_RANGE_END
    years = list(range(ys_, ye + 1))
    if len(years) > 10:
        await update.message.reply_text("⚠️ Максимум 10 лет."); return ConversationHandler.END
    context.user_data['ad_dyn_years'] = years
    # Показываем список кампаний через fake msg
    msg = await update.message.reply_text("⏳ Загружаю список кампаний...")
    return await ad_dynamics_show_campaigns_msg(msg, context, years)

async def ad_dynamics_show_campaigns(q_or_msg, context, years):
    """q_or_msg — либо callback_query, либо message."""
    chat_id = q_or_msg.message.chat_id if hasattr(q_or_msg, 'message') else q_or_msg.chat_id
    # Загружаем кампании
    token = await get_performance_token()
    if not token:
        if hasattr(q_or_msg, 'edit_message_text'):
            await q_or_msg.edit_message_text("❌ Не удалось получить токен.")
        else:
            await q_or_msg.edit_text("❌ Не удалось получить токен.")
        return ConversationHandler.END
    campaigns = await ad_fetch_campaigns(token)
    if not campaigns:
        await q_or_msg.edit_message_text("❌ Кампании не найдены.")
        return ConversationHandler.END
    context.user_data['ad_dyn_campaigns'] = [(str(c.get("id", "")), c.get("title", str(c.get("id"))))
                                              for c in campaigns if c.get("id")]
    kb = [[InlineKeyboardButton("🌐 Все кампании", callback_data="adcamp_all")]]
    for cid, title in context.user_data['ad_dyn_campaigns'][:30]:
        kb.append([InlineKeyboardButton(title[:40], callback_data=f"adcamp_{cid}")])
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="adcamp_cancel")])
    text = "Выберите кампанию:"
    if hasattr(q_or_msg, 'edit_message_text'):
        await q_or_msg.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await q_or_msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return WAITING_AD_DYN_CAMPAIGN_SELECT

async def ad_dynamics_show_campaigns_msg(msg, context, years):
    token = await get_performance_token()
    if not token:
        await msg.edit_text("❌ Не удалось получить токен.")
        return ConversationHandler.END
    campaigns = await ad_fetch_campaigns(token)
    if not campaigns:
        await msg.edit_text("❌ Кампании не найдены.")
        return ConversationHandler.END
    context.user_data['ad_dyn_campaigns'] = [(str(c.get("id", "")), c.get("title", str(c.get("id"))))
                                              for c in campaigns if c.get("id")]
    kb = [[InlineKeyboardButton("🌐 Все кампании", callback_data="adcamp_all")]]
    for cid, title in context.user_data['ad_dyn_campaigns'][:30]:
        kb.append([InlineKeyboardButton(title[:40], callback_data=f"adcamp_{cid}")])
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="adcamp_cancel")])
    await msg.edit_text("Выберите кампанию:", reply_markup=InlineKeyboardMarkup(kb))
    return WAITING_AD_DYN_CAMPAIGN_SELECT

async def ad_dynamics_campaign_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "adcamp_cancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
        return ConversationHandler.END
    if d == "adcamp_all":
        context.user_data['ad_dyn_campaign_ids'] = None  # все
        context.user_data['ad_dyn_campaign_title'] = "Все кампании"
    elif d.startswith("adcamp_"):
        cid = d[7:]
        context.user_data['ad_dyn_campaign_ids'] = [cid]
        title = cid
        for c_id, t in context.user_data.get('ad_dyn_campaigns', []):
            if c_id == cid: title = t; break
        context.user_data['ad_dyn_campaign_title'] = title
    else:
        return WAITING_AD_DYN_CAMPAIGN_SELECT
    # Показываем выбор метрики
    kb = []
    for key, label, _ in AD_DYN_METRICS:
        kb.append([InlineKeyboardButton(label, callback_data=f"admet_{key}")])
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="admet_cancel")])
    await q.edit_message_text("Выберите метрику:", reply_markup=InlineKeyboardMarkup(kb))
    return WAITING_AD_DYN_METRIC

async def ad_dynamics_metric_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "admet_cancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
        return ConversationHandler.END
    if d.startswith("admet_"):
        metric_key = d[6:]
        metric_label = metric_key
        for k, lbl, _ in AD_DYN_METRICS:
            if k == metric_key: metric_label = lbl; break
        years = context.user_data.get('ad_dyn_years', [])
        campaign_ids_sel = context.user_data.get('ad_dyn_campaign_ids', None)
        camp_title = context.user_data.get('ad_dyn_campaign_title', 'Все кампании')
        if not years:
            await q.edit_message_text("❌ Потерян период.")
            return ConversationHandler.END
        await q.edit_message_text(f"⏳ Строю график: {metric_label}\nКампания: {camp_title}")
        try:
            token = await get_performance_token()
            if not token:
                await q.edit_message_text("❌ Токен не получен.")
                return ConversationHandler.END
            campaigns, skus_by_campaign = await _get_all_campaign_data(token, q.message)
            if campaign_ids_sel is None:
                campaign_ids = [str(c.get("id")) for c in campaigns if c.get("id")]
            else:
                campaign_ids = campaign_ids_sel
            buf = await generate_ad_dynamics_chart(token, campaign_ids, skus_by_campaign,
                                                    years, metric_key, metric_label, None)
            if buf:
                caption = f"{camp_title} | {metric_label} | {years[0]}-{years[-1]}" if len(years) > 1 else f"{camp_title} | {metric_label} | {years[0]}"
                await q.message.reply_photo(photo=buf, caption=caption)
                await q.delete_message()
                await q.message.reply_text("Выберите действие:", reply_markup=ad_reports_kb())
            else:
                await q.edit_message_text("❌ Нет данных.")
        except Exception as e:
            write_log(f"❌ Динамика рекламы: {e}")
            try: await q.edit_message_text(f"❌ Ошибка: {e}")
            except: pass
        return ConversationHandler.END
    return WAITING_AD_DYN_METRIC

# ==================== СПРАВОЧНИК РАСХОДОВ (UI) ====================
async def show_expense_types(update, context):
    data = get_expense_types()
    if not data:
        await update.message.reply_text("Справочник пуст.")
        return
    items = []
    for tid, entry in data.items():
        if isinstance(entry, dict):
            name = entry.get("name", "?"); cnt = entry.get("count", 0); sm = entry.get("sum", 0.0)
        else:
            name = str(entry); cnt = 0; sm = 0.0
        items.append((tid, name, cnt, sm))
    def sort_key(x):
        is_unknown = x[1].startswith("❓") or "(type " in x[1]
        return (is_unknown, -x[3])
    items.sort(key=sort_key)
    known = [i for i in items if not (i[1].startswith("❓") or "(type " in i[1] and "Услуги" in i[1])]
    unknown = [i for i in items if i not in known]
    lines = ["📚 *Справочник расходов*", ""]
    lines.append(f"*Распознано:* {len(known)} | *Требуют уточнения:* {len(unknown)}")
    lines.append("")
    if known:
        lines.append("✅ *Известные:*")
        for tid, name, cnt, sm in known[:30]:
            lines.append(f"  `{tid}` → {name}" + (f" ({cnt}× / {sm:,.0f} ₽)" if cnt > 0 else ""))
        lines.append("")
    if unknown:
        lines.append("❓ *Требуют уточнения:*")
        for tid, name, cnt, sm in unknown[:30]:
            lines.append(f"  `{tid}` → {name}" + (f" ({cnt}× / {sm:,.0f} ₽)" if cnt > 0 else ""))
    text = "\n".join(lines)
    if len(text) > 4000: text = text[:4000] + "\n\n_…обрезано_"
    try:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=expense_types_kb())
    except Exception:
        await update.message.reply_text(text[:4000], reply_markup=expense_types_kb())

async def expense_types_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    chat_id = q.message.chat_id
    if not is_admin(chat_id):
        await q.edit_message_text("⛔ Только для админа."); return
    if d == "et_back":
        await q.edit_message_text("Главное меню:", reply_markup=None)
        await q.message.reply_text("Выберите действие:", reply_markup=main_kb(chat_id))
    elif d == "et_refresh":
        global _expense_types_cache
        _expense_types_cache = _load_expense_types_sync()
        await q.edit_message_text("🔄 Справочник перечитан.")

async def send_help(update, context):
    chat_id = update.effective_chat.id
    common = (
        "📖 *Справка*\n\n"
        "🔹 *Основные функции*\n"
        "• 📊 Отчёт по продажам – сводка за сегодня и текущий месяц.\n"
        "• 📦 Отчёт по товарам – топ товаров по выручке.\n"
        "• 📈 Отчёт по рекламе – статистика рекламных кампаний.\n"
        "• 📆 Выбрать дату – данные за конкретный день.\n"
        "• 📊 Выбрать период – месяц/квартал/год/произвольный.\n"
        "• 📈 Динамика продаж – график доставленных заказов.\n"
        "• 📈 Динамика по товару – график продаж товара.\n"
        "• 🔔 Автоматические рассылки – персональная настройка.\n\n"
        "🔹 *Отчёт по рекламе*\n"
        "• 📅 Реклама за текущий месяц – сводка с VS.\n"
        "• 📅 Реклама за период – месяц/квартал/год/произвольный.\n"
        "• 📈 Динамика по рекламе – график по метрике.\n\n"
        "🔹 *Автоматические отчёты*\n"
        "• 🕒 Выбор времени рассылок – обычный отчёт.\n"
        "• 📅 Добавление отчёта за Вчера – с блоком «Вчера».\n"
        "• 🔕 Режим тишины.\n"
        "• 📤 Отправить сейчас.\n\n"
        "🔹 *Часовой пояс*\n"
        "• Все расчёты – по МСК (UTC+3).\n\n"
    )
    if is_admin(chat_id):
        help_text = common + (
            "🔹 *Администрирование (только админ)*\n"
            "• ➕ Добавить менеджера.\n"
            "• ➖ Удалить менеджера.\n"
            "• 📋 Список менеджеров.\n"
            "• 📚 Справочник расходов.\n\n"
            f"🤖 Версия бота: {VERSION}"
        )
    else:
        help_text = common + f"🤖 Версия бота: {VERSION}"
    await update.message.reply_text(help_text, parse_mode="Markdown")

# ==================== ПРОДАЖИ ====================
async def today_report(update, context):
    if not has_access(update.effective_chat.id): return
    msg = await update.message.reply_text("⏳ Загружаю данные за сегодня...")
    try:
        r = await build_today_report(include_yesterday=False)
        await msg.delete()
        await update.message.reply_text(r, parse_mode="Markdown")
    except Exception as e:
        write_log(f"❌ {e}"); await msg.edit_text(f"❌ Ошибка: {e}")

async def date_menu(update, context):
    if not has_access(update.effective_chat.id): return
    now = get_moscow_today()
    await update.message.reply_text("Выберите дату:",
        reply_markup=create_calendar(now.year, now.month, "d_"))
    return WAITING_DATE_SINGLE

async def date_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "d_cancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
        return ConversationHandler.END
    if d.startswith("d_prev_") or d.startswith("d_next_"):
        m = re.search(r'(prev|next)_(\d+)_(\d+)', d)
        y, mo = int(m.group(2)), int(m.group(3))
        if m.group(1) == "prev":
            mo -= 1
            if mo == 0: mo, y = 12, y-1
        else:
            mo += 1
            if mo == 13: mo, y = 1, y+1
        await q.edit_message_reply_markup(reply_markup=create_calendar(y, mo, "d_"))
        return WAITING_DATE_SINGLE
    if d.startswith("d_"):
        ds = d[2:]
        if re.match(r"\d{4}-\d{2}-\d{2}$", ds):
            ok, r = validate_date(ds)
            if not ok: await q.edit_message_text(r); return WAITING_DATE_SINGLE
            status_msg = await q.message.reply_text(f"⏳ Данные за {ds}...")
            try:
                rpt = await build_period_report(ds, ds, ds, status_msg=status_msg)
                await _safe_edit(status_msg, rpt)
                await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
            except Exception as e:
                write_log(f"❌ {e}"); await _safe_edit(status_msg, f"❌ {e}")
            return ConversationHandler.END
    return WAITING_DATE_SINGLE

async def period_menu(update, context):
    if not has_access(update.effective_chat.id): return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗓️ По месяцам", callback_data="pm")],
        [InlineKeyboardButton("📅 По кварталам", callback_data="pq")],
        [InlineKeyboardButton("📆 По годам", callback_data="py")],
        [InlineKeyboardButton("✏️ Произвольный", callback_data="pcustom")],
        [InlineKeyboardButton("🔙 Назад", callback_data="pcancel")]])
    await update.message.reply_text("Выберите период:", reply_markup=kb)
    return WAITING_PERIOD_TYPE

async def period_type_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    cy = get_moscow_today().year; ys = list(range(cy-9, cy+1))
    if d == "pcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
        return ConversationHandler.END
    if d == "pm":
        btns = [[InlineKeyboardButton(str(y), callback_data=f"pmy_{y}")] for y in ys]
        btns.append([InlineKeyboardButton("🔙 Назад", callback_data="pcancel")])
        await q.edit_message_text("Год:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_PERIOD_YEAR
    if d == "pq":
        btns = [[InlineKeyboardButton(str(y), callback_data=f"pqy_{y}")] for y in ys]
        btns.append([InlineKeyboardButton("🔙 Назад", callback_data="pcancel")])
        await q.edit_message_text("Год:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_PERIOD_YEAR
    if d == "py":
        btns = [[InlineKeyboardButton(str(y), callback_data=f"pyy_{y}")] for y in ys]
        btns.append([InlineKeyboardButton("🔙 Назад", callback_data="pcancel")])
        await q.edit_message_text("Год:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_YEAR_SELECT
    if d == "pcustom":
        now = get_moscow_today()
        await q.edit_message_text("Начало:", reply_markup=create_calendar(now.year, now.month, "s_"))
        return WAITING_PERIOD_START
    return ConversationHandler.END

async def period_year_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "pcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
        return ConversationHandler.END
    if d.startswith("pmy_"):
        y = int(d.split("_")[1])
        btns = [[InlineKeyboardButton(MONTH_NAMES[i-1], callback_data=f"pmm_{i}_{y}")] for i in range(1,13)]
        btns.append([InlineKeyboardButton("🔙", callback_data="pcancel")])
        await q.edit_message_text(f"Месяц {y}:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_PERIOD_MONTH
    if d.startswith("pqy_"):
        y = int(d.split("_")[1])
        btns = [[InlineKeyboardButton(f"{qq} кв.", callback_data=f"pqq_{qq}_{y}")] for qq in range(1,5)]
        btns.append([InlineKeyboardButton("🔙", callback_data="pcancel")])
        await q.edit_message_text(f"Квартал {y}:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_PERIOD_QUARTER
    if d.startswith("pyy_"):
        y = int(d.split("_")[1])
        df = datetime.date(y,1,1).isoformat(); dt = datetime.date(y,12,31).isoformat()
        status_msg = await q.message.reply_text(f"⏳ За {y} год. Первый запрос может занять до 15 минут...")
        try:
            rpt = await build_period_report(df, dt, f"{y} год", status_msg=status_msg)
            await _safe_edit(status_msg, rpt)
            await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await _safe_edit(status_msg, f"❌ {e}")
        return ConversationHandler.END
    return WAITING_PERIOD_YEAR

async def period_month_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "pcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
        return ConversationHandler.END
    if d.startswith("pmm_"):
        pr = d.split("_"); m, y = int(pr[1]), int(pr[2])
        f = datetime.date(y,m,1)
        l = datetime.date(y,12,31) if m==12 else datetime.date(y,m+1,1)-datetime.timedelta(days=1)
        name = f"{MONTH_NAMES[m-1]} {y}"
        status_msg = await q.message.reply_text(f"⏳ За {name}...")
        try:
            rpt = await build_period_report(f.isoformat(), l.isoformat(), name, status_msg=status_msg)
            await _safe_edit(status_msg, rpt)
            await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await _safe_edit(status_msg, f"❌ {e}")
        return ConversationHandler.END
    return WAITING_PERIOD_MONTH

async def period_quarter_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "pcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
        return ConversationHandler.END
    if d.startswith("pqq_"):
        pr = d.split("_"); qn, y = int(pr[1]), int(pr[2])
        sm = (qn-1)*3 + 1; em = qn*3
        f = datetime.date(y, sm, 1)
        l = datetime.date(y,12,31) if em==12 else datetime.date(y,em+1,1)-datetime.timedelta(days=1)
        name = f"{qn} квартал {y}"
        status_msg = await q.message.reply_text(f"⏳ За {name}...")
        try:
            rpt = await build_period_report(f.isoformat(), l.isoformat(), name, status_msg=status_msg)
            await _safe_edit(status_msg, rpt)
            await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await _safe_edit(status_msg, f"❌ {e}")
        return ConversationHandler.END
    return WAITING_PERIOD_QUARTER

async def custom_start_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "s_cancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
        return ConversationHandler.END
    if d.startswith("s_prev_") or d.startswith("s_next_"):
        m = re.search(r'(prev|next)_(\d+)_(\d+)', d)
        y, mo = int(m.group(2)), int(m.group(3))
        if m.group(1) == "prev":
            mo -= 1
            if mo == 0: mo, y = 12, y-1
        else:
            mo += 1
            if mo == 13: mo, y = 1, y+1
        await q.edit_message_reply_markup(reply_markup=create_calendar(y, mo, "s_"))
        return WAITING_PERIOD_START
    if d.startswith("s_"):
        ds = d[2:]
        if re.match(r"\d{4}-\d{2}-\d{2}$", ds):
            ok, r = validate_date(ds)
            if not ok: await q.edit_message_text(r); return WAITING_PERIOD_START
            context.user_data['p_start'] = ds
            now = get_moscow_today()
            await q.edit_message_text(f"Начало: {ds}\nКонец:",
                reply_markup=create_calendar(now.year, now.month, "e_"))
            return WAITING_PERIOD_END
    return WAITING_PERIOD_START

async def custom_end_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "e_cancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
        return ConversationHandler.END
    if d.startswith("e_prev_") or d.startswith("e_next_"):
        m = re.search(r'(prev|next)_(\d+)_(\d+)', d)
        y, mo = int(m.group(2)), int(m.group(3))
        if m.group(1) == "prev":
            mo -= 1
            if mo == 0: mo, y = 12, y-1
        else:
            mo += 1
            if mo == 13: mo, y = 1, y+1
        await q.edit_message_reply_markup(reply_markup=create_calendar(y, mo, "e_"))
        return WAITING_PERIOD_END
    if d.startswith("e_"):
        de = d[2:]
        if re.match(r"\d{4}-\d{2}-\d{2}$", de):
            ds = context.user_data.get('p_start')
            if not ds: await q.edit_message_text("❌ потеряна дата"); return ConversationHandler.END
            ok, r = validate_period(ds, de)
            if not ok: await q.edit_message_text(r); return WAITING_PERIOD_END
            nm = f"{ds} – {de}"
            status_msg = await q.message.reply_text(f"⏳ За {nm}. Первый запрос может занять до 15 минут...")
            try:
                rpt = await build_period_report(ds, de, nm, status_msg=status_msg)
                await _safe_edit(status_msg, rpt)
                await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
            except Exception as e:
                write_log(f"❌ {e}"); await _safe_edit(status_msg, f"❌ {e}")
            context.user_data.pop('p_start', None)
            return ConversationHandler.END
    return WAITING_PERIOD_END

# ==================== ТОВАРЫ ====================
async def top_today(update, context):
    if not has_access(update.effective_chat.id): return
    msg = await update.message.reply_text("⏳ Загружаю товары за сегодня...")
    now = get_current_time_msk(); td = now.date().isoformat()
    try:
        postings = await fetch_postings(td, td)
        stats = aggregate_products(postings, td, td, tl=now.time(), ad=td)
        txt = format_top_products(stats, f"Топ товаров за {td}", 15) + "\n" + format_products_summary(stats)
        await msg.delete()
        await update.message.reply_text(txt)
    except Exception as e:
        write_log(f"❌ {e}"); await msg.edit_text(f"❌ {e}")

async def products_period_menu(update, context):
    if not has_access(update.effective_chat.id): return
    await update.message.reply_text("Период для товаров:", reply_markup=products_period_kb())
    return WAITING_PRODUCT_PERIOD_TYPE

async def products_period_type_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    cy = get_moscow_today().year; ys = list(range(cy-9, cy+1))
    if d == "tpcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
        return ConversationHandler.END
    if d in ("tpm", "tpq"):
        prefix = "tmy_" if d == "tpm" else "tqy_"
        btns = [[InlineKeyboardButton(str(y), callback_data=f"{prefix}{y}")] for y in ys]
        btns.append([InlineKeyboardButton("🔙", callback_data="tpcancel")])
        await q.edit_message_text("Год:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_PRODUCT_YEAR
    if d == "tpy":
        btns = [[InlineKeyboardButton(str(y), callback_data=f"tyy_{y}")] for y in ys]
        btns.append([InlineKeyboardButton("🔙", callback_data="tpcancel")])
        await q.edit_message_text("Год:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_PRODUCT_YEAR_SELECT
    if d == "tpc":
        now = get_moscow_today()
        await q.edit_message_text("Начало:", reply_markup=create_calendar(now.year, now.month, "ts_"))
        return WAITING_PRODUCT_PERIOD_START
    return ConversationHandler.END

async def products_year_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "tpcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
        return ConversationHandler.END
    if d.startswith("tmy_"):
        y = int(d.split("_")[1])
        btns = [[InlineKeyboardButton(MONTH_NAMES[i-1], callback_data=f"tmm_{i}_{y}")] for i in range(1,13)]
        btns.append([InlineKeyboardButton("🔙", callback_data="tpcancel")])
        await q.edit_message_text(f"Месяц {y}:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_PRODUCT_MONTH
    if d.startswith("tqy_"):
        y = int(d.split("_")[1])
        btns = [[InlineKeyboardButton(f"{qq} кв.", callback_data=f"tqq_{qq}_{y}")] for qq in range(1,5)]
        btns.append([InlineKeyboardButton("🔙", callback_data="tpcancel")])
        await q.edit_message_text(f"Квартал {y}:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_PRODUCT_QUARTER
    if d.startswith("tyy_"):
        y = int(d.split("_")[1])
        df = datetime.date(y,1,1).isoformat(); dt = datetime.date(y,12,31).isoformat()
        status_msg = await q.message.reply_text(f"⏳ Товары за {y} год...")
        try:
            postings = await fetch_postings(df, dt)
            stats = aggregate_products(postings, df, dt)
            txt = format_top_products(stats, f"Товары за {y} год", 20) + "\n" + format_products_summary(stats)
            await _safe_edit(status_msg, txt, parse_mode=None)
            await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await _safe_edit(status_msg, f"❌ {e}")
        return ConversationHandler.END
    return WAITING_PRODUCT_YEAR

async def products_month_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "tpcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
        return ConversationHandler.END
    if d.startswith("tmm_"):
        pr = d.split("_"); m, y = int(pr[1]), int(pr[2])
        f = datetime.date(y,m,1)
        l = datetime.date(y,12,31) if m==12 else datetime.date(y,m+1,1)-datetime.timedelta(days=1)
        nm = f"{MONTH_NAMES[m-1]} {y}"
        status_msg = await q.message.reply_text(f"⏳ Товары за {nm}...")
        try:
            postings = await fetch_postings(f.isoformat(), l.isoformat())
            stats = aggregate_products(postings, f.isoformat(), l.isoformat())
            txt = format_top_products(stats, f"Товары за {nm}", 20) + "\n" + format_products_summary(stats)
            await _safe_edit(status_msg, txt, parse_mode=None)
            await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await _safe_edit(status_msg, f"❌ {e}")
        return ConversationHandler.END
    return WAITING_PRODUCT_MONTH

async def products_quarter_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "tpcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
        return ConversationHandler.END
    if d.startswith("tqq_"):
        pr = d.split("_"); qn, y = int(pr[1]), int(pr[2])
        sm = (qn-1)*3 + 1; em = qn*3
        f = datetime.date(y, sm, 1)
        l = datetime.date(y,12,31) if em==12 else datetime.date(y,em+1,1)-datetime.timedelta(days=1)
        nm = f"{qn} квартал {y}"
        status_msg = await q.message.reply_text(f"⏳ Товары за {nm}...")
        try:
            postings = await fetch_postings(f.isoformat(), l.isoformat())
            stats = aggregate_products(postings, f.isoformat(), l.isoformat())
            txt = format_top_products(stats, f"Товары за {nm}", 20) + "\n" + format_products_summary(stats)
            await _safe_edit(status_msg, txt, parse_mode=None)
            await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await _safe_edit(status_msg, f"❌ {e}")
        return ConversationHandler.END
    return WAITING_PRODUCT_QUARTER

async def products_custom_start(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "ts_cancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
        return ConversationHandler.END
    if d.startswith("ts_prev_") or d.startswith("ts_next_"):
        m = re.search(r'(prev|next)_(\d+)_(\d+)', d)
        y, mo = int(m.group(2)), int(m.group(3))
        if m.group(1) == "prev":
            mo -= 1
            if mo == 0: mo, y = 12, y-1
        else:
            mo += 1
            if mo == 13: mo, y = 1, y+1
        await q.edit_message_reply_markup(reply_markup=create_calendar(y, mo, "ts_"))
        return WAITING_PRODUCT_PERIOD_START
    if d.startswith("ts_"):
        ds = d[3:]
        if re.match(r"\d{4}-\d{2}-\d{2}$", ds):
            ok, r = validate_date(ds)
            if not ok: await q.edit_message_text(r); return WAITING_PRODUCT_PERIOD_START
            context.user_data['tp_start'] = ds
            now = get_moscow_today()
            await q.edit_message_text(f"Начало: {ds}\nКонец:",
                reply_markup=create_calendar(now.year, now.month, "te_"))
            return WAITING_PRODUCT_PERIOD_END
    return WAITING_PRODUCT_PERIOD_START

async def products_custom_end(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "te_cancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
        return ConversationHandler.END
    if d.startswith("te_prev_") or d.startswith("te_next_"):
        m = re.search(r'(prev|next)_(\d+)_(\d+)', d)
        y, mo = int(m.group(2)), int(m.group(3))
        if m.group(1) == "prev":
            mo -= 1
            if mo == 0: mo, y = 12, y-1
        else:
            mo += 1
            if mo == 13: mo, y = 1, y+1
        await q.edit_message_reply_markup(reply_markup=create_calendar(y, mo, "te_"))
        return WAITING_PRODUCT_PERIOD_END
    if d.startswith("te_"):
        de = d[3:]
        if re.match(r"\d{4}-\d{2}-\d{2}$", de):
            ds = context.user_data.get('tp_start')
            if not ds: await q.edit_message_text("❌ потеряна дата"); return ConversationHandler.END
            ok, r = validate_period(ds, de)
            if not ok: await q.edit_message_text(r); return WAITING_PRODUCT_PERIOD_END
            nm = f"{ds} – {de}"
            status_msg = await q.message.reply_text(f"⏳ Товары за {nm}...")
            try:
                postings = await fetch_postings(ds, de)
                stats = aggregate_products(postings, ds, de)
                txt = format_top_products(stats, f"Товары за {nm}", 20) + "\n" + format_products_summary(stats)
                await _safe_edit(status_msg, txt, parse_mode=None)
                await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
            except Exception as e:
                write_log(f"❌ {e}"); await _safe_edit(status_msg, f"❌ {e}")
            context.user_data.pop('tp_start', None)
            return ConversationHandler.END
    return WAITING_PRODUCT_PERIOD_END

# ==================== ГРАФИКИ ПРОДАЖ ====================
async def dynamics_menu(update, context):
    if not has_access(update.effective_chat.id): return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Текущий год", callback_data="dyc")],
        [InlineKeyboardButton("📆 Выбрать год", callback_data="dys")],
        [InlineKeyboardButton("📊 Диапазон лет", callback_data="dyr")],
        [InlineKeyboardButton("🔙 Назад", callback_data="dyx")]])
    await update.message.reply_text("Динамика доставленных заказов по месяцам:", reply_markup=kb)
    return WAITING_DYN_SELECT

async def dynamics_select_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    cy = get_moscow_today().year; ys = list(range(cy-9, cy+1))
    if d == "dyx":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
        return ConversationHandler.END
    if d == "dyc":
        await q.edit_message_text(f"⏳ Строю график за {cy}...")
        try:
            buf = await generate_sales_chart([cy])
            await q.message.reply_photo(photo=buf, caption=f"Динамика за {cy}")
            await q.delete_message()
            await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await _safe_edit(q.message, f"❌ {e}")
        return ConversationHandler.END
    if d == "dys":
        btns = [[InlineKeyboardButton(str(y), callback_data=f"dyy_{y}")] for y in ys]
        btns.append([InlineKeyboardButton("🔙", callback_data="dyx")])
        await q.edit_message_text("Год:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_DYN_YEAR
    if d == "dyr":
        await q.edit_message_text("Введите начальный год (например, 2023):")
        return WAITING_DYN_RANGE_START
    return ConversationHandler.END

async def dynamics_year_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "dyx":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
        return ConversationHandler.END
    if d.startswith("dyy_"):
        y = int(d.split("_")[1])
        await q.edit_message_text(f"⏳ График за {y}...")
        try:
            buf = await generate_sales_chart([y])
            await q.message.reply_photo(photo=buf, caption=f"Динамика за {y}")
            await q.delete_message()
            await q.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await _safe_edit(q.message, f"❌ {e}")
        return ConversationHandler.END
    return WAITING_DYN_YEAR

async def dynamics_range_start(update, context):
    t = update.message.text.strip()
    if not t.isdigit(): await update.message.reply_text("❌ Введите число."); return WAITING_DYN_RANGE_START
    y = int(t); cy = get_moscow_today().year
    if y < 2000 or y > cy: await update.message.reply_text(f"❌ Год от 2000 до {cy}."); return WAITING_DYN_RANGE_START
    context.user_data['dyn_rs'] = y
    await update.message.reply_text("Введите конечный год:")
    return WAITING_DYN_RANGE_END

async def dynamics_range_end(update, context):
    t = update.message.text.strip()
    if not t.isdigit(): await update.message.reply_text("❌ Введите число."); return WAITING_DYN_RANGE_END
    ye = int(t); ys_ = context.user_data.get('dyn_rs')
    if ys_ is None: await update.message.reply_text("❌ Начните заново."); return ConversationHandler.END
    if ye < ys_: await update.message.reply_text("❌ Конец < начала."); return WAITING_DYN_RANGE_END
    years = list(range(ys_, ye + 1))
    if len(years) > 10: await update.message.reply_text("⚠️ Максимум 10 лет."); return ConversationHandler.END
    msg = await update.message.reply_text(f"⏳ График за {ys_}-{ye}...")
    try:
        buf = await generate_sales_chart(years)
        await msg.delete()
        await update.message.reply_photo(photo=buf, caption=f"Динамика за {ys_}-{ye}")
        await update.message.reply_text("Выберите действие:", reply_markup=sales_reports_kb())
    except Exception as e:
        write_log(f"❌ {e}"); await msg.edit_text(f"❌ {e}")
    context.user_data.pop('dyn_rs', None)
    return ConversationHandler.END

async def product_chart_menu(update, context):
    if not has_access(update.effective_chat.id): return
    msg = await update.message.reply_text("⏳ Загружаю топ товаров за 30 дней...")
    today = get_moscow_today(); td = today.isoformat()
    start = (today - datetime.timedelta(days=30)).isoformat()
    try:
        postings = await fetch_postings(start, td)
        stats = aggregate_products(postings, start, td)
        items = sorted(stats.items(), key=lambda x: x[1]["ordered_sum"], reverse=True)[:30]
        if not items: await msg.edit_text("❌ Нет данных."); return ConversationHandler.END
        context.user_data['pc_list'] = items
        kb = []
        for sku, s in items:
            nm = s["name"][:18] + ("..." if len(s["name"]) > 18 else "")
            oid = s.get("offer_id") or ""
            btn = f"{oid} | {nm}" if oid else nm
            kb.append([InlineKeyboardButton(btn[:60], callback_data=f"pcs_{sku}")])
        kb.append([InlineKeyboardButton("🔙 Назад", callback_data="pcx")])
        await msg.edit_text("Выберите товар:", reply_markup=InlineKeyboardMarkup(kb))
        return WAITING_PC_LIST
    except Exception as e:
        write_log(f"❌ {e}"); await msg.edit_text(f"❌ {e}"); return ConversationHandler.END

async def product_chart_list_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "pcx":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
        return ConversationHandler.END
    if d.startswith("pcs_"):
        sku = d[4:]
        context.user_data['pc_sku'] = sku
        nm = "Товар"
        for s_sku, s in context.user_data.get('pc_list', []):
            if s_sku == sku: nm = s["name"][:40]; break
        context.user_data['pc_name'] = nm
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Заказано (₽)", callback_data="pcm_ordered_sum")],
            [InlineKeyboardButton("Заказано (шт.)", callback_data="pcm_ordered_units")],
            [InlineKeyboardButton("Доставлено (₽)", callback_data="pcm_delivered_sum")],
            [InlineKeyboardButton("Доставлено (шт.)", callback_data="pcm_delivered_units")],
            [InlineKeyboardButton("Отменено (₽)", callback_data="pcm_canceled_sum")],
            [InlineKeyboardButton("Отменено (шт.)", callback_data="pcm_canceled_units")],
            [InlineKeyboardButton("Средний чек (₽)", callback_data="pcm_avg_check")],
            [InlineKeyboardButton("🔙 Назад", callback_data="pcx")]])
        await q.edit_message_text(f"Товар: {nm} (SKU: {sku})\nВыберите метрику:", reply_markup=kb)
        return WAITING_PC_METRIC
    return WAITING_PC_LIST

async def product_chart_metric_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "pcx":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
        return ConversationHandler.END
    if d.startswith("pcm_"):
        m = d[4:]
        context.user_data['pc_metric'] = m
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Текущий год", callback_data="pcp_current")],
            [InlineKeyboardButton("📆 Выбрать год", callback_data="pcp_year")],
            [InlineKeyboardButton("📊 Диапазон лет", callback_data="pcp_range")],
            [InlineKeyboardButton("🔙 Назад", callback_data="pcx")]])
        await q.edit_message_text(f"Метрика: {METRIC_LABELS.get(m, m)}\nВыберите период:", reply_markup=kb)
        return WAITING_PC_PERIOD
    return WAITING_PC_METRIC

async def product_chart_period_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    sku = context.user_data.get('pc_sku'); metric = context.user_data.get('pc_metric')
    if not sku or not metric: await q.edit_message_text("❌ Потеряны данные."); return ConversationHandler.END
    cy = get_moscow_today().year; ys = list(range(cy-9, cy+1))
    if d == "pcx":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
        return ConversationHandler.END
    if d == "pcp_current":
        await q.edit_message_text("⏳ Строю график...")
        try:
            buf = await generate_product_chart(sku, metric, [cy])
            nm = context.user_data.get('pc_name', sku)
            await q.message.reply_photo(photo=buf, caption=f"{nm} | {METRIC_LABELS.get(metric, metric)} | {cy}")
            await q.delete_message()
            await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await _safe_edit(q.message, f"❌ {e}")
        return ConversationHandler.END
    if d == "pcp_year":
        btns = [[InlineKeyboardButton(str(y), callback_data=f"pcy_{y}")] for y in ys]
        btns.append([InlineKeyboardButton("🔙", callback_data="pcx")])
        await q.edit_message_text("Год:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_PC_YEAR
    if d == "pcp_range":
        await q.edit_message_text("Введите начальный год:")
        return WAITING_PC_RANGE_START
    return ConversationHandler.END

async def product_chart_year_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    sku = context.user_data.get('pc_sku'); metric = context.user_data.get('pc_metric')
    if not sku or not metric: await q.edit_message_text("❌ Потеряны данные."); return ConversationHandler.END
    if d == "pcx":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
        return ConversationHandler.END
    if d.startswith("pcy_"):
        y = int(d.split("_")[1])
        await q.edit_message_text(f"⏳ График за {y}...")
        try:
            buf = await generate_product_chart(sku, metric, [y])
            nm = context.user_data.get('pc_name', sku)
            await q.message.reply_photo(photo=buf, caption=f"{nm} | {METRIC_LABELS.get(metric, metric)} | {y}")
            await q.delete_message()
            await q.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await _safe_edit(q.message, f"❌ {e}")
        return ConversationHandler.END
    return WAITING_PC_YEAR

async def product_chart_range_start(update, context):
    t = update.message.text.strip()
    if not t.isdigit(): await update.message.reply_text("❌ Введите число."); return WAITING_PC_RANGE_START
    y = int(t); cy = get_moscow_today().year
    if y < 2000 or y > cy: await update.message.reply_text(f"❌ Год 2000-{cy}."); return WAITING_PC_RANGE_START
    context.user_data['pc_rs'] = y
    await update.message.reply_text("Введите конечный год:")
    return WAITING_PC_RANGE_END

async def product_chart_range_end(update, context):
    t = update.message.text.strip()
    if not t.isdigit(): await update.message.reply_text("❌ Введите число."); return WAITING_PC_RANGE_END
    ye = int(t); ys_ = context.user_data.get('pc_rs')
    sku = context.user_data.get('pc_sku'); metric = context.user_data.get('pc_metric')
    if not ys_ or not sku or not metric:
        await update.message.reply_text("❌ Начните заново."); return ConversationHandler.END
    if ye < ys_: await update.message.reply_text("❌ Конец < начала."); return WAITING_PC_RANGE_END
    years = list(range(ys_, ye + 1))
    if len(years) > 10: await update.message.reply_text("⚠️ Максимум 10 лет."); return ConversationHandler.END
    msg = await update.message.reply_text("⏳ Строю график...")
    try:
        buf = await generate_product_chart(sku, metric, years)
        nm = context.user_data.get('pc_name', sku)
        await msg.delete()
        await update.message.reply_photo(photo=buf, caption=f"{nm} | {METRIC_LABELS.get(metric, metric)} | {ys_}-{ye}")
        await update.message.reply_text("Выберите действие:", reply_markup=products_reports_kb())
    except Exception as e:
        write_log(f"❌ {e}"); await msg.edit_text(f"❌ {e}")
    context.user_data.pop('pc_rs', None)
    return ConversationHandler.END

# ==================== РАССЫЛКИ ====================
async def auto_schedule_start(update, context):
    chat_id = update.effective_chat.id
    if not has_access(chat_id): return
    settings = await load_settings()
    us = get_user_settings(settings, chat_id)
    current = us.get("schedule_hours", [])
    context.user_data['temp_sch'] = list(current)
    if current:
        txt = f"Текущие часы: {', '.join(f'{h:02d}:00' for h in sorted(current))}"
    else:
        txt = "Часы рассылок не настроены."
    await update.message.reply_text(txt, reply_markup=hours_kb(current, "sch_"))

async def auto_yesterday_start(update, context):
    chat_id = update.effective_chat.id
    if not has_access(chat_id): return
    settings = await load_settings()
    us = get_user_settings(settings, chat_id)
    sch = us.get("schedule_hours", [])
    if not sch: await update.message.reply_text("❌ Сначала настройте «Выбор времени рассылок»."); return
    cur = us.get("yesterday_report_hours", [])
    context.user_data['temp_yest'] = list(cur)
    await update.message.reply_text("Выберите часы отправки отчёта за Вчера:",
        reply_markup=hours_kb(cur, "yest_", available=sch))

async def auto_silence_start(update, context):
    chat_id = update.effective_chat.id
    if not has_access(chat_id): return
    settings = await load_settings()
    us = get_user_settings(settings, chat_id)
    s = us.get("silence_start"); e = us.get("silence_end")
    if s is not None and e is not None:
        txt = f"Режим тишины: с {s:02d}:00 до {e:02d}:00"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить", callback_data="sil_edit")],
            [InlineKeyboardButton("❌ Отключить", callback_data="sil_disable")],
            [InlineKeyboardButton("🔙 Назад", callback_data="sil_back")]])
    else:
        txt = "Режим тишины не настроен."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔕 Установить", callback_data="sil_edit")],
            [InlineKeyboardButton("🔙 Назад", callback_data="sil_back")]])
    await update.message.reply_text(txt, reply_markup=kb)

async def auto_send_now(update, context):
    chat_id = update.effective_chat.id
    if not has_access(chat_id): return
    await update.message.reply_text("⏳ Формирую отчёт...")
    try:
        rpt = await build_today_report(include_yesterday=True)
        await context.bot.send_message(chat_id=chat_id, text=rpt, parse_mode="Markdown")
        await update.message.reply_text("✅ Отправлено.")
    except Exception as e:
        write_log(f"❌ {e}"); await update.message.reply_text(f"❌ {e}")

async def auto_menu_router(update, context):
    t = update.message.text
    if t == "🕒 Выбор времени рассылок": await auto_schedule_start(update, context)
    elif t == "📅 Добавление отчета за Вчера": await auto_yesterday_start(update, context)
    elif t == "🔕 Режим тишины": await auto_silence_start(update, context)
    elif t == "📤 Отправить отчет за Вчера сейчас": await auto_send_now(update, context)
    elif t == "🔙 Назад":
        await update.message.reply_text("Главное меню:", reply_markup=main_kb(update.effective_chat.id))

async def schedule_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    chat_id = q.message.chat_id
    if d == "sch_back":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=auto_kb())
        return ConversationHandler.END
    if d.startswith("sch_") and d[4:].isdigit():
        h = int(d[4:])
        temp = context.user_data.get('temp_sch', [])
        if h in temp: temp.remove(h)
        else: temp.append(h)
        context.user_data['temp_sch'] = temp
        await q.edit_message_reply_markup(reply_markup=hours_kb(temp, "sch_"))
        return WAITING_AUTO_SCHEDULE
    if d == "sch_save":
        temp = context.user_data.get('temp_sch', [])
        settings = await load_settings()
        us = get_user_settings(settings, chat_id)
        us["schedule_hours"] = temp
        us["yesterday_report_hours"] = [h for h in us.get("yesterday_report_hours", []) if h in temp]
        settings[str(chat_id)] = us
        await save_settings(settings)
        txt = ', '.join(f'{h:02d}:00' for h in sorted(temp)) if temp else 'не выбраны'
        await q.edit_message_text(f"✅ Сохранено. Часы: {txt}")
        await q.message.reply_text("Выберите действие:", reply_markup=auto_kb())
        return ConversationHandler.END
    return WAITING_AUTO_SCHEDULE

async def yesterday_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    chat_id = q.message.chat_id
    if d == "yest_back":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=auto_kb())
        return ConversationHandler.END
    if d.startswith("yest_") and d[5:].isdigit():
        h = int(d[5:])
        temp = context.user_data.get('temp_yest', [])
        if h in temp: temp.remove(h)
        else: temp.append(h)
        context.user_data['temp_yest'] = temp
        settings = await load_settings()
        us = get_user_settings(settings, chat_id)
        sch = us.get("schedule_hours", [])
        await q.edit_message_reply_markup(reply_markup=hours_kb(temp, "yest_", available=sch))
        return WAITING_AUTO_YESTERDAY
    if d == "yest_save":
        temp = context.user_data.get('temp_yest', [])
        settings = await load_settings()
        us = get_user_settings(settings, chat_id)
        us["yesterday_report_hours"] = temp
        settings[str(chat_id)] = us
        await save_settings(settings)
        txt = ', '.join(f'{h:02d}:00' for h in sorted(temp)) if temp else 'не выбраны'
        await q.edit_message_text(f"✅ Сохранено. Часы: {txt}")
        await q.message.reply_text("Выберите действие:", reply_markup=auto_kb())
        return ConversationHandler.END
    return WAITING_AUTO_YESTERDAY

async def silence_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    chat_id = q.message.chat_id
    if d == "sil_back":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=auto_kb())
        return ConversationHandler.END
    if d == "sil_disable":
        settings = await load_settings()
        us = get_user_settings(settings, chat_id)
        us["silence_start"] = None; us["silence_end"] = None
        settings[str(chat_id)] = us
        await save_settings(settings)
        await q.edit_message_text("✅ Отключено.")
        await q.message.reply_text("Выберите действие:", reply_markup=auto_kb())
        return ConversationHandler.END
    if d == "sil_edit":
        await q.edit_message_text("Выберите час начала:",
                                  reply_markup=hours_kb_silence("sil_s_"))
        return WAITING_AUTO_SILENCE_START
    return ConversationHandler.END

async def silence_start_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "sil_s_back":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=auto_kb())
        return ConversationHandler.END
    if d.startswith("sil_s_") and d[6:].isdigit():
        h = int(d[6:])
        context.user_data['temp_sil_s'] = h
        await q.edit_message_text(f"Начало: {h:02d}:00\nКонец:",
                                  reply_markup=hours_kb_silence("sil_e_"))
        return WAITING_AUTO_SILENCE_END
    return WAITING_AUTO_SILENCE_START

async def silence_end_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    chat_id = q.message.chat_id
    if d == "sil_e_back":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=auto_kb())
        return ConversationHandler.END
    if d.startswith("sil_e_") and d[6:].isdigit():
        h = int(d[6:])
        s = context.user_data.get('temp_sil_s')
        if s is None: await q.edit_message_text("❌ Ошибка."); return ConversationHandler.END
        settings = await load_settings()
        us = get_user_settings(settings, chat_id)
        us["silence_start"] = s; us["silence_end"] = h
        settings[str(chat_id)] = us
        await save_settings(settings)
        await q.edit_message_text(f"✅ Тишина: {s:02d}:00 – {h:02d}:00")
        await q.message.reply_text("Выберите действие:", reply_markup=auto_kb())
        context.user_data.pop('temp_sil_s', None)
        return ConversationHandler.END
    return WAITING_AUTO_SILENCE_END

# ==================== АДМИНИСТРИРОВАНИЕ ====================
async def admin_add_start(update, context):
    if not is_admin(update.effective_chat.id): return
    await update.message.reply_text("Введите ID (число) или @username:")
    return WAITING_ADD_MANAGER

async def admin_add_input(update, context):
    if not is_admin(update.effective_chat.id): return ConversationHandler.END
    t = update.message.text.strip()
    if not t: await update.message.reply_text("❌ Введите ID."); return WAITING_ADD_MANAGER
    if t.isdigit():
        uid = int(t)
        try:
            u = await context.bot.get_chat(uid)
            un = u.username or ""; fn = u.first_name or ""; ln = u.last_name or ""
        except Exception:
            await update.message.reply_text(f"❌ Не найден ID {uid}."); return WAITING_ADD_MANAGER
    else:
        un = t.lstrip('@')
        try:
            u = await context.bot.get_chat(un)
            uid = u.id; fn = u.first_name or ""; ln = u.last_name or ""
        except Exception:
            await update.message.reply_text(f"❌ Не найден @{un}."); return WAITING_ADD_MANAGER
    if uid == ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Админ уже имеет доступ."); return WAITING_ADD_MANAGER
    context.user_data['new_m'] = {"id": uid, "username": un, "first_name": fn, "last_name": ln}
    await update.message.reply_text("Введите телефон (или '-' пропустить):")
    return WAITING_MANAGER_PHONE

async def admin_add_phone(update, context):
    if not is_admin(update.effective_chat.id): return ConversationHandler.END
    ph = update.message.text.strip()
    if ph == "-": ph = ""
    d = context.user_data.get('new_m')
    if not d: await update.message.reply_text("❌ Потеряны данные."); return ConversationHandler.END
    if add_manager(d["id"], d["username"], d["first_name"], d["last_name"], ph):
        await update.message.reply_text(f"✅ Менеджер {d['id']} добавлен.")
    else:
        await update.message.reply_text(f"⚠️ Менеджер {d['id']} уже есть.")
    context.user_data.pop('new_m', None)
    await update.message.reply_text("Управление:", reply_markup=admin_kb())
    return ConversationHandler.END

async def admin_remove_start(update, context):
    if not is_admin(update.effective_chat.id): return
    await update.message.reply_text("Введите ID менеджера:")
    return WAITING_REMOVE_MANAGER

async def admin_remove_input(update, context):
    if not is_admin(update.effective_chat.id): return ConversationHandler.END
    try: uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Введите число."); return WAITING_REMOVE_MANAGER
    if uid == ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Админа нельзя удалить."); return WAITING_REMOVE_MANAGER
    if remove_manager(uid): await update.message.reply_text(f"✅ Удалён {uid}.")
    else: await update.message.reply_text(f"❌ Не найден {uid}.")
    await update.message.reply_text("Управление:", reply_markup=admin_kb())
    return ConversationHandler.END

async def admin_list(update, context):
    if not is_admin(update.effective_chat.id): return
    ms = load_managers()
    if not ms: await update.message.reply_text("Список пуст."); return
    lines = ["📋 Список:"]
    for m in ms:
        info = f"ID: {m.get('id')}"
        if m.get('username'): info += f", @{m['username']}"
        if m.get('first_name'): info += f", {m['first_name']}"
        if m.get('phone'): info += f", 📞 {m['phone']}"
        lines.append(info)
    await update.message.reply_text("\n".join(lines))

# ==================== ПЛАНИРОВЩИК ====================
async def check_auto_reports(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.datetime.now(MOSCOW_TZ)
    cur_hour = now.hour; cur_date = now.strftime("%Y-%m-%d")
    settings = await load_settings()
    if cur_hour == 12:
        recipients = [ADMIN_CHAT_ID] + [m["id"] for m in load_managers()]
        for uid in set(recipients):
            try:
                us = get_user_settings(settings, uid)
                if us.get("schedule_hours"): continue
                if us.get("last_reminder") == cur_date: continue
                await context.bot.send_message(chat_id=uid,
                    text="⚠️ *Напоминание:* у вас не настроены рассылки.",
                    parse_mode="Markdown")
                us["last_reminder"] = cur_date
                settings[str(uid)] = us
            except Exception as e: write_log(f"⚠️ {e}")
        await save_settings(settings)
    for sid, us in settings.items():
        try: uid = int(sid)
        except: continue
        sch = us.get("schedule_hours", [])
        if not sch or cur_hour not in sch: continue
        s = us.get("silence_start"); e = us.get("silence_end")
        if s is not None and e is not None:
            if s < e:
                if s <= cur_hour < e: continue
            else:
                if cur_hour >= s or cur_hour < e: continue
        yest_hours = us.get("yesterday_report_hours", [])
        if cur_hour in yest_hours:
            last = us.get("last_sent_yesterday")
            if last:
                try:
                    ld, lh = last.split()
                    if ld == cur_date and int(lh) == cur_hour: continue
                except: pass
            try:
                rpt = await build_today_report(include_yesterday=True)
                await context.bot.send_message(chat_id=uid, text=rpt, parse_mode="Markdown")
                us["last_sent_yesterday"] = f"{cur_date} {cur_hour}"
                settings[sid] = us
                await save_settings(settings)
            except Exception as e: write_log(f"❌ {e}")
        else:
            last = us.get("last_sent_regular")
            if last:
                try:
                    ld, lh = last.split()
                    if ld == cur_date and int(lh) == cur_hour: continue
                except: pass
            try:
                rpt = await build_today_report(include_yesterday=False)
                await context.bot.send_message(chat_id=uid, text=rpt, parse_mode="Markdown")
                us["last_sent_regular"] = f"{cur_date} {cur_hour}"
                settings[sid] = us
                await save_settings(settings)
            except Exception as e: write_log(f"❌ {e}")

async def cancel(update, context):
    chat_id = update.effective_chat.id
    await update.message.reply_text("Отменено.", reply_markup=main_kb(chat_id))
    return ConversationHandler.END

# ==================== ЗАПУСК ====================
def main():
    if not validate_env_vars(): sys.exit(1)
    _ensure_data_dir()
    _init_expense_types_file()
    write_log(f"🚀 Запуск (v{VERSION})")
    write_log(f"✅ OZON_CLIENT_ID: {mask_secret(OZON_CLIENT_ID)}")
    write_log(f"✅ ADMIN_CHAT_ID: {ADMIN_CHAT_ID}")
    update_version_history(VERSION, CHANGELOG_MESSAGE)
    app = (Application.builder()
           .token(TELEGRAM_BOT_TOKEN)
           .connect_timeout(30.0).read_timeout(30.0).write_timeout(30.0)
           .post_init(init_http_session).post_shutdown(close_http_session).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("version", version_command))
    app.add_handler(CommandHandler("help", send_help))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.Regex(
        "^(📊 Отчёт по продажам|📦 Отчёт по товарам|📈 Отчёт по рекламе|"
        "🔔 Автоматические рассылки|⚙️ Администрирование|"
        "📚 Справочник расходов|📖 Справка)$"), handle_main_menu))
    app.add_handler(MessageHandler(filters.Text(["🔄 Проверить доступ"]), check_access_cmd))
    app.add_handler(MessageHandler(filters.Text(["📅 Продажи за сегодня"]), today_report))
    app.add_handler(MessageHandler(filters.Text(["📅 Топ товаров за сегодня"]), top_today))
    app.add_handler(MessageHandler(filters.Text(["📋 Список менеджеров"]), admin_list))
    app.add_handler(MessageHandler(filters.Text(["📅 Реклама за текущий месяц"]), ad_report_month_handler))
    app.add_handler(MessageHandler(filters.Text(["📅 Реклама за период"]), ad_period_menu))
    app.add_handler(MessageHandler(filters.Text(["📈 Динамика по рекламе"]), ad_dynamics_menu))
    app.add_handler(MessageHandler(filters.Regex(
        "^(🕒 Выбор времени рассылок|📅 Добавление отчета за Вчера|"
        "🔕 Режим тишины|📤 Отправить отчет за Вчера сейчас|🔙 Назад)$"), auto_menu_router))
    app.add_handler(CallbackQueryHandler(expense_types_cb, pattern="^et_"))
    # Conversation handlers
    conv_date = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📆 Выбрать дату"), date_menu)],
        states={WAITING_DATE_SINGLE: [CallbackQueryHandler(date_cb)]},
        fallbacks=[CommandHandler("cancel", cancel)])
    conv_period = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📊 Выбрать период"), period_menu)],
        states={
            WAITING_PERIOD_TYPE: [CallbackQueryHandler(period_type_cb)],
            WAITING_PERIOD_YEAR: [CallbackQueryHandler(period_year_cb)],
            WAITING_PERIOD_MONTH: [CallbackQueryHandler(period_month_cb)],
            WAITING_PERIOD_QUARTER: [CallbackQueryHandler(period_quarter_cb)],
            WAITING_PERIOD_START: [CallbackQueryHandler(custom_start_cb)],
            WAITING_PERIOD_END: [CallbackQueryHandler(custom_end_cb)],
            WAITING_YEAR_SELECT: [CallbackQueryHandler(period_year_cb)]},
        fallbacks=[CommandHandler("cancel", cancel)])
    conv_prod = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("🏆 Товары за период"), products_period_menu)],
        states={
            WAITING_PRODUCT_PERIOD_TYPE: [CallbackQueryHandler(products_period_type_cb)],
            WAITING_PRODUCT_YEAR: [CallbackQueryHandler(products_year_cb)],
            WAITING_PRODUCT_MONTH: [CallbackQueryHandler(products_month_cb)],
            WAITING_PRODUCT_QUARTER: [CallbackQueryHandler(products_quarter_cb)],
            WAITING_PRODUCT_PERIOD_START: [CallbackQueryHandler(products_custom_start)],
            WAITING_PRODUCT_PERIOD_END: [CallbackQueryHandler(products_custom_end)],
            WAITING_PRODUCT_YEAR_SELECT: [CallbackQueryHandler(products_year_cb)]},
        fallbacks=[CommandHandler("cancel", cancel)])
    conv_dyn = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📈 Динамика продаж"), dynamics_menu)],
        states={
            WAITING_DYN_SELECT: [CallbackQueryHandler(dynamics_select_cb)],
            WAITING_DYN_YEAR: [CallbackQueryHandler(dynamics_year_cb)],
            WAITING_DYN_RANGE_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, dynamics_range_start)],
            WAITING_DYN_RANGE_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, dynamics_range_end)]},
        fallbacks=[CommandHandler("cancel", cancel)])
    conv_pchart = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📈 Динамика по товару"), product_chart_menu)],
        states={
            WAITING_PC_LIST: [CallbackQueryHandler(product_chart_list_cb)],
            WAITING_PC_METRIC: [CallbackQueryHandler(product_chart_metric_cb)],
            WAITING_PC_PERIOD: [CallbackQueryHandler(product_chart_period_cb)],
            WAITING_PC_YEAR: [CallbackQueryHandler(product_chart_year_cb)],
            WAITING_PC_RANGE_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_chart_range_start)],
            WAITING_PC_RANGE_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_chart_range_end)]},
        fallbacks=[CommandHandler("cancel", cancel)])
    # Ad report period
    conv_ad_period = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📅 Реклама за период"), ad_period_menu)],
        states={
            WAITING_AD_REPORT_PERIOD_TYPE: [CallbackQueryHandler(ad_period_type_cb)],
            WAITING_AD_REPORT_PERIOD_YEAR: [CallbackQueryHandler(ad_period_year_cb)],
            WAITING_AD_REPORT_PERIOD_MONTH: [CallbackQueryHandler(ad_period_month_cb)],
            WAITING_AD_REPORT_PERIOD_QUARTER: [CallbackQueryHandler(ad_period_quarter_cb)],
            WAITING_AD_REPORT_PERIOD_START: [CallbackQueryHandler(ad_period_custom_start_cb)],
            WAITING_AD_REPORT_PERIOD_END: [CallbackQueryHandler(ad_period_custom_end_cb)],
        },
        fallbacks=[CommandHandler("cancel", cancel)])
    # Ad dynamics
    conv_ad_dyn = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📈 Динамика по рекламе"), ad_dynamics_menu)],
        states={
            WAITING_AD_DYN_PERIOD_TYPE: [CallbackQueryHandler(ad_dynamics_period_cb)],
            WAITING_AD_DYN_YEAR: [CallbackQueryHandler(ad_dynamics_year_cb)],
            WAITING_AD_DYN_RANGE_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, ad_dynamics_range_start)],
            WAITING_AD_DYN_RANGE_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, ad_dynamics_range_end)],
            WAITING_AD_DYN_CAMPAIGN_SELECT: [CallbackQueryHandler(ad_dynamics_campaign_cb)],
            WAITING_AD_DYN_METRIC: [CallbackQueryHandler(ad_dynamics_metric_cb)],
        },
        fallbacks=[CommandHandler("cancel", cancel)])
    conv_sch = ConversationHandler(
        entry_points=[CallbackQueryHandler(schedule_cb, pattern="^sch_")],
        states={WAITING_AUTO_SCHEDULE: [CallbackQueryHandler(schedule_cb, pattern="^sch_")]},
        fallbacks=[CommandHandler("cancel", cancel)])
    conv_yest = ConversationHandler(
        entry_points=[CallbackQueryHandler(yesterday_cb, pattern="^yest_")],
        states={WAITING_AUTO_YESTERDAY: [CallbackQueryHandler(yesterday_cb, pattern="^yest_")]},
        fallbacks=[CommandHandler("cancel", cancel)])
    conv_sil = ConversationHandler(
        entry_points=[CallbackQueryHandler(silence_cb, pattern="^sil_")],
        states={
            WAITING_AUTO_SILENCE_START: [CallbackQueryHandler(silence_start_cb, pattern="^sil_s_")],
            WAITING_AUTO_SILENCE_END: [CallbackQueryHandler(silence_end_cb, pattern="^sil_e_")]},
        fallbacks=[CommandHandler("cancel", cancel)])
    conv_add = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("➕ Добавить менеджера"), admin_add_start)],
        states={
            WAITING_ADD_MANAGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_input)],
            WAITING_MANAGER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_phone)]},
        fallbacks=[CommandHandler("cancel", cancel)])
    conv_rm = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("➖ Удалить менеджера"), admin_remove_start)],
        states={WAITING_REMOVE_MANAGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_remove_input)]},
        fallbacks=[CommandHandler("cancel", cancel)])
    app.add_handler(conv_date); app.add_handler(conv_period); app.add_handler(conv_prod)
    app.add_handler(conv_dyn); app.add_handler(conv_pchart)
    app.add_handler(conv_ad_period); app.add_handler(conv_ad_dyn)
    app.add_handler(conv_sch); app.add_handler(conv_yest); app.add_handler(conv_sil)
    app.add_handler(conv_add); app.add_handler(conv_rm)
    if app.job_queue:
        app.job_queue.run_repeating(check_auto_reports, interval=900, first=10)
    write_log("🚀 Бот готов.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, timeout=30)

if __name__ == "__main__":
    main()
