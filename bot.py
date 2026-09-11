# ============================================================
# ОТЧЁТ ПО РЕКЛАМЕ — раздел "Текущий месяц"
# Отдельный тестовый скрипт. После проверки интегрируется в основной бот.
# ============================================================

import asyncio
import aiohttp
import datetime
import json
import os
import re
import hashlib
from typing import Optional, List, Dict, Tuple

# ==================== КОНФИГ (замените на свои значения) ====================
OZON_PERFORMANCE_CLIENT_ID = os.getenv("OZON_PERFORMANCE_CLIENT_ID")
OZON_PERFORMANCE_CLIENT_SECRET = os.getenv("OZON_PERFORMANCE_CLIENT_SECRET")
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")

MOSCOW_TZ = datetime.timezone(datetime.timedelta(hours=3))
DATA_DIR = "/app/data"
DISK_CACHE_DIR = "/app/data/cache"

# ==================== ЛОГИ ====================
def write_log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ==================== КЭШ (упрощённый, по дням) ====================
def _disk_path(key):
    h = hashlib.md5(key.encode()).hexdigest()[:16]
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', key)[:40]
    return os.path.join(DISK_CACHE_DIR, f"{safe}_{h}.json")

def disk_cache_get(key):
    path = _disk_path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return None

def disk_cache_set(key, value):
    try:
        os.makedirs(DISK_CACHE_DIR, exist_ok=True)
        with open(_disk_path(key), "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
    except Exception as e:
        write_log(f"⚠️ disk-cache: {e}")

# ==================== HTTP-СЕССИЯ ====================
_http_session = None
_perf_rate_lock = asyncio.Lock()
_perf_last_request = 0

async def init_session():
    global _http_session
    timeout = aiohttp.ClientTimeout(total=60)
    connector = aiohttp.TCPConnector(limit=20, limit_per_host=5, ttl_dns_cache=300)
    _http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)

async def close_session():
    global _http_session
    if _http_session:
        await _http_session.close()

