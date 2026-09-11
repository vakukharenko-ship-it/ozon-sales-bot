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
VERSION = "2.4.2"
CHANGELOG_MESSAGE = "Товарные отчёты (топ за день/период + динамика). Persistent disk cache — повторные запросы мгновенные."

# ==================== КОНСТАНТЫ ====================
API_TIMEOUT = 60
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY = 10
POSTINGS_RATE = 0.5
FINANCE_RATE = 0.5
PERFORMANCE_RATE = 1.0
CACHE_TTL_SECONDS = 3600       # RAM-кэш: 1 час
DISK_CACHE_DIR = "/app/data/cache"
VERSION_HISTORY_FILE = "version_history.json"
LOG_FILE = "/app/data/ozon_log.txt"

# Состояния
WAITING_DATE_SINGLE = 1
WAITING_PERIOD_TYPE = 2
WAITING_PERIOD_START = 3
WAITING_PERIOD_END = 4
WAITING_PERIOD_YEAR = 5
WAITING_PERIOD_MONTH = 6
WAITING_PERIOD_QUARTER = 7
WAITING_YEAR_SELECT = 8

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
TYPE_ID_NAMES = {1: "Обработка товара", 29: "Последняя миля", 32: "Логистика",
                 98: "Доп. услуги отправления", 12: "Прочие услуги Ozon",
                 76: "Внешние услуги Ozon"}
CATEGORY_FALLBACK = {"ITEM": "Услуги по товарам", "NON_ITEM": "Внешние услуги Ozon",
                     "POSTING": "Услуги отправления", "CONTAINER": "Контейнеры"}

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
        except: history = []
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

# ==================== PERSISTENT CACHE ====================
def _disk_path(key):
    h = hashlib.md5(key.encode()).hexdigest()[:16]
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', key)[:40]
    return os.path.join(DISK_CACHE_DIR, f"{safe}_{h}.json")

def disk_cache_get(key):
    path = _disk_path(key)
    if not os.path.exists(path): return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except: return None

