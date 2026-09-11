import datetime
import json
import os
import time
import re
import calendar as cal_mod
import asyncio
import aiohttp
import warnings
import sys
from typing import Optional, List, Dict
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

# ==================== ВЕРСИЯ ====================
VERSION = "2.4.1"
CHANGELOG_MESSAGE = "Возвращены произвольные даты и периоды (месяц/квартал/год/custom) с сравнением с предыдущим периодом."

# ==================== КОНСТАНТЫ ====================
API_TIMEOUT = 60
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY = 10
POSTINGS_RATE = 0.5
FINANCE_RATE = 0.5
PERFORMANCE_RATE = 1.0
CACHE_TTL_SECONDS = 300
VERSION_HISTORY_FILE = "version_history.json"
LOG_FILE = "/app/data/ozon_log.txt"

# Состояния диалогов
WAITING_DATE_SINGLE = 1
WAITING_PERIOD_TYPE = 2
WAITING_PERIOD_START = 3
WAITING_PERIOD_END = 4
WAITING_PERIOD_YEAR = 5
WAITING_PERIOD_MONTH = 6
WAITING_PERIOD_QUARTER = 7
WAITING_YEAR_SELECT = 8

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

# ==================== КОНФИГ ====================
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

# ==================== СПРАВОЧНИКИ ====================
TYPE_ID_NAMES = {
    1: "Обработка товара",
    29: "Последняя миля",
    32: "Логистика",
    98: "Доп. услуги отправления",
    12: "Прочие услуги Ozon",
    76: "Внешние услуги Ozon",
}
CATEGORY_FALLBACK = {
    "ITEM": "Услуги по товарам",
    "NON_ITEM": "Внешние услуги Ozon",
    "POSTING": "Услуги отправления",
    "CONTAINER": "Контейнеры",
}

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
    if not value or len(value) <= visible:
        return "***"
    return f"{value[:visible]}***"

