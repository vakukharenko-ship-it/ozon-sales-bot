import datetime
import json
import os
import time
import asyncio
import aiohttp
import warnings
import sys
from typing import Optional, List, Dict
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)
from telegram.warnings import PTBUserWarning

warnings.filterwarnings("ignore", category=PTBUserWarning)

# ==================== ВЕРСИЯ ====================
VERSION = "2.3.8"
CHANGELOG_MESSAGE = "Детальное логирование отгрузок, попытка FBS если FBO пусто, логирование финансов."

# ==================== КОНСТАНТЫ ====================
API_TIMEOUT = 60
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY = 10
RATE_LIMIT_REQUESTS_PER_SECOND = 0.33
CACHE_TTL_SECONDS = 300
VERSION_HISTORY_FILE = "version_history.json"
LOG_FILE = "/app/data/ozon_log.txt"

# ==================== ГЛОБАЛЬНЫЕ ОБЪЕКТЫ ====================
_http_session = None
_rate_limiter = None
_api_semaphore = None
_cache_lock = asyncio.Lock()
_api_cache = {}
_cache_timestamps = {}

# ==================== КОНФИГУРАЦИЯ ====================
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
OZON_PERFORMANCE_CLIENT_ID = os.getenv("OZON_PERFORMANCE_CLIENT_ID")
OZON_PERFORMANCE_CLIENT_SECRET = os.getenv("OZON_PERFORMANCE_CLIENT_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID_STR = os.getenv("ADMIN_CHAT_ID")
ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_STR) if ADMIN_CHAT_ID_STR and ADMIN_CHAT_ID_STR.isdigit() else 0

OZON_POSTING_FBO_URL = "https://api-seller.ozon.ru/v3/posting/fbo/list"
OZON_POSTING_FBS_URL = "https://api-seller.ozon.ru/v3/posting/fbs/list"
OZON_FINANCE_ACCRUAL_BY_DAY_URL = "https://api-seller.ozon.ru/v1/finance/accrual/by-day"

MOSCOW_TZ = datetime.timezone(datetime.timedelta(hours=3))

# ---------- ЛОГИ ----------
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
            write_log(f"ℹ️ Версия {version} уже зарегистрирована.")
            return
    now = datetime.datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S")
    history.append({"version": version, "date": now, "message": message})
    try:
        with open(VERSION_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        write_log(f"✅ Добавлена запись в историю: {version}")
    except Exception as e:
        write_log(f"❌ Ошибка записи истории: {e}")

# ---------- УТИЛИТЫ ----------
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

# ==================== ИНИЦИАЛИЗАЦИЯ ====================
async def init_http_session(app):
    global _http_session, _rate_limiter, _api_semaphore
    _rate_limiter = RateLimiter()
    _api_semaphore = asyncio.Semaphore(1)
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    connector = aiohttp.TCPConnector(limit=50, limit_per_host=10, ttl_dns_cache=300)
    _http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    write_log(f"✅ HTTP-сессия инициализирована (v{VERSION})")

async def close_http_session(app):
    global _http_session
    if _http_session:
        await _http_session.close()
        write_log("🔒 HTTP-сессия закрыта.")

# ==================== API ====================
async def api_request_with_retry(url, headers, payload=None, method='POST'):
    global _http_session, _rate_limiter, _api_semaphore
    async with _api_semaphore:
        for attempt in range(API_RETRY_ATTEMPTS):
            try:
                await _rate_limiter.acquire()
                if method == 'POST':
                    async with _http_session.post(url, headers=headers, json=payload) as resp:
                        body = await resp.text()
                        if resp.status == 429:
                            wait_time = API_RETRY_DELAY * (2 ** attempt)
                            write_log(f"⚠️ 429, ждём {wait_time}с (попытка {attempt+1}/{API_RETRY_ATTEMPTS})")
                            await asyncio.sleep(wait_time)
                            continue
                        if resp.status >= 400:
                            write_log(f"❌ API {resp.status} {url}: {body[:500]}")
                            raise aiohttp.ClientResponseError(
                                resp.request_info, resp.history,
                                status=resp.status, message=body[:200]
                            )
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
                                status=resp.status, message=body[:200]
                            )
                        return json.loads(body)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == API_RETRY_ATTEMPTS - 1:
                    write_log(f"❌ API failed after {API_RETRY_ATTEMPTS} attempts: {e}")
                    raise
                write_log(f"⚠️ Request failed (attempt {attempt+1}/{API_RETRY_ATTEMPTS}): {e}")
                await asyncio.sleep(API_RETRY_DELAY * (attempt + 1))
        raise Exception("API request failed after retries")