def disk_cache_set(key, value):
    try:
        os.makedirs(DISK_CACHE_DIR, exist_ok=True)
        with open(_disk_path(key), "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
    except Exception as e:
        write_log(f"⚠️ Ошибка disk-cache: {e}")

async def get_from_cache(key):
    async with _cache_lock:
        if key in _api_cache:
            ts = _cache_timestamps.get(key)
            if ts and (time.time() - ts) < CACHE_TTL_SECONDS:
                return _api_cache[key]
    # RAM промах — проверяем диск
    val = disk_cache_get(key)
    if val is not None:
        async with _cache_lock:
            _api_cache[key] = val
            _cache_timestamps[key] = time.time()
        write_log(f"💾 Кэш-хит: {key[:50]}")
    return val

async def save_to_cache(key, value):
    async with _cache_lock:
        _api_cache[key] = value
        _cache_timestamps[key] = time.time()
    disk_cache_set(key, value)

# ==================== УТИЛИТЫ ====================
def get_moscow_today(): return datetime.datetime.now(MOSCOW_TZ).date()
def get_current_time_msk(): return datetime.datetime.now(MOSCOW_TZ)
def is_admin(chat_id): return chat_id == ADMIN_CHAT_ID

def fmt_num(val): return f"{val:,.2f}".replace(",", " ") if val else "0.00"
def fmt_int(val): return str(val) if val else "0"

def parse_money(obj) -> float:
    if obj is None: return 0.0
    if isinstance(obj, dict):
        val = obj.get("amount", obj.get("value", 0))
    else:
        val = obj
    try: return float(str(val).replace(",", "."))
    except (TypeError, ValueError): return 0.0

def parse_price(product) -> float: return parse_money(product.get("price"))

def type_name(type_id, cat):
    if type_id in TYPE_ID_NAMES: return TYPE_ID_NAMES[type_id]
    fb = CATEGORY_FALLBACK.get(cat, "Услуги")
    return f"{fb} (type {type_id})" if type_id else fb

def calc_delta(cur, prev):
    if prev == 0: return None
    try: return ((cur - prev) / abs(prev)) * 100
    except: return None

def fmt_pct(val):
    if val is None: return "∞"
    return f"+{val:.1f}%" if val > 0 else f"{val:.1f}%"

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
        self.rate = rate
        self.lock = asyncio.Lock()
        self.last = 0
    async def acquire(self):
        async with self.lock:
            now = time.time()
            w = (1.0 / self.rate) - (now - self.last)
            if w > 0: await asyncio.sleep(w)
            self.last = time.time()

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
                        if resp.status == 429:
                            w = API_RETRY_DELAY * (2 ** attempt)
                            write_log(f"⚠️ 429, ждём {w}с")
                            await asyncio.sleep(w); continue
                        if resp.status >= 400:
                            write_log(f"❌ API {resp.status}: {body[:300]}")
                            raise aiohttp.ClientResponseError(resp.request_info, resp.history,
                                status=resp.status, message=body[:200])
                        return json.loads(body)
                else:
                    async with _http_session.get(url, headers=headers, params=payload) as resp:
                        body = await resp.text()
                        if resp.status == 429:
                            w = API_RETRY_DELAY * (2 ** attempt)
                            write_log(f"⚠️ 429, ждём {w}с")
                            await asyncio.sleep(w); continue
                        if resp.status >= 400:
                            write_log(f"❌ API {resp.status}: {body[:300]}")
                            raise aiohttp.ClientResponseError(resp.request_info, resp.history,
                                status=resp.status, message=body[:200])
                        return json.loads(body)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == API_RETRY_ATTEMPTS - 1:
                    write_log(f"❌ API failed: {e}"); raise
                await asyncio.sleep(API_RETRY_DELAY * (attempt + 1))
        raise Exception("API failed")

# ==================== TOKEN ====================
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
        write_log(f"❌ Ошибка токена: {e}"); return None

# ==================== ОТГРУЗКИ ====================
async def fetch_postings(date_from, date_to):
    cache_key = f"postings_{date_from}_{date_to}"
    cached = await get_from_cache(cache_key)
    if cached is not None: return cached

    headers = {"Client-Id": OZON_CLIENT_ID, "Api-Key": OZON_API_KEY, "Content-Type": "application/json"}
    since = f"{date_from}T00:00:00Z"; to = f"{date_to}T23:59:59Z"
    all_p = []; cursor = ""; LIMIT = 100
    while True:
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

# ==================== РЕКЛАМА ====================
async def fetch_advertising_expense(date_from, date_to):
    cache_key = f"ad_{date_from}_{date_to}"
    cached = await get_from_cache(cache_key)
    if cached is not None: return cached
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
        await save_to_cache(cache_key, total); return total
    except Exception as e:
        write_log(f"❌ Реклама: {e}"); return 0.0

# ==================== ФИНАНСЫ ====================
async def fetch_finance_accruals_by_day(date_str):
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
    return all_a

async def fetch_finance_transactions(date_from, date_to):
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
    write_log(f"💰 Финансы: {len(days)} дней параллельно")
    tasks = [fetch_finance_accruals_by_day(d) for d in days]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_a = []
    for i, res in enumerate(results):
        if isinstance(res, list): all_a.extend(res)
    write_log(f"💰 Начислений: {len(all_a)}")
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
                    if amt < 0: add(type_name(tid, cat), abs(amt))
            continue
        if cat == "ITEM":
            i_fees = item.get("item_fees") or {}
            for grp in i_fees.get("fees", []) or []:
                for fee in grp.get("fees", []) or []:
                    tid = fee.get("type_id"); amt = parse_money(fee.get("accrued"))
                    if amt < 0: add(type_name(tid, cat), abs(amt))
            continue
        if cat == "NON_ITEM":
            non = item.get("non_item_fee") or {}
            tid = non.get("type_id"); amt = parse_money(non.get("accrued"))
            if amt < 0: add(type_name(tid, cat), abs(amt))
            continue
        if cat == "CONTAINER":
            cont = item.get("container_fees") or {}
            amt = parse_money(cont.get("accrued", cont))
            if amt < 0: add("Контейнеры", abs(amt))
            continue
        total = parse_money(item.get("total_amount"))
        if total < 0: add(cat or "Прочее", abs(total))
    return result

# ==================== АГРЕГАЦИЯ ОТГРУЗОК ====================
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
            sku = str(prod.get("sku", "0"))
            name = prod.get("name", "—")
            oid = prod.get("offer_id", "")
            q = int(prod.get("quantity", 0))
            price = parse_price(prod)
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

# ==================== ФОРМАТИРОВАНИЕ ====================
def format_expense_block(exp, title, limit=20):
    if not exp: return f"🔹 *{title}*\nНет данных о расходах.\n"
    total = sum(exp.values())
    lines = [f"🔹 *{title}*", f"  *Итого:* {total:,.2f} ₽"]
    for k, v in sorted(exp.items(), key=lambda x: x[1], reverse=True)[:limit]:
        lines.append(f"    {k}: {v:,.2f} ₽")
    return "\n".join(lines)

def format_top_products(products, title, limit=15):
    if not products: return f"📦 *{title}*\nНет данных."
    sorted_items = sorted(products.items(), key=lambda x: x[1]["ordered_sum"], reverse=True)[:limit]
    lines = [f"📦 *{title}*", ""]
    for i, (sku, s) in enumerate(sorted_items, 1):
        name = s["name"][:40]
        oid = s["offer_id"]
        lines.append(f"{i}. {name}" + (f" (Арт: {oid})" if oid else ""))
        lines.append(f"   🛒 {fmt_num(s['ordered_sum'])} ₽ / {s['ordered_units']} шт.")
        lines.append(f"   📦 {fmt_num(s['delivered_sum'])} ₽ / {s['delivered_units']} шт.")
        lines.append(f"   ❌ {fmt_num(s['canceled_sum'])} ₽ / {s['canceled_units']} шт.")
        lines.append("")
    return "\n".join(lines)

def format_products_summary(products):
    if not products: return "Нет данных"
    rev = sum(p["ordered_sum"] for p in products.values())
    units = sum(p["ordered_units"] for p in products.values())
    orders = sum(p["order_count"] for p in products.values())
    avg = (rev / orders) if orders > 0 else 0
    return (f"Сводка: товаров {len(products)} | выручка {fmt_num(rev)} ₽ | "
            f"единиц {units} | заказов {orders} | ср.чек {fmt_num(avg)} ₽")

def format_period_comparison(cur, prev, name):
    lines = [f"📊 *Продажи за {name}*", ""]
    for label, key_sum, key_units in [
        ("🛒 *Заказано*", "ordered_sum", "ordered_units"),
        ("📦 *Доставлено*", "delivered_sum", "delivered_units"),
        ("❌ *Отменено*", "canceled_sum", "canceled_units")]:
        cs = cur.get(key_sum, 0); ps = prev.get(key_sum, 0)
        cu = cur.get(key_units, 0); pu = prev.get(key_units, 0)
        lines.append(label)
        lines.append(f"  {fmt_num(cs)} ₽ ({fmt_pct(calc_delta(cs, ps))}) / {fmt_int(cu)} шт. ({fmt_pct(calc_delta(cu, pu))})")
        lines.append(f"  vs: {fmt_num(ps)} ₽ / {fmt_int(pu)} шт.")
        lines.append("")
    cur_ad = cur.get("ad_expense", 0); prev_ad = prev.get("ad_expense", 0)
    drr = cur.get("drr"); edrr = cur.get("effective_drr")
    lines.append("📢 *Реклама*")
    lines.append(f"  {fmt_num(cur_ad)} ₽ ({fmt_pct(calc_delta(cur_ad, prev_ad))})")
    lines.append(f"  ДРР: {f'{drr:.2f}%' if drr is not None else '∞'} | "
                 f"ДРР по доставл.: {f'{edrr:.2f}%' if edrr is not None else '∞'}")
    lines.append(f"  vs: {fmt_num(prev_ad)} ₽")
    lines.append("")
    lines.append(format_expense_block(cur.get("expenses", {}), "Расходы за период"))
    return "\n".join(lines)

# ==================== СБОРКА МЕТРИК ====================
async def get_period_metrics(df, dt):
    postings, ad, fin = await asyncio.gather(
        fetch_postings(df, dt),
        fetch_advertising_expense(df, dt),
        fetch_finance_transactions(df, dt),
    )
    agg = aggregate_postings_range(postings, df, dt)
    exp = aggregate_finance_expenses(fin)
    osum = agg["ordered_sum"]; dsum = agg["delivered_sum"]
    return {**agg, "ad_expense": ad,
            "drr": (ad / osum * 100) if osum > 0 else None,
            "effective_drr": (ad / dsum * 100) if dsum > 0 else None,
            "expenses": exp}

# ==================== ОТЧЁТЫ ====================
async def build_today_report():
    now = get_current_time_msk()
    td = now.date(); today = td.isoformat()
    yest = (td - datetime.timedelta(days=1)).isoformat()
    ct = now.time()
    cm = td.replace(day=1); cm_s = cm.isoformat()
    pm = (cm - datetime.timedelta(days=1)).replace(day=1); pm_s = pm.isoformat()
    dp = (td - cm).days + 1
    pm_e = pm + datetime.timedelta(days=dp - 1); pm_e_s = pm_e.isoformat()

    postings_cur, postings_prev, ad_t, ad_m, fin_t, fin_m = await asyncio.gather(
        fetch_postings(cm_s, today),
        fetch_postings(pm_s, pm_e_s),
        fetch_advertising_expense(today, today),
        fetch_advertising_expense(cm_s, today),
        fetch_finance_transactions(today, today),
        fetch_finance_transactions(cm_s, today),
    )
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
        du = t_m["delivered_units"]
        cr = (cu_ / du * 100) if du > 0 else None
        return (f"🔹 *Сегодня (на {now.strftime('%H:%M')} МСК)*\n"
                f"  🛒 {fmt_num(os_)} ₽ / {fmt_int(ou_)} шт.\n"
                f"    vs вчера: {fmt_pct(calc_delta(os_, y_m['ordered_sum']))} / "
                f"{fmt_pct(calc_delta(ou_, y_m['ordered_units']))}\n"
                f"  ❌ {fmt_num(cs_)} ₽ / {fmt_int(cu_)} шт.\n"
                f"  Доля отмен: {f'{cr:.2f}%' if cr is not None else '∞'}")
    def blk_month():
        os_ = m_m["ordered_sum"]; ou_ = m_m["ordered_units"]
        ds_ = m_m["delivered_sum"]; du_ = m_m["delivered_units"]
        cs_ = m_m["canceled_sum"]; cu_ = m_m["canceled_units"]
        drr = (ad_m / os_ * 100) if os_ > 0 else None
        edrr = (ad_m / ds_ * 100) if ds_ > 0 else None
        return (f"🔹 *Текущий месяц*\n"
                f"  🛒 {fmt_num(os_)} ₽ / {fmt_int(ou_)} шт. "
                f"({fmt_pct(calc_delta(os_, p_m['ordered_sum']))} / "
                f"{fmt_pct(calc_delta(ou_, p_m['ordered_units']))})\n"
                f"  📦 {fmt_num(ds_)} ₽ / {fmt_int(du_)} шт. "
                f"({fmt_pct(calc_delta(ds_, p_m['delivered_sum']))} / "
                f"{fmt_pct(calc_delta(du_, p_m['delivered_units']))})\n"
                f"  ❌ {fmt_num(cs_)} ₽ / {fmt_int(cu_)} шт.\n"
                f"  📢 Реклама: {fmt_num(ad_m)} ₽\n"
                f"  ДРР: {f'{drr:.2f}%' if drr is not None else '∞'} | "
                f"ДРР по доставл.: {f'{edrr:.2f}%' if edrr is not None else '∞'}")
    parts = [blk_today(), blk_month(),
             format_expense_block(exp_t, "Расходы сегодня"),
             format_expense_block(exp_m, "Расходы за текущий месяц")]
    return "📊 *Продажи за сегодня*\n\n\n" + "\n\n".join(parts)

async def build_period_report(df, dt, name):
    pf, pt = prev_period(df, dt)
    write_log(f"📊 Период {df}–{dt}, предыдущий {pf}–{pt}")
    cur, prev = await asyncio.gather(
        get_period_metrics(df, dt),
        get_period_metrics(pf, pt),
    )
    return format_period_comparison(cur, prev, name)

# ==================== КАЛЕНДАРЬ ====================
MONTH_NAMES = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
               "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

def create_calendar(year, month, prefix):
    kb = []
    kb.append([InlineKeyboardButton(f"{MONTH_NAMES[month-1]} {year}", callback_data="ignore")])
    kb.append([InlineKeyboardButton(d, callback_data="ignore") for d in ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]])
    first, ndays = cal_mod.monthrange(year, month)
    row = [InlineKeyboardButton(" ", callback_data="ignore") for _ in range(first)]
    for d in range(1, ndays+1):
        row.append(InlineKeyboardButton(str(d), callback_data=f"{prefix}{year}-{month:02d}-{d:02d}"))
        if len(row) == 7: kb.append(row); row = []
    if row:
        while len(row) < 7: row.append(InlineKeyboardButton(" ", callback_data="ignore"))
        kb.append(row)
    kb.append([
        InlineKeyboardButton("◀️", callback_data=f"{prefix}prev_{year}_{month}"),
        InlineKeyboardButton(" ", callback_data="ignore"),
        InlineKeyboardButton("▶️", callback_data=f"{prefix}next_{year}_{month}")])
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data=f"{prefix}cancel")])
    return InlineKeyboardMarkup(kb)