def validate_env_vars() -> bool:
    req = {"OZON_CLIENT_ID": OZON_CLIENT_ID, "OZON_API_KEY": OZON_API_KEY,
           "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN, "ADMIN_CHAT_ID": ADMIN_CHAT_ID_STR}
    missing = [k for k, v in req.items() if not v]
    if missing:
        write_log(f"❌ Missing env vars: {', '.join(missing)}")
        return False
    if not ADMIN_CHAT_ID_STR.isdigit():
        write_log("❌ ADMIN_CHAT_ID должен быть числом")
        return False
    return True

def update_version_history(version, message):
    history = []
    if os.path.exists(VERSION_HISTORY_FILE):
        try:
            with open(VERSION_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except:
            history = []
    for e in history:
        if e.get("version") == version:
            write_log(f"ℹ️ Версия {version} уже в истории.")
            return
    now = datetime.datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S")
    history.append({"version": version, "date": now, "message": message})
    try:
        with open(VERSION_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        write_log(f"✅ История: {version}")
    except Exception as e:
        write_log(f"❌ Ошибка истории: {e}")

# ==================== УТИЛИТЫ ====================
def get_moscow_today():
    return datetime.datetime.now(MOSCOW_TZ).date()

def get_current_time_msk():
    return datetime.datetime.now(MOSCOW_TZ)

def is_admin(chat_id):
    return chat_id == ADMIN_CHAT_ID

def fmt_num(val):
    return f"{val:,.2f}".replace(",", " ") if val else "0.00"

def fmt_int(val):
    return str(val) if val else "0"

def parse_money(obj) -> float:
    if obj is None:
        return 0.0
    if isinstance(obj, dict):
        val = obj.get("amount", obj.get("value", 0))
    else:
        val = obj
    try:
        return float(str(val).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0

def parse_price(product) -> float:
    return parse_money(product.get("price"))

def type_name(type_id, category_code):
    if type_id in TYPE_ID_NAMES:
        return TYPE_ID_NAMES[type_id]
    fallback = CATEGORY_FALLBACK.get(category_code, "Услуги")
    return f"{fallback} (type {type_id})" if type_id else fallback

def calc_delta(current, previous):
    if previous == 0:
        return None
    try:
        return ((current - previous) / abs(previous)) * 100
    except:
        return None

def fmt_pct(val):
    if val is None:
        return "∞"
    return f"+{val:.1f}%" if val > 0 else f"{val:.1f}%"

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
        return False, "❌ Неверный формат даты"

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

# ==================== КЭШ ====================
async def get_from_cache(key):
    async with _cache_lock:
        if key in _api_cache:
            ts = _cache_timestamps.get(key)
            if ts and (time.time() - ts) < CACHE_TTL_SECONDS:
                return _api_cache[key]
    return None

async def save_to_cache(key, value):
    async with _cache_lock:
        _api_cache[key] = value
        _cache_timestamps[key] = time.time()

# ==================== RATE LIMITER ====================
class RateLimiter:
    def __init__(self, rate):
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

# ==================== ИНИЦИАЛИЗАЦИЯ ====================
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
        write_log("🔒 HTTP закрыта.")

# ==================== API ====================
async def api_request_with_retry(url, headers, payload=None, method='POST', kind='finance'):
    global _http_session
    if kind == 'perf':
        sem, rate = _perf_sem, _perf_rate
    elif kind == 'postings':
        sem, rate = _postings_sem, _postings_rate
    else:
        sem, rate = _finance_sem, _finance_rate

    async with sem:
        for attempt in range(API_RETRY_ATTEMPTS):
            try:
                await rate.acquire()
                if method == 'POST':
                    async with _http_session.post(url, headers=headers, json=payload) as resp:
                        body = await resp.text()
                        if resp.status == 429:
                            wait_time = API_RETRY_DELAY * (2 ** attempt)
                            write_log(f"⚠️ 429, ждём {wait_time}с")
                            await asyncio.sleep(wait_time)
                            continue
                        if resp.status >= 400:
                            write_log(f"❌ API {resp.status} {url}: {body[:500]}")
                            raise aiohttp.ClientResponseError(
                                resp.request_info, resp.history,
                                status=resp.status, message=body[:200])
                        return json.loads(body)
                else:
                    async with _http_session.get(url, headers=headers, params=payload) as resp:
                        body = await resp.text()
                        if resp.status == 429:
                            wait_time = API_RETRY_DELAY * (2 ** attempt)
                            write_log(f"⚠️ 429, ждём {wait_time}с")
                            await asyncio.sleep(wait_time)
                            continue
                        if resp.status >= 400:
                            write_log(f"❌ API {resp.status} {url}: {body[:500]}")
                            raise aiohttp.ClientResponseError(
                                resp.request_info, resp.history,
                                status=resp.status, message=body[:200])
                        return json.loads(body)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == API_RETRY_ATTEMPTS - 1:
                    write_log(f"❌ API failed after {API_RETRY_ATTEMPTS}: {e}")
                    raise
                write_log(f"⚠️ Attempt {attempt+1}/{API_RETRY_ATTEMPTS}: {e}")
                await asyncio.sleep(API_RETRY_DELAY * (attempt + 1))
        raise Exception("API failed")

# ==================== TOKEN ====================
async def get_performance_token():
    if not OZON_PERFORMANCE_CLIENT_ID or not OZON_PERFORMANCE_CLIENT_SECRET:
        return None
    url = "https://api-performance.ozon.ru/api/client/token"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    payload = {"client_id": OZON_PERFORMANCE_CLIENT_ID,
               "client_secret": OZON_PERFORMANCE_CLIENT_SECRET,
               "grant_type": "client_credentials"}
    try:
        data = await api_request_with_retry(url, headers, payload, method='POST', kind='perf')
        return data.get("access_token")
    except Exception as e:
        write_log(f"❌ Ошибка токена: {e}")
        return None

# ==================== ОТГРУЗКИ FBO v3 ====================
async def fetch_postings(date_from, date_to):
    cache_key = f"postings_{date_from}_{date_to}"
    cached = await get_from_cache(cache_key)
    if cached is not None:
        return cached

    headers = {"Client-Id": OZON_CLIENT_ID, "Api-Key": OZON_API_KEY, "Content-Type": "application/json"}
    since_iso = f"{date_from}T00:00:00Z"
    to_iso = f"{date_to}T23:59:59Z"
    all_postings = []
    cursor = ""
    LIMIT = 100
    while True:
        payload = {
            "dir": "ASC",
            "filter": {"since": since_iso, "to": to_iso},
            "limit": LIMIT,
            "translit": False,
            "with": {"analytics_data": True, "financial_data": True}
        }
        if cursor:
            payload["cursor"] = cursor
        try:
            data = await api_request_with_retry(
                OZON_POSTING_FBO_URL, headers, payload, method='POST', kind='postings')
        except Exception as e:
            write_log(f"❌ Ошибка FBO: {e}")
            break

        postings = data.get("postings", [])
        if not postings:
            break
        all_postings.extend(postings)

        has_next = data.get("has_next", False)
        cursor = data.get("cursor", "")
        if not has_next or not cursor:
            break
        if len(postings) < LIMIT:
            break

    write_log(f"📦 Загружено отгрузок: {len(all_postings)} за {date_from}–{date_to}")
    await save_to_cache(cache_key, all_postings)
    return all_postings

# ==================== РЕКЛАМА ====================
async def fetch_advertising_expense(date_from, date_to):
    cache_key = f"ad_{date_from}_{date_to}"
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
        data = await api_request_with_retry(url, headers, params, method='GET', kind='perf')
        total = 0.0
        if isinstance(data, dict) and "rows" in data:
            for item in data["rows"]:
                item_date = item.get("date", "")[:10]
                if date_from <= item_date <= date_to:
                    ms = item.get("moneySpent")
                    if ms is not None:
                        try:
                            total += float(str(ms).replace(",", "."))
                        except:
                            pass
        await save_to_cache(cache_key, total)
        return total
    except Exception as e:
        write_log(f"❌ Реклама: {e}")
        return 0.0

# ==================== ФИНАНСЫ ====================
async def fetch_finance_accruals_by_day(date_str: str) -> List[Dict]:
    headers = {"Client-Id": OZON_CLIENT_ID, "Api-Key": OZON_API_KEY, "Content-Type": "application/json"}
    payload = {"date": date_str}
    all_accruals = []
    while True:
        try:
            data = await api_request_with_retry(
                OZON_FINANCE_ACCRUAL_BY_DAY_URL, headers, payload, method='POST', kind='finance')
        except Exception as e:
            write_log(f"❌ Финансы {date_str}: {e}")
            break
        accruals = data.get("accruals", [])
        if not accruals:
            break
        all_accruals.extend(accruals)
        last_id = data.get("last_id")
        if last_id:
            payload["last_id"] = last_id
        else:
            break
    return all_accruals

async def fetch_finance_transactions(date_from, date_to):
    cache_key = f"fin_{date_from}_{date_to}"
    cached = await get_from_cache(cache_key)
    if cached is not None:
        return cached
    start_dt = datetime.datetime.strptime(date_from, "%Y-%m-%d").date()
    end_dt = datetime.datetime.strptime(date_to, "%Y-%m-%d").date()
    today = get_moscow_today()
    if start_dt > today:
        return []
    if end_dt > today:
        end_dt = today

    days = []
    current = start_dt
    while current <= end_dt:
        days.append(current.isoformat())
        current += datetime.timedelta(days=1)

    write_log(f"💰 Финансы: {len(days)} дней параллельно ({date_from}–{date_to})")

    tasks = [fetch_finance_accruals_by_day(d) for d in days]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_accruals = []
    for i, res in enumerate(results):
        if isinstance(res, list):
            all_accruals.extend(res)
        elif isinstance(res, Exception):
            write_log(f"⚠️ Ошибка в дне {days[i]}: {res}")

    write_log(f"💰 Всего начислений: {len(all_accruals)}")
    await save_to_cache(cache_key, all_accruals)
    return all_accruals

# ==================== АГРЕГАЦИЯ ====================
def aggregate_finance_expenses(accruals: List[Dict]) -> Dict[str, float]:
    result: Dict[str, float] = {}

    def add(name: str, amount: float):
        if amount > 0:
            result[name] = result.get(name, 0.0) + amount

    for item in accruals:
        if not isinstance(item, dict):
            continue
        cat = item.get("accrued_category", "")

        if cat == "POSTING":
            posting = item.get("posting") or {}
            for product in posting.get("products", []) or []:
                comm = product.get("commission") or {}
                sc = parse_money(comm.get("sale_commission"))
                if sc < 0:
                    add("Комиссия Ozon", abs(sc))
                delivery = product.get("delivery") or {}
                for svc in delivery.get("services", []) or []:
                    tid = svc.get("type_id")
                    amt = parse_money(svc.get("accrued"))
                    if amt < 0:
                        add(type_name(tid, cat), abs(amt))
            continue

        if cat == "ITEM":
            item_fees = item.get("item_fees") or {}
            for fee_group in item_fees.get("fees", []) or []:
                for fee in fee_group.get("fees", []) or []:
                    tid = fee.get("type_id")
                    amt = parse_money(fee.get("accrued"))
                    if amt < 0:
                        add(type_name(tid, cat), abs(amt))
            continue

        if cat == "NON_ITEM":
            non = item.get("non_item_fee") or {}
            tid = non.get("type_id")
            amt = parse_money(non.get("accrued"))
            if amt < 0:
                add(type_name(tid, cat), abs(amt))
            continue

        if cat == "CONTAINER":
            cont = item.get("container_fees") or {}
            amt = parse_money(cont.get("accrued", cont))
            if amt < 0:
                add("Контейнеры", abs(amt))
            continue

        total = parse_money(item.get("total_amount"))
        if total < 0:
            add(cat or "Прочее", abs(total))

    return result

def aggregate_postings_range(postings, date_from, date_to):
    """Агрегирует отгрузки за указанный диапазон дат."""
    result = {"ordered_units": 0, "ordered_sum": 0.0,
              "delivered_units": 0, "delivered_sum": 0.0,
              "canceled_units": 0, "canceled_sum": 0.0}
    for posting in postings:
        if not isinstance(posting, dict):
            continue
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

        total_units = 0
        total_sum = 0.0
        for product in posting.get("products", []):
            if not isinstance(product, dict):
                continue
            qty = int(product.get("quantity", 0))
            price = parse_price(product)
            total_units += qty
            total_sum += price * qty

        status = posting.get("status", "")
        result["ordered_units"] += total_units
        result["ordered_sum"] += total_sum
        if status in ("cancelled", "canceled"):
            result["canceled_units"] += total_units
            result["canceled_sum"] += total_sum
        elif status in ("delivered", "completed"):
            result["delivered_units"] += total_units
            result["delivered_sum"] += total_sum
    return result

# ==================== ЗАГРУЗКА МЕТРИК ЗА ПЕРИОД ====================
async def get_period_metrics(date_from, date_to):
    """Параллельно грузит все данные за период и возвращает метрики."""
    postings = await fetch_postings(date_from, date_to)
    ad = await fetch_advertising_expense(date_from, date_to)
    fin = await fetch_finance_transactions(date_from, date_to)

    agg = aggregate_postings_range(postings, date_from, date_to)
    expenses = aggregate_finance_expenses(fin)

    ordered_sum = agg["ordered_sum"]
    delivered_sum = agg["delivered_sum"]
    drr = (ad / ordered_sum * 100) if ordered_sum > 0 else None
    eff_drr = (ad / delivered_sum * 100) if delivered_sum > 0 else None

    return {
        "ordered_sum": ordered_sum,
        "ordered_units": agg["ordered_units"],
        "delivered_sum": delivered_sum,
        "delivered_units": agg["delivered_units"],
        "canceled_sum": agg["canceled_sum"],
        "canceled_units": agg["canceled_units"],
        "ad_expense": ad,
        "drr": drr,
        "effective_drr": eff_drr,
        "expenses": expenses,
    }

# ==================== ФОРМАТИРОВАНИЕ ====================
def format_expense_block(expenses_by_type, title, limit=20):
    if not expenses_by_type:
        return f"🔹 *{title}*\nНет данных о расходах.\n"
    total = sum(expenses_by_type.values())
    lines = [f"🔹 *{title}*", f"  *Итого:* {total:,.2f} ₽"]
    items = sorted(expenses_by_type.items(), key=lambda x: x[1], reverse=True)[:limit]
    for category, amount in items:
        lines.append(f"    {category}: {amount:,.2f} ₽")
    return "\n".join(lines)

def format_period_comparison(cur, prev, period_name):
    """Сравнение текущего и предыдущего периода."""
    lines = [f"📊 *Продажи за {period_name}*", ""]

    # Заказано
    cur_os, prev_os = cur.get("ordered_sum", 0), prev.get("ordered_sum", 0)
    cur_ou, prev_ou = cur.get("ordered_units", 0), prev.get("ordered_units", 0)
    d_os = fmt_pct(calc_delta(cur_os, prev_os))
    d_ou = fmt_pct(calc_delta(cur_ou, prev_ou))
    lines.append(f"🛒 *Заказано*")
    lines.append(f"  На сумму: {fmt_num(cur_os)} ₽ ({d_os})")
    lines.append(f"  Штук: {fmt_int(cur_ou)} ({d_ou})")
    lines.append(f"  vs период: {fmt_num(prev_os)} ₽ / {fmt_int(prev_ou)} шт.")
    lines.append("")

    # Доставлено
    cur_ds, prev_ds = cur.get("delivered_sum", 0), prev.get("delivered_sum", 0)
    cur_du, prev_du = cur.get("delivered_units", 0), prev.get("delivered_units", 0)
    d_ds = fmt_pct(calc_delta(cur_ds, prev_ds))
    d_du = fmt_pct(calc_delta(cur_du, prev_du))
    lines.append(f"📦 *Доставлено*")
    lines.append(f"  На сумму: {fmt_num(cur_ds)} ₽ ({d_ds})")
    lines.append(f"  Штук: {fmt_int(cur_du)} ({d_du})")
    lines.append(f"  vs период: {fmt_num(prev_ds)} ₽ / {fmt_int(prev_du)} шт.")
    lines.append("")

    # Отменено
    cur_cs, prev_cs = cur.get("canceled_sum", 0), prev.get("canceled_sum", 0)
    cur_cu, prev_cu = cur.get("canceled_units", 0), prev.get("canceled_units", 0)
    d_cs = fmt_pct(calc_delta(cur_cs, prev_cs))
    d_cu = fmt_pct(calc_delta(cur_cu, prev_cu))
    cur_cr = (cur_cu / cur_du * 100) if cur_du > 0 else None
    prev_cr = (prev_cu / prev_du * 100) if prev_du > 0 else None
    cr_text = f"{cur_cr:.2f}%" if cur_cr is not None else "∞"
    lines.append(f"❌ *Отменено*")
    lines.append(f"  На сумму: {fmt_num(cur_cs)} ₽ ({d_cs})")
    lines.append(f"  Штук: {fmt_int(cur_cu)} ({d_cu})")
    lines.append(f"  Доля отмен: {cr_text}")
    lines.append(f"  vs период: {fmt_num(prev_cs)} ₽ / {fmt_int(prev_cu)} шт.")
    lines.append("")

    # Реклама
    cur_ad, prev_ad = cur.get("ad_expense", 0), prev.get("ad_expense", 0)
    d_ad = fmt_pct(calc_delta(cur_ad, prev_ad))
    drr = cur.get("drr")
    eff_drr = cur.get("effective_drr")
    drr_text = f"{drr:.2f}%" if drr is not None else "∞"
    eff_text = f"{eff_drr:.2f}%" if eff_drr is not None else "∞"
    lines.append(f"📢 *Реклама*")
    lines.append(f"  Расходы: {fmt_num(cur_ad)} ₽ ({d_ad})")
    lines.append(f"  ДРР: {drr_text} | ДРР по доставленным: {eff_text}")
    lines.append(f"  vs период: {fmt_num(prev_ad)} ₽")
    lines.append("")

    # Расходы
    lines.append(format_expense_block(cur.get("expenses", {}), "Расходы за период"))
    return "\n".join(lines)

# ==================== ВЫЧИСЛЕНИЕ ПРЕДЫДУЩЕГО ПЕРИОДА ====================
def prev_period(date_from, date_to):
    """Предыдущий период такой же длины."""
    d1 = datetime.datetime.strptime(date_from, "%Y-%m-%d").date()
    d2 = datetime.datetime.strptime(date_to, "%Y-%m-%d").date()
    length = (d2 - d1).days + 1
    prev_end = d1 - datetime.timedelta(days=1)
    prev_start = prev_end - datetime.timedelta(days=length - 1)
    return prev_start.isoformat(), prev_end.isoformat()

# ==================== ОТЧЁТЫ ====================
async def build_today_report():
    now = get_current_time_msk()
    today_date = now.date()
    today_str = today_date.isoformat()
    yesterday_str = (today_date - datetime.timedelta(days=1)).isoformat()
    current_time = now.time()

    current_month_start = today_date.replace(day=1)
    cm_start_str = current_month_start.isoformat()
    pm_start = (current_month_start - datetime.timedelta(days=1)).replace(day=1)
    pm_start_str = pm_start.isoformat()
    days_passed = (today_date - current_month_start).days + 1
    pm_end = pm_start + datetime.timedelta(days=days_passed - 1)
    pm_end_str = pm_end.isoformat()

    write_log("📊 Сегодня: параллельная загрузка")
    postings_cur, postings_prev, ad_today, ad_month, fin_today, fin_month = await asyncio.gather(
        fetch_postings(cm_start_str, today_str),
        fetch_postings(pm_start_str, pm_end_str),
        fetch_advertising_expense(today_str, today_str),
        fetch_advertising_expense(cm_start_str, today_str),
        fetch_finance_transactions(today_str, today_str),
        fetch_finance_transactions(cm_start_str, today_str),
    )

    def agg_range(df, dt_, tl=None, ad_day=None):
        r = {"ordered_units": 0, "ordered_sum": 0.0, "delivered_units": 0,
             "delivered_sum": 0.0, "canceled_units": 0, "canceled_sum": 0.0}
        for p in postings_cur:
            created_at = p.get("created_at", "")
            if not created_at: continue
            try:
                dtx = datetime.datetime.fromisoformat(created_at.replace('Z', '+00:00')).astimezone(MOSCOW_TZ)
            except: continue
            ds = dtx.date().isoformat()
            if df and ds < df: continue
            if dt_ and ds > dt_: continue
            if tl is not None and ad_day is not None and ds == ad_day and dtx.time() > tl: continue
            u, s = 0, 0.0
            for prod in p.get("products", []):
                q = int(prod.get("quantity", 0))
                s += parse_price(prod) * q
                u += q
            st = p.get("status", "")
            r["ordered_units"] += u; r["ordered_sum"] += s
            if st in ("cancelled", "canceled"):
                r["canceled_units"] += u; r["canceled_sum"] += s
            elif st in ("delivered", "completed"):
                r["delivered_units"] += u; r["delivered_sum"] += s
        return r

    today_m = agg_range(today_str, today_str, current_time, today_str)
    yest_m = agg_range(yesterday_str, yesterday_str, current_time, yesterday_str)
    month_m = agg_range(cm_start_str, today_str, current_time, today_str)

    prev_m = aggregate_postings_range(postings_prev, pm_start_str, pm_end_str)

    exp_today = aggregate_finance_expenses(fin_today)
    exp_month = aggregate_finance_expenses(fin_month)
    if ad_today > 0:
        exp_today["Оплата за клик"] = exp_today.get("Оплата за клик", 0) + ad_today
    if ad_month > 0:
        exp_month["Оплата за клик"] = exp_month.get("Оплата за клик", 0) + ad_month

    def block_today():
        os_ = today_m["ordered_sum"]; ou_ = today_m["ordered_units"]
        cs_ = today_m["canceled_sum"]; cu_ = today_m["canceled_units"]
        d_os = fmt_pct(calc_delta(os_, yest_m["ordered_sum"]))
        d_ou = fmt_pct(calc_delta(ou_, yest_m["ordered_units"]))
        d_cs = fmt_pct(calc_delta(cs_, yest_m["canceled_sum"]))
        d_cu = fmt_pct(calc_delta(cu_, yest_m["canceled_units"]))
        du = today_m["delivered_units"]
        cr = (cu_ / du * 100) if du > 0 else None
        cr_text = f"{cr:.2f}%" if cr is not None else "∞"
        return (
            f"🔹 *Сегодня (на {now.strftime('%H:%M')} МСК)*\n"
            f"  🛒 Заказано: {fmt_num(os_)} ₽ / {fmt_int(ou_)} шт.\n"
            f"    vs вчера: {d_os} / {d_ou}\n"
            f"  ❌ Отменено: {fmt_num(cs_)} ₽ / {fmt_int(cu_)} шт.\n"
            f"    vs вчера: {d_cs} / {d_cu}\n"
            f"  Доля отмен: {cr_text}"
        )

    def block_month():
        os_ = month_m["ordered_sum"]; ou_ = month_m["ordered_units"]
        ds_ = month_m["delivered_sum"]; du_ = month_m["delivered_units"]
        cs_ = month_m["canceled_sum"]; cu_ = month_m["canceled_units"]
        d_os = fmt_pct(calc_delta(os_, prev_m["ordered_sum"]))
        d_ou = fmt_pct(calc_delta(ou_, prev_m["ordered_units"]))
        d_ds = fmt_pct(calc_delta(ds_, prev_m["delivered_sum"]))
        d_du = fmt_pct(calc_delta(du_, prev_m["delivered_units"]))
        d_cs = fmt_pct(calc_delta(cs_, prev_m["canceled_sum"]))
        d_cu = fmt_pct(calc_delta(cu_, prev_m["canceled_units"]))
        rev = os_
        drr = (ad_month / rev * 100) if rev > 0 else None
        drev = ds_
        eff_drr = (ad_month / drev * 100) if drev > 0 else None
        drr_text = f"{drr:.2f}%" if drr is not None else "∞"
        eff_drr_text = f"{eff_drr:.2f}%" if eff_drr is not None else "∞"
        return (
            f"🔹 *Текущий месяц*\n"
            f"  🛒 Заказано: {fmt_num(os_)} ₽ / {fmt_int(ou_)} шт. ({d_os} / {d_ou})\n"
            f"  📦 Доставлено: {fmt_num(ds_)} ₽ / {fmt_int(du_)} шт. ({d_ds} / {d_du})\n"
            f"  ❌ Отменено: {fmt_num(cs_)} ₽ / {fmt_int(cu_)} шт. ({d_cs} / {d_cu})\n"
            f"  📢 Реклама: {fmt_num(ad_month)} ₽\n"
            f"  ДРР: {drr_text} | ДРР по доставленным: {eff_drr_text}"
        )

    parts = [block_today(), block_month()]
    parts.append(format_expense_block(exp_today, "Расходы сегодня"))
    parts.append(format_expense_block(exp_month, "Расходы за текущий месяц"))
    return "📊 *Продажи за сегодня*\n\n\n" + "\n\n".join(parts)

async def build_period_report(date_from, date_to, period_name):
    prev_from, prev_to = prev_period(date_from, date_to)
    write_log(f"📊 Период {date_from}–{date_to}, предыдущий {prev_from}–{prev_to}")

    # Параллельно грузим 4 набора данных
    cur, prev = await asyncio.gather(
        get_period_metrics(date_from, date_to),
        get_period_metrics(prev_from, prev_to),
    )
    return format_period_comparison(cur, prev, period_name)

# ==================== КАЛЕНДАРЬ ====================
MONTH_NAMES = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
               "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

def create_calendar(year, month, callback_prefix):
    keyboard = []
    keyboard.append([InlineKeyboardButton(f"{MONTH_NAMES[month-1]} {year}", callback_data="ignore")])
    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    keyboard.append([InlineKeyboardButton(d, callback_data="ignore") for d in week_days])

    first_day, num_days = cal_mod.monthrange(year, month)
    row = [InlineKeyboardButton(" ", callback_data="ignore") for _ in range(first_day)]
    for day in range(1, num_days + 1):
        row.append(InlineKeyboardButton(str(day), callback_data=f"{callback_prefix}{year}-{month:02d}-{day:02d}"))
        if len(row) == 7:
            keyboard.append(row); row = []
    if row:
        while len(row) < 7:
            row.append(InlineKeyboardButton(" ", callback_data="ignore"))
        keyboard.append(row)

    nav = [
        InlineKeyboardButton("◀️", callback_data=f"{callback_prefix}prev_{year}_{month}"),
        InlineKeyboardButton(" ", callback_data="ignore"),
        InlineKeyboardButton("▶️", callback_data=f"{callback_prefix}next_{year}_{month}")
    ]
    keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f"{callback_prefix}cancel")])
    return InlineKeyboardMarkup(keyboard)

# ==================== КЛАВИАТУРЫ ====================
def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📊 Продажи за сегодня")],
        [KeyboardButton("📅 Выбрать дату")],
        [KeyboardButton("📆 Выбрать период")],
        [KeyboardButton("🔄 Обновить")],
    ], resize_keyboard=True)