# ---------- TOKEN PERFORMANCE ----------
async def get_performance_token():
    if not OZON_PERFORMANCE_CLIENT_ID or not OZON_PERFORMANCE_CLIENT_SECRET:
        return None
    url = "https://api-performance.ozon.ru/api/client/token"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    payload = {"client_id": OZON_PERFORMANCE_CLIENT_ID,
               "client_secret": OZON_PERFORMANCE_CLIENT_SECRET,
               "grant_type": "client_credentials"}
    try:
        data = await api_request_with_retry(url, headers, payload, method='POST')
        token = data.get("access_token")
        if token:
            write_log("✅ Токен Performance получен.")
            return token
        write_log(f"❌ Ошибка токена: {data}")
        return None
    except Exception as e:
        write_log(f"❌ Ошибка токена: {e}")
        return None

# ---------- ОТГРУЗКИ ----------
async def fetch_postings_from_endpoint(url, date_from, date_to, endpoint_name):
    """Запрашивает отгрузки с одного эндпоинта. Логирует структуру ответа."""
    headers = {"Client-Id": OZON_CLIENT_ID, "Api-Key": OZON_API_KEY, "Content-Type": "application/json"}
    since_iso = f"{date_from}T00:00:00Z"
    to_iso = f"{date_to}T23:59:59Z"
    all_postings = []
    offset = 0
    LIMIT = 100
    first_page = True
    while True:
        payload = {
            "dir": "ASC",
            "filter": {"since": since_iso, "to": to_iso},
            "limit": LIMIT,
            "offset": offset,
            "translit": False,
            "with": {"analytics_data": True, "financial_data": True}
        }
        try:
            data = await api_request_with_retry(url, headers, payload, method='POST')
        except Exception as e:
            write_log(f"❌ [{endpoint_name}] ошибка: {e}")
            break
        if first_page:
            keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            write_log(f"🔍 [{endpoint_name}] ключи ответа: {keys}")
            # Логируем первые 500 символов тела
            snippet = json.dumps(data, ensure_ascii=False)[:500]
            write_log(f"🔍 [{endpoint_name}] тело: {snippet}")
            first_page = False
        postings = data.get("result", [])
        if not postings:
            break
        all_postings.extend(postings)
        if len(postings) < LIMIT:
            break
        offset += LIMIT
    write_log(f"📦 [{endpoint_name}] загружено: {len(all_postings)} за {date_from}–{date_to}")
    return all_postings

async def fetch_postings(date_from, date_to):
    cache_key = f"postings_{date_from}_{date_to}"
    cached = await get_from_cache(cache_key)
    if cached is not None:
        return cached

    # Сначала FBO
    postings = await fetch_postings_from_endpoint(OZON_POSTING_FBO_URL, date_from, date_to, "FBO")
    # Если FBO пусто — пробуем FBS
    if not postings:
        write_log(f"ℹ️ FBO пусто, пробую FBS...")
        postings = await fetch_postings_from_endpoint(OZON_POSTING_FBS_URL, date_from, date_to, "FBS")

    await save_to_cache(cache_key, postings)
    return postings

# ---------- РЕКЛАМА ----------
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
        data = await api_request_with_retry(url, headers, params, method='GET')
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
        write_log(f"❌ Ошибка рекламы: {e}")
        return 0.0