# ==================== КЛАВИАТУРЫ ====================
def main_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📊 Продажи за сегодня")],
        [KeyboardButton("📅 Выбрать дату"), KeyboardButton("📆 Выбрать период")],
        [KeyboardButton("📦 Топ товаров за сегодня")],
        [KeyboardButton("🏆 Товары за период")],
    ], resize_keyboard=True)

def products_period_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗓️ По месяцам", callback_data="tpm")],
        [InlineKeyboardButton("📅 По кварталам", callback_data="tpq")],
        [InlineKeyboardButton("📆 По годам", callback_data="tpy")],
        [InlineKeyboardButton("✏️ Произвольный период", callback_data="tpc")],
        [InlineKeyboardButton("🔙 Назад", callback_data="tpcancel")],
    ])

# ==================== ХЕНДЛЕРЫ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("❌ Нет доступа.", reply_markup=ReplyKeyboardRemove())
        return
    await update.message.reply_text(f"🤖 Версия: {VERSION}\n\nВыберите действие:", reply_markup=main_kb())

async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🤖 Версия: {VERSION}")

# ----- Продажи сегодня -----
async def today_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    msg = await update.message.reply_text("⏳ Загружаю данные...")
    try:
        r = await build_today_report()
        await msg.delete()
        await update.message.reply_text(r, parse_mode="Markdown")
    except Exception as e:
        write_log(f"❌ Ошибка: {e}"); await msg.edit_text(f"❌ Ошибка: {e}")