def back_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("🔙 Назад")]], resize_keyboard=True)

# ==================== ХЕНДЛЕРЫ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("❌ Нет доступа.", reply_markup=ReplyKeyboardRemove())
        return
    await update.message.reply_text(
        f"🤖 Версия: {VERSION}\n\nВыберите действие:",
        reply_markup=main_keyboard())

async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🤖 Версия: {VERSION}")

async def today_report_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        return
    progress_msg = await update.message.reply_text("⏳ Загружаю данные...")
    try:
        report = await build_today_report()
        await progress_msg.delete()
        await update.message.reply_text(report, parse_mode="Markdown")
    except Exception as e:
        write_log(f"❌ Ошибка: {e}")
        await progress_msg.edit_text(f"❌ Ошибка: {e}")

# ---------- Выбор даты ----------
async def date_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        return
    now = get_moscow_today()
    await update.message.reply_text(
        "Выберите дату:", reply_markup=create_calendar(now.year, now.month, "d_"))
    return WAITING_DATE_SINGLE

async def date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "d_cancel":
        await query.edit_message_text("Выбор отменён.")
        await query.message.reply_text("Выберите действие:", reply_markup=main_keyboard())
        return ConversationHandler.END

    if data.startswith("d_prev_") or data.startswith("d_next_"):
        m = re.search(r'(prev|next)_(\d+)_(\d+)', data)
        action, y, mo = m.group(1), int(m.group(2)), int(m.group(3))
        if action == "prev":
            mo -= 1
            if mo == 0: mo = 12; y -= 1
        else:
            mo += 1
            if mo == 13: mo = 1; y += 1
        await query.edit_message_reply_markup(reply_markup=create_calendar(y, mo, "d_"))
        return WAITING_DATE_SINGLE

    if data.startswith("d_"):
        date_str = data[2:]
        if re.match(r"\d{4}-\d{2}-\d{2}$", date_str):
            valid, result = validate_date(date_str)
            if not valid:
                await query.edit_message_text(result)
                return WAITING_DATE_SINGLE
            await query.edit_message_text(f"⏳ Загружаю данные за {date_str}...")
            try:
                report = await build_period_report(date_str, date_str, date_str)
                await query.edit_message_text(report, parse_mode="Markdown")
                await query.message.reply_text("Выберите действие:", reply_markup=main_keyboard())
            except Exception as e:
                write_log(f"❌ Ошибка даты: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")
            return ConversationHandler.END
    await query.edit_message_text("❌ Неверный выбор.")
    return WAITING_DATE_SINGLE