async def _perf_rate_acquire():
    """Rate limiter для Performance API: 1 запрос в секунду."""
    global _perf_last_request
    async with _perf_rate_lock:
        now = asyncio.get_event_loop().time()
        wait = 1.0 - (now - _perf_last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _perf_last_request = asyncio.get_event_loop().time()

async def api_request_with_retry(url, headers, payload=None, method='POST', kind='perf'):
    """Универсальный запрос с retry. kind='perf' — для Performance API."""
    global _http_session
    for attempt in range(3):
        try:
            if kind == 'perf':
                await _perf_rate_acquire()
            if method == 'POST':
                async with _http_session.post(url, headers=headers, json=payload) as resp:
                    body = await resp.text()
                    if resp.status == 429:
                        w = 2 * (2 ** attempt)
                        write_log(f"⚠️ 429, ждём {w}с")
                        await asyncio.sleep(w)
                        continue
                    if resp.status >= 400:
                        write_log(f"❌ API {resp.status}: {body[:300]}")
                        raise aiohttp.ClientResponseError(
                            resp.request_info, resp.history,
                            status=resp.status, message=body[:200])
                    return json.loads(body)
            else:
                async with _http_session.get(url, headers=headers, params=payload) as resp:
                    body = await resp.text()
                    if resp.status == 429:
                        w = 2 * (2 ** attempt)
                        write_log(f"⚠️ 429, ждём {w}с")
                        await asyncio.sleep(w)
                        continue
                    if resp.status >= 400:
                        write_log(f"❌ API {resp.status}: {body[:300]}")
                        raise aiohttp.ClientResponseError(
                            resp.request_info, resp.history,
                            status=resp.status, message=body[:200])
                    return json.loads(body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == 2:
                write_log(f"❌ API failed: {e}")
                raise
            await asyncio.sleep(2 * (attempt + 1))
    raise Exception("API failed")

# ==================== PERFORMANCE TOKEN ====================
async def get_performance_token():
    if not OZON_PERFORMANCE_CLIENT_ID or not OZON_PERFORMANCE_CLIENT_SECRET:
        return None
    url = "https://api-performance.ozon.ru/api/client/token"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    payload = {
        "client_id": OZON_PERFORMANCE_CLIENT_ID,
        "client_secret": OZON_PERFORMANCE_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }
    try:
        data = await api_request_with_retry(url, headers, payload, method='POST', kind='perf')
        return data.get("access_token")
    except Exception as e:
        write_log(f"❌ Токен Performance: {e}")
        return None

# ==================== КАМПАНИИ ====================
async def fetch_campaigns():
    """Возвращает список кампаний: [{id, title, ...}, ...]"""
    cache_key = "perf_campaigns_list"
    cached = disk_cache_get(cache_key)
    if cached is not None:
        return cached
    token = await get_performance_token()
    if not token:
        return []
    url = "https://api-performance.ozon.ru/api/client/campaign"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        data = await api_request_with_retry(url, headers, method='GET', kind='perf')
        # Ответ может быть списком или {list: [...]}
        if isinstance(data, list):
            campaigns = data
        elif isinstance(data, dict):
            campaigns = data.get("list", data.get("campaigns", []))
        else:
            campaigns = []
        await disk_cache_set(cache_key, campaigns)
        write_log(f"📋 Кампаний получено: {len(campaigns)}")
        return campaigns
    except Exception as e:
        write_log(f"❌ Кампании: {e}")
        return []

# ==================== СТАТИСТИКА ПО КАМПАНИЯМ ====================
async def fetch_campaign_stats(campaign_ids: List[str], date_from: str, date_to: str) -> List[Dict]:
    """
    Запрашивает статистику по указанным кампаниям за период.
    Возвращает список строк отчёта (каждая строка — одна кампания/товар).
    """
    cache_key = f"perf_stats_{date_from}_{date_to}_{'_'.join(sorted(campaign_ids))}"
    cached = disk_cache_get(cache_key)
    if cached is not None:
        return cached
    token = await get_performance_token()
    if not token:
        return []
    # Используем метод /api/client/statistics/campaign/product/json
    url = "https://api-performance.ozon.ru/api/client/statistics/campaign/product/json"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    params = {"dateFrom": date_from, "dateTo": date_to}
    if campaign_ids:
        # Передаём как repeated query param
        params["campaignIds"] = campaign_ids
    try:
        data = await api_request_with_retry(url, headers, params, method='GET', kind='perf')
        # Ответ — JSON с полями, содержащими строки отчёта
        rows = []
        if isinstance(data, dict):
            # Ищем массив с данными
            for key in ("rows", "result", "data", "items"):
                if key in data and isinstance(data[key], list):
                    rows = data[key]
                    break
            if not rows and "content" in data:
                # Может быть вложенный объект
                inner = data["content"]
                if isinstance(inner, list):
                    rows = inner
                elif isinstance(inner, dict):
                    for key in ("rows", "result", "data", "items"):
                        if key in inner and isinstance(inner[key], list):
                            rows = inner[key]
                            break
        elif isinstance(data, list):
            rows = data
        await disk_cache_set(cache_key, rows)
        write_log(f"📊 Статистика кампаний: {len(rows)} строк за {date_from}–{date_to}")
        return rows
    except Exception as e:
        write_log(f"❌ Статистика кампаний: {e}")
        return []

# ==================== ЗАКАЗЫ (Seller API) ====================
async def fetch_postings(date_from: str, date_to: str) -> List[Dict]:
    """Загружает заказы FBO за период (для расчёта 'Продано товаров ВСЕГО')."""
    cache_key = f"postings_{date_from}_{date_to}"
    cached = disk_cache_get(cache_key)
    if cached is not None:
        return cached
    headers = {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }
    since = f"{date_from}T00:00:00Z"
    to = f"{date_to}T23:59:59Z"
    all_p = []
    cursor = ""
    LIMIT = 100
    while True:
        payload = {
            "dir": "ASC",
            "filter": {"since": since, "to": to},
            "limit": LIMIT,
            "translit": False,
            "with": {"analytics_data": True, "financial_data": True},
        }
        if cursor:
            payload["cursor"] = cursor
        try:
            data = await api_request_with_retry(
                "https://api-seller.ozon.ru/v3/posting/fbo/list",
                headers, payload, method='POST', kind='postings')
        except Exception as e:
            write_log(f"❌ FBO: {e}")
            break
        postings = data.get("postings", [])
        if not postings:
            break
        all_p.extend(postings)
        if not data.get("has_next"):
            break
        cursor = data.get("cursor", "")
        if not cursor or len(postings) < LIMIT:
            break
    await disk_cache_set(cache_key, all_p)
    write_log(f"📦 Заказов: {len(all_p)} за {date_from}–{date_to}")
    return all_p

# ==================== АГРЕГАЦИЯ ДАННЫХ ПО РЕКЛАМЕ ====================
def parse_money(val) -> float:
    if val is None:
        return 0.0
    if isinstance(val, dict):
        val = val.get("amount", val.get("value", 0))
    try:
        return float(str(val).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0

def extract_sku_from_stats_row(row: Dict) -> List[str]:
    """Извлекает список SKU из строки статистики кампании."""
    skus = []
    # Пробуем разные поля
    for key in ("sku", "skuList", "skus", "products"):
        val = row.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    sku = item.get("sku") or item.get("SKU")
                    if sku:
                        skus.append(str(sku))
                else:
                    skus.append(str(item))
        elif isinstance(val, str):
            skus.append(val)
    # Иногда sku может быть в поле "item"
    item = row.get("item")
    if isinstance(item, dict):
        sku = item.get("sku")
        if sku:
            skus.append(str(sku))
    return list(set(skus))

def aggregate_ad_stats(rows: List[Dict], postings: List[Dict]) -> Dict:
    """
    Агрегирует строки статистики рекламных кампаний.
    Возвращает словарь с суммарными показателями.
    """
    result = {
        "avg_bid": 0.0,
        "avg_cpc": 0.0,
        "ad_sales": 0.0,
        "total_sales": 0.0,
        "expense": 0.0,
        "drr_ad": None,
        "drr_total": None,
        "impressions": 0,
        "clicks": 0,
        "carts": 0,
        "ctr": 0.0,
    }
    total_bid = 0.0
    bid_count = 0
    total_expense = 0.0
    total_impressions = 0
    total_clicks = 0
    total_carts = 0
    ad_sales_total = 0.0
    skus_in_ads = set()

    for row in rows:
        # Ставка
        bid = parse_money(row.get("avgBid") or row.get("bid") or row.get("rate"))
        if bid > 0:
            total_bid += bid
            bid_count += 1

        # Расход
        expense = parse_money(row.get("expense") or row.get("cost") or row.get("spend"))
        total_expense += expense

        # Показы, клики, корзины
        total_impressions += int(row.get("impressions") or row.get("shows") or 0)
        total_clicks += int(row.get("clicks") or row.get("clicksCount") or 0)
        total_carts += int(row.get("carts") or row.get("addToCart") or row.get("cartAdds") or 0)

        # Заказы (продажи в рекламе)
        ad_sales = parse_money(row.get("ordersSum") or row.get("revenue") or row.get("adSales"))
        ad_sales_total += ad_sales

        # SKU
        skus_in_ads.update(extract_sku_from_stats_row(row))

    # Средняя ставка
    result["avg_bid"] = total_bid / bid_count if bid_count > 0 else 0.0

    # Средняя стоимость клика = расход / клики
    result["avg_cpc"] = total_expense / total_clicks if total_clicks > 0 else 0.0

    # Расход
    result["expense"] = total_expense

    # Показы, клики, корзины
    result["impressions"] = total_impressions
    result["clicks"] = total_clicks
    result["carts"] = total_carts

    # CTR = клики / показы * 100
    result["ctr"] = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0.0

    # Продано товаров в рекламной кампании (сумма заказов по рекламным SKU)
    # Считаем из postings: суммируем amount для товаров, чьи SKU в skus_in_ads
    total_sales_from_ads = 0.0
    for p in postings:
        for prod in p.get("products", []):
            sku = str(prod.get("sku", ""))
            if sku in skus_in_ads:
                qty = int(prod.get("quantity", 0))
                price = parse_money(prod.get("price"))
                total_sales_from_ads += price * qty

    result["ad_sales"] = ad_sales_total
    result["total_sales"] = total_sales_from_ads

    # ДРР в рекламной кампании = расход / ad_sales * 100
    result["drr_ad"] = (total_expense / ad_sales_total * 100) if ad_sales_total > 0 else None

    # ДРР общий = расход / total_sales * 100
    result["drr_total"] = (total_expense / total_sales_from_ads * 100) if total_sales_from_ads > 0 else None

    return result

def aggregate_by_campaign(rows: List[Dict], postings: List[Dict]) -> List[Dict]:
    """
    Группирует строки статистики по кампаниям и агрегирует каждую.
    Возвращает список словарей с полями кампании и агрегатами.
    """
    by_campaign: Dict[str, List[Dict]] = {}
    for row in rows:
        cid = str(row.get("campaignId") or row.get("campaign_id") or row.get("id") or "unknown")
        by_campaign.setdefault(cid, []).append(row)

    result = []
    for cid, c_rows in by_campaign.items():
        agg = aggregate_ad_stats(c_rows, postings)
        # Название кампании — ищем в первой строке
        title = c_rows[0].get("campaignName") or c_rows[0].get("campaign_title") or cid
        skus = set()
        for r in c_rows:
            skus.update(extract_sku_from_stats_row(r))
        result.append({
            "campaign_id": cid,
            "campaign_name": title,
            "skus": sorted(skus),
            **agg,
        })
    return result

# ==================== VS-СРАВНЕНИЕ ====================
def calc_delta(cur, prev):
    if prev == 0:
        return None
    try:
        return ((cur - prev) / abs(prev)) * 100
    except:
        return None

def fmt_pct(val):
    if val is None:
        return "∞"
    return f"+{val:.1f}%" if val > 0 else f"{val:.1f}%"

def indicator(cur, prev, better_is_higher=True):
    """🟢 если лучше, 🔴 если хуже."""
    if prev is None or cur is None:
        return ""
    if prev == 0:
        return "🟢" if cur > 0 else ""
    delta = calc_delta(cur, prev)
    if delta is None:
        return ""
    if better_is_higher:
        return "🟢" if delta > 0 else ("🔴" if delta < 0 else "")
    else:
        return "🟢" if delta < 0 else ("🔴" if delta > 0 else "")

def fmt_num(val):
    if val is None:
        return "0.00"
    return f"{val:,.2f}".replace(",", " ")

def fmt_int(val):
    return str(int(val)) if val else "0"

# ==================== ФОРМИРОВАНИЕ ОТЧЁТА ====================
def build_ad_report(current: Dict, previous: Dict,
                    current_by_campaign: List[Dict], previous_by_campaign: List[Dict]) -> str:
    """Формирует текст отчёта с VS и индикаторами."""

    def line(label: str, cur_val, prev_val, fmt_func=fmt_num,
             better_is_higher=True, suffix="") -> str:
        cur_str = fmt_func(cur_val) if cur_val is not None else "—"
        prev_str = fmt_func(prev_val) if prev_val is not None else "—"
        ind = indicator(cur_val, prev_val, better_is_higher)
        return f"{ind} {label}: {cur_str}{suffix} vs {prev_str}{suffix}"

    lines = []

    # --- Суммарный блок ---
    lines.append("Статистика за текущий месяц (без сегодня):")
    lines.append("Суммарные данные по всем рекламным кампаниям")
    lines.append("")
    lines.append(line("Ваша ставка, руб.", current.get("avg_bid"), previous.get("avg_bid"),
                      better_is_higher=False))
    lines.append(line("Средняя стоимость клика, руб.", current.get("avg_cpc"), previous.get("avg_cpc"),
                      better_is_higher=False))
    lines.append(line("Продано товаров в рекламной компании, руб.",
                      current.get("ad_sales"), previous.get("ad_sales"),
                      better_is_higher=True))
    lines.append(line("Продано товаров ВСЕГО, руб.", current.get("total_sales"),
                      previous.get("total_sales"), better_is_higher=True))
    lines.append(line("Расход, руб.", current.get("expense"), previous.get("expense"),
                      better_is_higher=False))
    lines.append(line("ДРР в рекламной компании, %", current.get("drr_ad"),
                      previous.get("drr_ad"),
                      fmt_func=lambda v: f"{v:.2f}" if v is not None else "∞",
                      better_is_higher=False, suffix="%"))
    lines.append(line("ДРР ОБЩИЙ, %", current.get("drr_total"), previous.get("drr_total"),
                      fmt_func=lambda v: f"{v:.2f}" if v is not None else "∞",
                      better_is_higher=False, suffix="%"))
    lines.append(line("Показы, кол-во.", current.get("impressions"),
                      previous.get("impressions"), fmt_func=fmt_int, better_is_higher=True))
    lines.append(line("Клики, кол-во.", current.get("clicks"),
                      previous.get("clicks"), fmt_func=fmt_int, better_is_higher=True))
    lines.append(line("Добавления в корзину, кол-во.", current.get("carts"),
                      previous.get("carts"), fmt_func=fmt_int, better_is_higher=True))
    lines.append(line("CTR, %", current.get("ctr"), previous.get("ctr"),
                      fmt_func=lambda v: f"{v:.2f}", better_is_higher=True, suffix="%"))
    lines.append("")

    # --- Блок по кампаниям ---
    lines.append("ПО КОМПАНИЯМ")
    lines.append("")

    # Создаём словарь предыдущих кампаний по id для быстрого поиска
    prev_map = {c["campaign_id"]: c for c in previous_by_campaign}

    for camp in current_by_campaign:
        cid = camp["campaign_id"]
        prev_camp = prev_map.get(cid, {})

        lines.append(f"Название рекламной компании: {camp['campaign_name']}")
        sku_str = ", ".join(camp["skus"][:5])
        if len(camp["skus"]) > 5:
            sku_str += " …"
        lines.append(f"СКЮ/АРТИКУЛ: {sku_str}")
        lines.append("")

        lines.append(line("Ваша ставка, руб.", camp.get("avg_bid"), prev_camp.get("avg_bid"),
                          better_is_higher=False))
        lines.append(line("Средняя стоимость клика, руб.", camp.get("avg_cpc"),
                          prev_camp.get("avg_cpc"), better_is_higher=False))
        lines.append(line("Продано товаров в рекламной компании, руб.",
                          camp.get("ad_sales"), prev_camp.get("ad_sales"),
                          better_is_higher=True))
        lines.append(line("Продано товаров ВСЕГО, руб.", camp.get("total_sales"),
                          prev_camp.get("total_sales"), better_is_higher=True))
        lines.append(line("Расход, руб.", camp.get("expense"), prev_camp.get("expense"),
                          better_is_higher=False))
        lines.append(line("ДРР в рекламной компании, %", camp.get("drr_ad"),
                          prev_camp.get("drr_ad"),
                          fmt_func=lambda v: f"{v:.2f}" if v is not None else "∞",
                          better_is_higher=False, suffix="%"))
        lines.append(line("ДРР ОБЩИЙ, %", camp.get("drr_total"), prev_camp.get("drr_total"),
                          fmt_func=lambda v: f"{v:.2f}" if v is not None else "∞",
                          better_is_higher=False, suffix="%"))
        lines.append(line("Показы, кол-во.", camp.get("impressions"),
                          prev_camp.get("impressions"), fmt_func=fmt_int, better_is_higher=True))
        lines.append(line("Клики, кол-во.", camp.get("clicks"),
                          prev_camp.get("clicks"), fmt_func=fmt_int, better_is_higher=True))
        lines.append(line("Добавления в корзину, кол-во.", camp.get("carts"),
                          prev_camp.get("carts"), fmt_func=fmt_int, better_is_higher=True))
        lines.append(line("CTR, %", camp.get("ctr"), prev_camp.get("ctr"),
                          fmt_func=lambda v: f"{v:.2f}", better_is_higher=True, suffix="%"))
        lines.append("")

    return "\n".join(lines)

# ==================== ОСНОВНАЯ ФУНКЦИЯ ====================
async def generate_ad_report_month():
    """
    Формирует отчёт по рекламе за текущий месяц (до вчера) vs аналогичный период предыдущего месяца.
    Возвращает текстовый отчёт.
    """
    await init_session()
    try:
        today = datetime.datetime.now(MOSCOW_TZ).date()
        yesterday = today - datetime.timedelta(days=1)

        # Текущий период: с 1-го числа текущего месяца по вчера
        current_from = today.replace(day=1)
        current_to = yesterday

        # Предыдущий период: аналогичный по длине
        # Длина текущего периода (в днях)
        days_count = (current_to - current_from).days + 1

        # Начало предыдущего месяца
        prev_month_end = current_from - datetime.timedelta(days=1)
        prev_month_start = prev_month_end.replace(day=1)
        # Конец предыдущего периода — на days_count-1 дней позже начала
        prev_to = prev_month_start + datetime.timedelta(days=days_count - 1)
        if prev_to > prev_month_end:
            prev_to = prev_month_end
        prev_from = prev_month_start

        date_cur_from = current_from.isoformat()
        date_cur_to = current_to.isoformat()
        date_prev_from = prev_from.isoformat()
        date_prev_to = prev_to.isoformat()

        write_log(f"📅 Текущий период: {date_cur_from} – {date_cur_to}")
        write_log(f"📅 Предыдущий период: {date_prev_from} – {date_prev_to}")

        # 1. Получаем список кампаний
        campaigns = await fetch_campaigns()
        if not campaigns:
            return "❌ Не удалось получить список рекламных кампаний."
        campaign_ids = [str(c.get("id") or c.get("campaignId") or c.get("campaign_id"))
                        for c in campaigns if c.get("id") or c.get("campaignId") or c.get("campaign_id")]
        # Убираем дубликаты и None
        campaign_ids = list(set([c for c in campaign_ids if c and c != "None"]))
        write_log(f"🆔 ID кампаний: {len(campaign_ids)}")

        # 2. Статистика за оба периода (параллельно)
        stats_cur_task = fetch_campaign_stats(campaign_ids, date_cur_from, date_cur_to)
        stats_prev_task = fetch_campaign_stats(campaign_ids, date_prev_from, date_prev_to)
        # 3. Заказы за оба периода (параллельно)
        postings_cur_task = fetch_postings(date_cur_from, date_cur_to)
        postings_prev_task = fetch_postings(date_prev_from, date_prev_to)

        stats_cur, stats_prev, postings_cur, postings_prev = await asyncio.gather(
            stats_cur_task, stats_prev_task, postings_cur_task, postings_prev_task)

        # 4. Агрегация
        agg_cur = aggregate_ad_stats(stats_cur, postings_cur)
        agg_prev = aggregate_ad_stats(stats_prev, postings_prev)
        by_campaign_cur = aggregate_by_campaign(stats_cur, postings_cur)
        by_campaign_prev = aggregate_by_campaign(stats_prev, postings_prev)

        # 5. Формирование отчёта
        report = build_ad_report(agg_cur, agg_prev, by_campaign_cur, by_campaign_prev)
        return report
    finally:
        await close_session()

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    report = asyncio.run(generate_ad_report_month())
    print("\n" + "=" * 60)
    print(report)
    print("=" * 60)