# ----- Дата -----
async def date_menu(update, context):
    if not is_admin(update.effective_chat.id): return
    now = get_moscow_today()
    await update.message.reply_text("Выберите дату:",
        reply_markup=create_calendar(now.year, now.month, "d_"))
    return WAITING_DATE_SINGLE

async def date_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "d_cancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
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
            if not ok:
                await q.edit_message_text(r); return WAITING_DATE_SINGLE
            await q.edit_message_text(f"⏳ Данные за {ds}...")
            try:
                rpt = await build_period_report(ds, ds, ds)
                await q.edit_message_text(rpt, parse_mode="Markdown")
                await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
            except Exception as e:
                write_log(f"❌ {e}"); await q.edit_message_text(f"❌ {e}")
            return ConversationHandler.END
    return WAITING_DATE_SINGLE

# ----- Период -----
async def period_menu(update, context):
    if not is_admin(update.effective_chat.id): return
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
    cy = get_moscow_today().year
    ys = list(range(cy-9, cy+1))
    if d == "pcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
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
        await q.edit_message_text("Начало:",
            reply_markup=create_calendar(now.year, now.month, "s_"))
        return WAITING_PERIOD_START
    return ConversationHandler.END

async def period_year_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "pcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
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
        await q.edit_message_text(f"⏳ За {y} год...")
        try:
            rpt = await build_period_report(df, dt, f"{y} год")
            await q.edit_message_text(rpt, parse_mode="Markdown")
            await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await q.edit_message_text(f"❌ {e}")
        return ConversationHandler.END
    return WAITING_PERIOD_YEAR