# ---------- ФИНАНСЫ ----------
async def fetch_finance_accruals_by_day(date_str: str) -> List[Dict]:
    headers = {"Client-Id": OZON_CLIENT_ID, "Api-Key": OZON_API_KEY, "Content-Type": "application/json"}
    payload = {"date": date_str}
    all_accruals = []
    first_page = True
    while True:
        try:
            data = await api_request_with_retry(OZON_FINANCE_ACCRUAL_BY_DAY_URL, headers, payload, method='POST')
        except Exception as e:
            write_log(f"❌ Финансы {date_str}: {e}")
            break
        if first_page:
            keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            write_log(f"🔍 [FIN] {date_str} ключи: {keys}")
            snippet = json.dumps(data, ensure_ascii=False)[:400]
            write_log(f"🔍 [FIN] {date_str} тело: {snippet}")
            first_page = False
        accruals = data.get("accruals", [])
        if not accruals:
            break
        all_accruals.extend(accruals)
        last_id = data.get("last_id")
        if last_id is not None:
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
    all_accruals = []
    current = start_dt
    total_days = (end_dt - start_dt).days + 1
    day_idx = 0
    while current <= end_dt:
        day_idx += 1
        write_log(f"💰 Финансы {date_from}–{date_to}: день {day_idx}/{total_days} ({current.isoformat()})")
        day_accruals = await fetch_finance_accruals_by_day(current.isoformat())
        all_accruals.extend(day_accruals)
        current += datetime.timedelta(days=1)
    write_log(f"💰 Всего начислений: {len(all_accruals)} за {date_from}–{date_to}")
    await save_to_cache(cache_key, all_accruals)
    return all_accruals

# ---------- АГРЕГАЦИЯ ----------
def aggregate_finance_expenses(accruals: List[Dict]) -> Dict[str, float]:
    result = {}
    for item in accruals:
        amount = item.get("amount", item.get("value", 0))
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            continue
        category = item.get("accrued_category") or item.get("name") or item.get("type") or "Прочее"
        if isinstance(category, dict):
            category = category.get("name", "Прочее")
        if amount < 0:
            result[category] = result.get(category, 0) + abs(amount)
        elif amount > 0 and any(kw in str(category).lower() for kw in
            ["комиссия", "доставка", "логистика", "эквайринг", "хранение", "возврат", "упаковка", "страхование", "утилизация", "потеря", "кросс-докинг"]):
            result[category] = result.get(category, 0) + abs(amount)
    return result