# ---------- Выбор периода ----------
async def period_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗓️ По месяцам", callback_data="pm")],
        [InlineKeyboardButton("📅 По кварталам", callback_data="pq")],
        [InlineKeyboardButton("📆 По годам", callback_data="py")],
        [InlineKeyboardButton("✏️ Произвольный период", callback_data="pcustom")],
        [InlineKeyboardButton("🔙 Назад", callback_data="pcancel")],
    ])
    await update.message.reply_text("Выберите тип периода:", reply_markup=keyboard)
    return WAITING_PERIOD_TYPE

async def period_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    cur_year = get_moscow_today().year
    years = list(range(cur_year - 9, cur_year + 1))

    if data == "pcancel":
        await query.edit_message_text("Отменено.")
        await query.message.reply_text("Выберите действие:", reply_markup=main_keyboard())
        return ConversationHandler.END

    if data == "pm":
        btns = [[InlineKeyboardButton(str(y), callback_data=f"pmy_{y}")] for y in years]
        btns.append([InlineKeyboardButton("🔙 Назад", callback_data="pcancel")])
        await query.edit_message_text("Выберите год:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_PERIOD_YEAR

    if data == "pq":
        btns = [[InlineKeyboardButton(str(y), callback_data=f"pqy_{y}")] for y in years]
        btns.append([InlineKeyboardButton("🔙 Назад", callback_data="pcancel")])
        await query.edit_message_text("Выберите год:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_PERIOD_YEAR

    if data == "py":
        btns = [[InlineKeyboardButton(str(y), callback_data=f"pyy_{y}")] for y in years]
        btns.append([InlineKeyboardButton("🔙 Назад", callback_data="pcancel")])
        await query.edit_message_text("Выберите год:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_YEAR_SELECT

    if data == "pcustom":
        now = get_moscow_today()
        await query.edit_message_text(
            "Выберите начальную дату:",
            reply_markup=create_calendar(now.year, now.month, "s_"))
        return WAITING_PERIOD_START

    return ConversationHandler.END

# ---------- Выбор года/месяца ----------
async def period_year_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "pcancel":
        await query.edit_message_text("Отменено.")
        await query.message.reply_text("Выберите действие:", reply_markup=main_keyboard())
        return ConversationHandler.END

    if data.startswith("pmy_"):
        y = int(data.split("_")[1])
        btns = [[InlineKeyboardButton(MONTH_NAMES[i-1], callback_data=f"pmm_{i}_{y}")] for i in range(1, 13)]
        btns.append([InlineKeyboardButton("🔙 Назад", callback_data="pcancel")])
        await query.edit_message_text(f"Месяц {y}:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_PERIOD_MONTH

    if data.startswith("pqy_"):
        y = int(data.split("_")[1])
        btns = [[InlineKeyboardButton(f"{q} кв.", callback_data=f"pqq_{q}_{y}")] for q in range(1, 5)]
        btns.append([InlineKeyboardButton("🔙 Назад", callback_data="pcancel")])
        await query.edit_message_text(f"Квартал {y}:", reply_markup=InlineKeyboardMarkup(btns))
        return WAITING_PERIOD_QUARTER

    if data.startswith("pyy_"):
        y = int(data.split("_")[1])
        date_from = datetime.date(y, 1, 1).isoformat()
        date_to = datetime.date(y, 12, 31).isoformat()
        await query.edit_message_text(f"⏳ Загружаю данные за {y} год...")
        try:
            report = await build_period_report(date_from, date_to, f"{y} год")
            await query.edit_message_text(report, parse_mode="Markdown")
            await query.message.reply_text("Выберите действие:", reply_markup=main_keyboard())
        except Exception as e:
            write_log(f"❌ Ошибка года: {e}")
            await query.edit_message_text(f"❌ Ошибка: {e}")
        return ConversationHandler.END

    return WAITING_PERIOD_YEAR

async def period_month_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "pcancel":
        await query.edit_message_text("Отменено.")
        await query.message.reply_text("Выберите действие:", reply_markup=main_keyboard())
        return ConversationHandler.END

    if data.startswith("pmm_"):
        parts = data.split("_")
        m, y = int(parts[1]), int(parts[2])
        first = datetime.date(y, m, 1)
        last = datetime.date(y, 12, 31) if m == 12 else datetime.date(y, m+1, 1) - datetime.timedelta(days=1)
        period_name = f"{MONTH_NAMES[m-1]} {y}"
        await query.edit_message_text(f"⏳ Загружаю данные за {period_name}...")
        try:
            report = await build_period_report(first.isoformat(), last.isoformat(), period_name)
            await query.edit_message_text(report, parse_mode="Markdown")
            await query.message.reply_text("Выберите действие:", reply_markup=main_keyboard())
        except Exception as e:
            write_log(f"❌ Ошибка месяца: {e}")
            await query.edit_message_text(f"❌ Ошибка: {e}")
        return ConversationHandler.END

    return WAITING_PERIOD_MONTH

async def period_quarter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "pcancel":
        await query.edit_message_text("Отменено.")
        await query.message.reply_text("Выберите действие:", reply_markup=main_keyboard())
        return ConversationHandler.END

    if data.startswith("pqq_"):
        parts = data.split("_")
        q, y = int(parts[1]), int(parts[2])
        start_m = (q-1)*3 + 1
        end_m = q*3
        first = datetime.date(y, start_m, 1)
        last = datetime.date(y, 12, 31) if end_m == 12 else datetime.date(y, end_m+1, 1) - datetime.timedelta(days=1)
        period_name = f"{q} квартал {y}"
        await query.edit_message_text(f"⏳ Загружаю данные за {period_name}...")
        try:
            report = await build_period_report(first.isoformat(), last.isoformat(), period_name)
            await query.edit_message_text(report, parse_mode="Markdown")
            await query.message.reply_text("Выберите действие:", reply_markup=main_keyboard())
        except Exception as e:
            write_log(f"❌ Ошибка квартала: {e}")
            await query.edit_message_text(f"❌ Ошибка: {e}")
        return ConversationHandler.END

    return WAITING_PERIOD_QUARTER

# ---------- Произвольный период ----------
async def custom_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "s_cancel":
        await query.edit_message_text("Отменено.")
        await query.message.reply_text("Выберите действие:", reply_markup=main_keyboard())
        return ConversationHandler.END

    if data.startswith("s_prev_") or data.startswith("s_next_"):
        m = re.search(r'(prev|next)_(\d+)_(\d+)', data)
        action, y, mo = m.group(1), int(m.group(2)), int(m.group(3))
        if action == "prev":
            mo -= 1
            if mo == 0: mo = 12; y -= 1
        else:
            mo += 1
            if mo == 13: mo = 1; y += 1
        await query.edit_message_reply_markup(reply_markup=create_calendar(y, mo, "s_"))
        return WAITING_PERIOD_START

    if data.startswith("s_"):
        date_str = data[2:]
        if re.match(r"\d{4}-\d{2}-\d{2}$", date_str):
            valid, result = validate_date(date_str)
            if not valid:
                await query.edit_message_text(result)
                return WAITING_PERIOD_START
            context.user_data['period_start'] = date_str
            now = get_moscow_today()
            await query.edit_message_text(
                f"Начало: {date_str}\nВыберите конечную дату:",
                reply_markup=create_calendar(now.year, now.month, "e_"))
            return WAITING_PERIOD_END

    return WAITING_PERIOD_START

async def custom_end_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "e_cancel":
        await query.edit_message_text("Отменено.")
        await query.message.reply_text("Выберите действие:", reply_markup=main_keyboard())
        return ConversationHandler.END

    if data.startswith("e_prev_") or data.startswith("e_next_"):
        m = re.search(r'(prev|next)_(\d+)_(\d+)', data)
        action, y, mo = m.group(1), int(m.group(2)), int(m.group(3))
        if action == "prev":
            mo -= 1
            if mo == 0: mo = 12; y -= 1
        else:
            mo += 1
            if mo == 13: mo = 1; y += 1
        await query.edit_message_reply_markup(reply_markup=create_calendar(y, mo, "e_"))
        return WAITING_PERIOD_END

    if data.startswith("e_"):
        end_str = data[2:]
        if re.match(r"\d{4}-\d{2}-\d{2}$", end_str):
            start_str = context.user_data.get('period_start')
            if not start_str:
                await query.edit_message_text("❌ Ошибка: начальная дата потеряна.")
                return ConversationHandler.END
            valid, result = validate_period(start_str, end_str)
            if not valid:
                await query.edit_message_text(result)
                return WAITING_PERIOD_END
            period_name = f"{start_str} – {end_str}"
            await query.edit_message_text(f"⏳ Загружаю данные за {period_name}...")
            try:
                report = await build_period_report(start_str, end_str, period_name)
                await query.edit_message_text(report, parse_mode="Markdown")
                await query.message.reply_text("Выберите действие:", reply_markup=main_keyboard())
            except Exception as e:
                write_log(f"❌ Ошибка периода: {e}")
                await query.edit_message_text(f"❌ Ошибка: {e}")
            context.user_data.pop('period_start', None)
            return ConversationHandler.END

    return WAITING_PERIOD_END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END

# ==================== ЗАПУСК ====================
def main():
    if not validate_env_vars():
        sys.exit(1)
    write_log(f"🚀 Запуск (v{VERSION})")
    write_log(f"✅ OZON_CLIENT_ID: {mask_secret(OZON_CLIENT_ID)}")
    write_log(f"✅ OZON_API_KEY: {mask_secret(OZON_API_KEY)}")
    write_log(f"✅ TELEGRAM_BOT_TOKEN: {mask_secret(TELEGRAM_BOT_TOKEN)}")
    write_log(f"✅ ADMIN_CHAT_ID: {ADMIN_CHAT_ID}")
    update_version_history(VERSION, CHANGELOG_MESSAGE)

    app = (Application.builder()
           .token(TELEGRAM_BOT_TOKEN)
           .connect_timeout(30.0)
           .read_timeout(30.0)
           .write_timeout(30.0)
           .post_init(init_http_session)
           .post_shutdown(close_http_session)
           .build())

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("version", version_command))
    app.add_handler(CommandHandler("cancel", cancel))

    # Кнопки главного меню
    app.add_handler(MessageHandler(
        filters.Text(["📊 Продажи за сегодня", "🔄 Обновить"]),
        today_report_handler))

    # Диалог «Выбрать дату»
    conv_date = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📅 Выбрать дату"), date_menu)],
        states={WAITING_DATE_SINGLE: [CallbackQueryHandler(date_callback)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Диалог «Выбрать период»
    conv_period = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📆 Выбрать период"), period_menu)],
        states={
            WAITING_PERIOD_TYPE: [CallbackQueryHandler(period_type_callback)],
            WAITING_PERIOD_YEAR: [CallbackQueryHandler(period_year_callback)],
            WAITING_PERIOD_MONTH: [CallbackQueryHandler(period_month_callback)],
            WAITING_PERIOD_QUARTER: [CallbackQueryHandler(period_quarter_callback)],
            WAITING_PERIOD_START: [CallbackQueryHandler(custom_start_callback)],
            WAITING_PERIOD_END: [CallbackQueryHandler(custom_end_callback)],
            WAITING_YEAR_SELECT: [CallbackQueryHandler(period_year_callback)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_date)
    app.add_handler(conv_period)

    write_log("🚀 Бот готов.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, timeout=30)

if __name__ == "__main__":
    main()