async def period_month_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "pcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
        return ConversationHandler.END
    if d.startswith("pmm_"):
        pr = d.split("_"); m, y = int(pr[1]), int(pr[2])
        f = datetime.date(y,m,1)
        l = datetime.date(y,12,31) if m==12 else datetime.date(y,m+1,1)-datetime.timedelta(days=1)
        name = f"{MONTH_NAMES[m-1]} {y}"
        await q.edit_message_text(f"⏳ За {name}...")
        try:
            rpt = await build_period_report(f.isoformat(), l.isoformat(), name)
            await q.edit_message_text(rpt, parse_mode="Markdown")
            await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await q.edit_message_text(f"❌ {e}")
        return ConversationHandler.END
    return WAITING_PERIOD_MONTH

async def period_quarter_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "pcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
        return ConversationHandler.END
    if d.startswith("pqq_"):
        pr = d.split("_"); qn, y = int(pr[1]), int(pr[2])
        sm = (qn-1)*3 + 1; em = qn*3
        f = datetime.date(y, sm, 1)
        l = datetime.date(y,12,31) if em==12 else datetime.date(y,em+1,1)-datetime.timedelta(days=1)
        name = f"{qn} квартал {y}"
        await q.edit_message_text(f"⏳ За {name}...")
        try:
            rpt = await build_period_report(f.isoformat(), l.isoformat(), name)
            await q.edit_message_text(rpt, parse_mode="Markdown")
            await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await q.edit_message_text(f"❌ {e}")
        return ConversationHandler.END
    return WAITING_PERIOD_QUARTER