def aggregate_postings_multi(postings, ranges):
    results = {label: {"ordered_units": 0, "ordered_sum": 0.0, "delivered_units": 0,
                       "delivered_sum": 0.0, "canceled_units": 0, "canceled_sum": 0.0}
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
        t = dt_msk.time()
        for label, df, dt_, tl, ad in ranges:
            if df and date_str < df: continue
            if dt_ and date_str > dt_: continue
            if tl is not None and ad is not None and date_str == ad and t > tl: continue
            total_units = 0
            total_sum = 0.0
            for product in posting.get("products", []):
                qty = int(product.get("quantity", 0))
                try:
                    price = float(product.get("price", "0"))
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

# ---------- ФОРМАТИРОВАНИЕ ----------
def format_expense_block(expenses_by_type, title):
    if not expenses_by_type:
        return f"🔹 *{title}*\nНет данных о расходах.\n"
    total = sum(expenses_by_type.values())
    lines = [f"🔹 *{title}*", f"  *Итого:* {total:,.2f} ₽"]
    for category, amount in sorted(expenses_by_type.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"    {str(category)[:40]}: {amount:,.2f} ₽")
    return "\n".join(lines)

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

# ---------- ОТЧЁТ ----------
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

    write_log("📊 Начинаю формирование отчёта...")

    write_log("📦 Шаг 1/4: отгрузки за текущий месяц")
    postings_cur = await fetch_postings(cm_start_str, today_str)

    write_log("📦 Шаг 2/4: отгрузки за прошлый период")
    postings_prev = await fetch_postings(pm_start_str, pm_end_str)

    ranges_cur = [
        ("today", today_str, today_str, current_time, today_str),
        ("yesterday", yesterday_str, yesterday_str, current_time, yesterday_str),
        ("month", cm_start_str, today_str, current_time, today_str),
    ]
    ranges_prev = [("prev", pm_start_str, pm_end_str, current_time, pm_end_str)]

    agg_cur = aggregate_postings_multi(postings_cur, ranges_cur)
    agg_prev = aggregate_postings_multi(postings_prev, ranges_prev)

    today_m = agg_cur.get("today", {})
    yest_m = agg_cur.get("yesterday", {})
    month_m = agg_cur.get("month", {})
    prev_m = agg_prev.get("prev", {})

    write_log(f"📊 Сегодня: {today_m.get('ordered_units', 0)} шт, месяц: {month_m.get('ordered_units', 0)} шт")

    write_log("📢 Шаг 3/4: реклама")
    ad_today = await fetch_advertising_expense(today_str, today_str)
    ad_month = await fetch_advertising_expense(cm_start_str, today_str)

    write_log("💰 Шаг 4/4: финансы")
    fin_today = await fetch_finance_transactions(today_str, today_str)
    fin_month = await fetch_finance_transactions(cm_start_str, today_str)

    exp_today = aggregate_finance_expenses(fin_today)
    exp_month = aggregate_finance_expenses(fin_month)
    if ad_today > 0:
        exp_today["Оплата за клик"] = ad_today
    if ad_month > 0:
        exp_month["Оплата за клик"] = ad_month

    def block_today():
        os_ = today_m.get("ordered_sum", 0)
        ou_ = today_m.get("ordered_units", 0)
        cs_ = today_m.get("canceled_sum", 0)
        cu_ = today_m.get("canceled_units", 0)
        d_os = fmt_pct(calc_delta(os_, yest_m.get("ordered_sum", 0)))
        d_ou = fmt_pct(calc_delta(ou_, yest_m.get("ordered_units", 0)))
        d_cs = fmt_pct(calc_delta(cs_, yest_m.get("canceled_sum", 0)))
        d_cu = fmt_pct(calc_delta(cu_, yest_m.get("canceled_units", 0)))
        du = today_m.get("delivered_units", 0)
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
        os_ = month_m.get("ordered_sum", 0)
        ou_ = month_m.get("ordered_units", 0)
        ds_ = month_m.get("delivered_sum", 0)
        du_ = month_m.get("delivered_units", 0)
        cs_ = month_m.get("canceled_sum", 0)
        cu_ = month_m.get("canceled_units", 0)
        d_os = fmt_pct(calc_delta(os_, prev_m.get("ordered_sum", 0)))
        d_ou = fmt_pct(calc_delta(ou_, prev_m.get("ordered_units", 0)))
        d_ds = fmt_pct(calc_delta(ds_, prev_m.get("delivered_sum", 0)))
        d_du = fmt_pct(calc_delta(du_, prev_m.get("delivered_units", 0)))
        d_cs = fmt_pct(calc_delta(cs_, prev_m.get("canceled_sum", 0)))
        d_cu = fmt_pct(calc_delta(cu_, prev_m.get("canceled_units", 0)))
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
    report = "📊 *Продажи за сегодня*\n\n\n" + "\n\n".join(parts)
    write_log(f"✅ Отчёт сформирован, длина: {len(report)}")
    return report

# ==================== ТЕЛЕГРАМ ====================
def main_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📊 Продажи за сегодня")], [KeyboardButton("🔄 Обновить")]],
        resize_keyboard=True
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("❌ Нет доступа.", reply_markup=ReplyKeyboardRemove())
        return
    await update.message.reply_text(
        f"🤖 Версия бота: {VERSION}\n\nНажмите кнопку ниже, чтобы получить отчёт.",
        reply_markup=main_keyboard()
    )

async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🤖 Версия: {VERSION}")

async def today_report_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("❌ Нет доступа.")
        return
    progress_msg = await update.message.reply_text("⏳ Загружаю данные...")
    try:
        report = await build_today_report()
        await progress_msg.delete()
        await update.message.reply_text(report, parse_mode="Markdown")
    except Exception as e:
        write_log(f"❌ Ошибка формирования отчёта: {e}")
        await progress_msg.edit_text(f"❌ Ошибка: {e}")

# ==================== ЗАПУСК ====================
def main():
    if not validate_env_vars():
        sys.exit(1)
    write_log(f"🚀 Запуск (v{VERSION})")
    write_log(f"✅ OZON_CLIENT_ID: {mask_secret(OZON_CLIENT_ID)}")
    write_log(f"✅ OZON_API_KEY: {mask_secret(OZON_API_KEY)}")
    write_log(f"✅ TELEGRAM_BOT_TOKEN: {mask_secret(TELEGRAM_BOT_TOKEN)}")
    write_log(f"✅ ADMIN_CHAT_ID: {ADMIN_CHAT_ID}")
    if not OZON_PERFORMANCE_CLIENT_ID or not OZON_PERFORMANCE_CLIENT_SECRET:
        write_log("⚠️ PERFORMANCE_CLIENT_ID/SECRET не заданы — реклама будет 0.")
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
    app.add_handler(MessageHandler(
        filters.Text(["📊 Продажи за сегодня", "🔄 Обновить"]),
        today_report_handler
    ))

    write_log("🚀 Бот готов.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, timeout=30)

if __name__ == "__main__":
    main()