async def custom_start_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "s_cancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
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
            if not ok:
                await q.edit_message_text(r); return WAITING_PERIOD_START
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
        await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
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
            if not ds:
                await q.edit_message_text("❌ потеряна начальная дата"); return ConversationHandler.END
            ok, r = validate_period(ds, de)
            if not ok:
                await q.edit_message_text(r); return WAITING_PERIOD_END
            nm = f"{ds} – {de}"
            await q.edit_message_text(f"⏳ За {nm}...")
            try:
                rpt = await build_period_report(ds, de, nm)
                await q.edit_message_text(rpt, parse_mode="Markdown")
                await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
            except Exception as e:
                write_log(f"❌ {e}"); await q.edit_message_text(f"❌ {e}")
            context.user_data.pop('p_start', None)
            return ConversationHandler.END
    return WAITING_PERIOD_END

# ----- Товары сегодня -----
async def top_today(update, context):
    if not is_admin(update.effective_chat.id): return
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

# ----- Товары за период -----
async def products_period_menu(update, context):
    if not is_admin(update.effective_chat.id): return
    await update.message.reply_text("Период для товаров:", reply_markup=products_period_kb())
    return WAITING_PRODUCT_PERIOD_TYPE

async def products_period_type_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    cy = get_moscow_today().year; ys = list(range(cy-9, cy+1))
    if d == "tpcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
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
        await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
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
        await q.edit_message_text(f"⏳ Товары за {y} год...")
        try:
            postings = await fetch_postings(df, dt)
            stats = aggregate_products(postings, df, dt)
            txt = format_top_products(stats, f"Товары за {y} год", 20) + "\n" + format_products_summary(stats)
            await q.edit_message_text(txt)
            await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await q.edit_message_text(f"❌ {e}")
        return ConversationHandler.END
    return WAITING_PRODUCT_YEAR

async def products_month_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "tpcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
        return ConversationHandler.END
    if d.startswith("tmm_"):
        pr = d.split("_"); m, y = int(pr[1]), int(pr[2])
        f = datetime.date(y,m,1)
        l = datetime.date(y,12,31) if m==12 else datetime.date(y,m+1,1)-datetime.timedelta(days=1)
        nm = f"{MONTH_NAMES[m-1]} {y}"
        await q.edit_message_text(f"⏳ Товары за {nm}...")
        try:
            postings = await fetch_postings(f.isoformat(), l.isoformat())
            stats = aggregate_products(postings, f.isoformat(), l.isoformat())
            txt = format_top_products(stats, f"Товары за {nm}", 20) + "\n" + format_products_summary(stats)
            await q.edit_message_text(txt)
            await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await q.edit_message_text(f"❌ {e}")
        return ConversationHandler.END
    return WAITING_PRODUCT_MONTH

async def products_quarter_cb(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "tpcancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
        return ConversationHandler.END
    if d.startswith("tqq_"):
        pr = d.split("_"); qn, y = int(pr[1]), int(pr[2])
        sm = (qn-1)*3 + 1; em = qn*3
        f = datetime.date(y, sm, 1)
        l = datetime.date(y,12,31) if em==12 else datetime.date(y,em+1,1)-datetime.timedelta(days=1)
        nm = f"{qn} квартал {y}"
        await q.edit_message_text(f"⏳ Товары за {nm}...")
        try:
            postings = await fetch_postings(f.isoformat(), l.isoformat())
            stats = aggregate_products(postings, f.isoformat(), l.isoformat())
            txt = format_top_products(stats, f"Товары за {nm}", 20) + "\n" + format_products_summary(stats)
            await q.edit_message_text(txt)
            await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
        except Exception as e:
            write_log(f"❌ {e}"); await q.edit_message_text(f"❌ {e}")
        return ConversationHandler.END
    return WAITING_PRODUCT_QUARTER

async def products_custom_start(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "ts_cancel":
        await q.edit_message_text("Отменено.")
        await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
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
            if not ok:
                await q.edit_message_text(r); return WAITING_PRODUCT_PERIOD_START
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
        await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
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
            if not ds:
                await q.edit_message_text("❌ потеряна дата"); return ConversationHandler.END
            ok, r = validate_period(ds, de)
            if not ok:
                await q.edit_message_text(r); return WAITING_PRODUCT_PERIOD_END
            nm = f"{ds} – {de}"
            await q.edit_message_text(f"⏳ Товары за {nm}...")
            try:
                postings = await fetch_postings(ds, de)
                stats = aggregate_products(postings, ds, de)
                txt = format_top_products(stats, f"Товары за {nm}", 20) + "\n" + format_products_summary(stats)
                await q.edit_message_text(txt)
                await q.message.reply_text("Выберите действие:", reply_markup=main_kb())
            except Exception as e:
                write_log(f"❌ {e}"); await q.edit_message_text(f"❌ {e}")
            context.user_data.pop('tp_start', None)
            return ConversationHandler.END
    return WAITING_PRODUCT_PERIOD_END

async def cancel(update, context):
    await update.message.reply_text("Отменено.", reply_markup=main_kb())
    return ConversationHandler.END

# ==================== ЗАПУСК ====================
def main():
    if not validate_env_vars(): sys.exit(1)
    write_log(f"🚀 Запуск (v{VERSION})")
    write_log(f"✅ OZON_CLIENT_ID: {mask_secret(OZON_CLIENT_ID)}")
    write_log(f"✅ OZON_API_KEY: {mask_secret(OZON_API_KEY)}")
    write_log(f"✅ TELEGRAM_BOT_TOKEN: {mask_secret(TELEGRAM_BOT_TOKEN)}")
    write_log(f"✅ ADMIN_CHAT_ID: {ADMIN_CHAT_ID}")
    write_log(f"✅ Disk cache: {DISK_CACHE_DIR}")
    update_version_history(VERSION, CHANGELOG_MESSAGE)

    app = (Application.builder()
           .token(TELEGRAM_BOT_TOKEN)
           .connect_timeout(30.0).read_timeout(30.0).write_timeout(30.0)
           .post_init(init_http_session).post_shutdown(close_http_session).build())

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("version", version_command))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(MessageHandler(
        filters.Text(["📊 Продажи за сегодня"]), today_report))
    app.add_handler(MessageHandler(
        filters.Text(["📦 Топ товаров за сегодня"]), top_today))

    conv_date = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📅 Выбрать дату"), date_menu)],
        states={WAITING_DATE_SINGLE: [CallbackQueryHandler(date_cb)]},
        fallbacks=[CommandHandler("cancel", cancel)])

    conv_period = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("📆 Выбрать период"), period_menu)],
        states={
            WAITING_PERIOD_TYPE: [CallbackQueryHandler(period_type_cb)],
            WAITING_PERIOD_YEAR: [CallbackQueryHandler(period_year_cb)],
            WAITING_PERIOD_MONTH: [CallbackQueryHandler(period_month_cb)],
            WAITING_PERIOD_QUARTER: [CallbackQueryHandler(period_quarter_cb)],
            WAITING_PERIOD_START: [CallbackQueryHandler(custom_start_cb)],
            WAITING_PERIOD_END: [CallbackQueryHandler(custom_end_cb)],
            WAITING_YEAR_SELECT: [CallbackQueryHandler(period_year_cb)],
        },
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
            WAITING_PRODUCT_YEAR_SELECT: [CallbackQueryHandler(products_year_cb)],
        },
        fallbacks=[CommandHandler("cancel", cancel)])

    app.add_handler(conv_date)
    app.add_handler(conv_period)
    app.add_handler(conv_prod)

    write_log("🚀 Бот готов.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, timeout=30)

if __name__ == "__main__":
    main()
