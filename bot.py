# ============================================================
# ОТЧЁТ ПО РЕКЛАМЕ — раздел "Текущий месяц"
# Тестовый скрипт. После проверки переносим в основной бот.
# ============================================================
import asyncio
import aiohttp
import datetime
import json
import os
from typing import List, Dict, Optional

# ==================== КОНФИГ ====================
OZON_PERFORMANCE_CLIENT_ID = os.getenv("OZON_PERFORMANCE_CLIENT_ID")
OZON_PERFORMANCE_CLIENT_SECRET = os.getenv("OZON_PERFORMANCE_CLIENT_SECRET")
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
MOSCOW_TZ = datetime.timezone(datetime.timedelta(hours=3))


def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ==================== HTTP ====================
_http_session = None
_perf_rate_lock = asyncio.Lock()
_perf_last = 0


async def init_session():
    global _http_session
    _http_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60),
        connector=aiohttp.TCPConnector(limit=20, limit_per_host=5, ttl_dns_cache=300),
    )


async def close_session():
    if _http_session:
        await _http_session.close()


async def _perf_rate_acquire():
    """1 запрос в секунду к Performance API."""
    global _perf_last
    async with _perf_rate_lock:
        now = asyncio.get_event_loop().time()
        wait = 1.0 - (now - _perf_last)
        if wait > 0:
            await asyncio.sleep(wait)
        _perf_last = asyncio.get_event_loop().time()


async def api_get(url, headers, params=None, kind='perf'):
    for attempt in range(3):
        try:
            if kind == 'perf':
                await _perf_rate_acquire()
            async with _http_session.get(url, headers=headers, params=params) as r:
                body = await r.text()
                if r.status == 429:
                    w = 2 * (2 ** attempt)
                    log(f"⚠️ 429, ждём {w}с")
                    await asyncio.sleep(w)
                    continue
                if r.status >= 400:
                    log(f"❌ GET {url} → {r.status}: {body[:200]}")
                    raise aiohttp.ClientResponseError(r.request_info, r.history,
                        status=r.status, message=body[:200])
                return json.loads(body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == 2:
                log(f"❌ GET failed: {e}")
                raise
            await asyncio.sleep(2 * (attempt + 1))
    raise Exception("API failed")


async def api_post(url, headers, payload=None, kind='seller'):
    for attempt in range(3):
        try:
            if kind == 'perf':
                await _perf_rate_acquire()
            async with _http_session.post(url, headers=headers, json=payload) as r:
                body = await r.text()
                if r.status == 429:
                    w = 2 * (2 ** attempt)
                    log(f"⚠️ 429, ждём {w}с")
                    await asyncio.sleep(w)
                    continue
                if r.status >= 400:
                    log(f"❌ POST {url} → {r.status}: {body[:200]}")
                    raise aiohttp.ClientResponseError(r.request_info, r.history,
                        status=r.status, message=body[:200])
                return json.loads(body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == 2:
                log(f"❌ POST failed: {e}")
                raise
            await asyncio.sleep(2 * (attempt + 1))
    raise Exception("API failed")


# ==================== ПАРСИНГ ЧИСЕЛ ====================
def to_float(val, default=0.0):
    """'705,69' → 705.69, '0,03' → 0.03, None → default."""
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def to_int(val, default=0):
    try:
        return int(to_float(val, 0))
    except Exception:
        return default


# ==================== PERFORMANCE API ====================
async def get_perf_token():
    if not OZON_PERFORMANCE_CLIENT_ID or not OZON_PERFORMANCE_CLIENT_SECRET:
        log("❌ Performance credentials не заданы")
        return None
    url = "https://api-performance.ozon.ru/api/client/token"
    payload = {
        "client_id": OZON_PERFORMANCE_CLIENT_ID,
        "client_secret": OZON_PERFORMANCE_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }
    try:
        data = await api_post(url, {"Content-Type": "application/json"}, payload, kind='perf')
        token = data.get("access_token")
        if token:
            log("✅ Performance токен получен")
        return token
    except Exception as e:
        log(f"❌ Токен Performance: {e}")
        return None


async def fetch_campaigns(token) -> List[Dict]:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        data = await api_get("https://api-performance.ozon.ru/api/client/campaign",
                             headers, kind='perf')
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("list", data.get("campaigns", []))
    except Exception as e:
        log(f"❌ Кампании: {e}")
    return []


async def fetch_campaign_objects(token, campaign_id: str) -> List[str]:
    """Возвращает список SKU, участвующих в кампании."""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api-performance.ozon.ru/api/client/campaign/{campaign_id}/objects"
    try:
        data = await api_get(url, headers, kind='perf')
        result = []
        if isinstance(data, dict):
            for item in data.get("list", []):
                if isinstance(item, dict) and item.get("id"):
                    result.append(str(item["id"]))
        return result
    except Exception:
        return []


async def fetch_stats(token, campaign_ids: List[str], date_from: str, date_to: str) -> List[Dict]:
    """
    Одна строка на кампанию. Возвращает список словарей вида:
    {id, title, moneySpent, views, clicks, ctr, clickPrice, orders, ordersMoney, drr, toCart, ...}
    """
    if not campaign_ids:
        return []
    headers = {"Authorization": f"Bearer {token}"}
    url = "https://api-performance.ozon.ru/api/client/statistics/campaign/product/json"
    # Передаём как repeated params
    params = [("dateFrom", date_from), ("dateTo", date_to)]
    for cid in campaign_ids:
        params.append(("campaignIds", cid))
    # aiohttp не принимает list of tuples в params — строим вручную
    query = "&".join(f"{k}={v}" for k, v in params)
    full_url = f"{url}?{query}"
    try:
        data = await api_get(full_url, headers, kind='perf')
        rows = data.get("rows", []) if isinstance(data, dict) else []
        log(f"📊 Статистика по {len(rows)} кампаниям за {date_from}–{date_to}")
        return rows
    except Exception as e:
        log(f"❌ Статистика: {e}")
        return []


# ==================== SELLER API (заказы) ====================
async def fetch_postings(date_from: str, date_to: str) -> List[Dict]:
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
            data = await api_post("https://api-seller.ozon.ru/v3/posting/fbo/list",
                                  headers, payload, kind='seller')
        except Exception as e:
            log(f"❌ FBO: {e}")
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
    log(f"📦 Заказов: {len(all_p)} за {date_from}–{date_to}")
    return all_p


def sum_sales_by_skus(postings: List[Dict], skus: set) -> float:
    """Суммирует продажи по указанным SKU."""
    total = 0.0
    for p in postings:
        for prod in p.get("products", []):
            sku = str(prod.get("sku", ""))
            if sku in skus:
                qty = int(prod.get("quantity", 0))
                try:
                    price = float(str(prod.get("price", "0")).replace(",", "."))
                except Exception:
                    price = 0.0
                total += price * qty
    return total


# ==================== АГРЕГАЦИЯ ====================
def aggregate_stats_rows(rows: List[Dict], skus_by_campaign: Dict[str, List[str]],
                        postings: List[Dict]) -> Dict:
    """Агрегирует строки статистики в суммарный блок."""
    total = {
        "avg_bid": 0.0, "avg_cpc": 0.0, "ad_sales": 0.0, "total_sales": 0.0,
        "expense": 0.0, "drr_ad": None, "drr_total": None,
        "impressions": 0, "clicks": 0, "carts": 0, "ctr": 0.0,
        "orders": 0,
    }
    total_bid = 0.0
    bid_count = 0
    total_expense = 0.0
    total_ad_sales = 0.0
    total_views = 0
    total_clicks = 0
    total_carts = 0
    total_orders = 0
    all_skus = set()

    for r in rows:
        cid = str(r.get("id", ""))
        bid = to_float(r.get("clickPrice"))
        if bid > 0:
            total_bid += bid
            bid_count += 1
        total_expense += to_float(r.get("moneySpent"))
        total_ad_sales += to_float(r.get("ordersMoney"))
        total_views += to_int(r.get("views"))
        total_clicks += to_int(r.get("clicks"))
        total_carts += to_int(r.get("toCart"))
        total_orders += to_int(r.get("orders"))
        all_skus.update(skus_by_campaign.get(cid, []))

    # Продано товаров ВСЕГО = сумма продаж по всем рекламируемым SKU
    total_sales_all = sum_sales_by_sku_set(postings, all_skus) if all_skus else 0.0

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


def sum_sales_by_sku_set(postings: List[Dict], skus: set) -> float:
    total = 0.0
    for p in postings:
        for prod in p.get("products", []):
            if str(prod.get("sku", "")) in skus:
                qty = int(prod.get("quantity", 0))
                try:
                    price = float(str(prod.get("price", "0")).replace(",", "."))
                except Exception:
                    price = 0.0
                total += price * qty
    return total


def aggregate_by_campaign(rows: List[Dict], skus_by_campaign: Dict[str, List[str]],
                         postings: List[Dict]) -> List[Dict]:
    """Обрабатывает строки статистики: каждая строка — одна кампания."""
    result = []
    for r in rows:
        cid = str(r.get("id", ""))
        expense = to_float(r.get("moneySpent"))
        ad_sales = to_float(r.get("ordersMoney"))
        views = to_int(r.get("views"))
        clicks = to_int(r.get("clicks"))
        carts = to_int(r.get("toCart"))
        orders = to_int(r.get("orders"))
        click_price = to_float(r.get("clickPrice"))
        skus = skus_by_campaign.get(cid, [])
        total_sales = sum_sales_by_sku_set(postings, set(skus)) if skus else 0.0
        result.append({
            "campaign_id": cid,
            "campaign_name": r.get("title", cid),
            "skus": skus,
            "avg_bid": click_price,
            "avg_cpc": expense / clicks if clicks > 0 else 0.0,
            "ad_sales": ad_sales,
            "total_sales": total_sales,
            "expense": expense,
            "drr_ad": (expense / ad_sales * 100) if ad_sales > 0 else None,
            "drr_total": (expense / total_sales * 100) if total_sales > 0 else None,
            "impressions": views,
            "clicks": clicks,
            "carts": carts,
            "orders": orders,
            "ctr": (clicks / views * 100) if views > 0 else 0.0,
        })
    return result


# ==================== СРАВНЕНИЕ ====================
def indicator(cur, prev, better_is_higher=True):
    """🟢 лучше / 🔴 хуже / '' нет данных."""
    if cur is None or prev is None:
        return ""
    if prev == 0 and cur == 0:
        return ""
    if prev == 0:
        return "🟢" if cur > 0 else "🔴"
    delta = (cur - prev) / abs(prev)
    if delta == 0:
        return ""
    if better_is_higher:
        return "🟢" if delta > 0 else "🔴"
    return "🟢" if delta < 0 else "🔴"


def fmt_f(val, suffix=""):
    if val is None:
        return f"—{suffix}"
    return f"{val:,.2f}".replace(",", " ") + suffix


def fmt_i(val):
    if val is None:
        return "—"
    return f"{int(val):,}".replace(",", " ")


def line(label, cur, prev, fmt_func, better_is_higher=True, suffix="", no_indicator=False):
    ind = "" if no_indicator else indicator(cur, prev, better_is_higher)
    prefix = f"{ind} " if ind else "   "
    cur_str = fmt_func(cur)
    prev_str = fmt_func(prev)
    return f"{prefix}{label}: {cur_str} vs {prev_str}"


def fmt_pct(val):
    if val is None:
        return "—"
    return f"{val:.2f}%"


# ==================== ФОРМИРОВАНИЕ ОТЧЁТА ====================
def build_report(cur_total, prev_total, cur_camps, prev_camps, period_label):
    lines = []
    lines.append(f"📊 Статистика за текущий месяц на {period_label} (без сегодня):")
    lines.append("Суммарные данные по всем рекламным кампаниям")
    lines.append("")
    lines.append(line("Ваша ставка, руб.", cur_total["avg_bid"], prev_total["avg_bid"],
                      fmt_f, better_is_higher=False, no_indicator=True))
    lines.append(line("Средняя стоимость клика, руб.", cur_total["avg_cpc"], prev_total["avg_cpc"],
                      fmt_f, better_is_higher=False))
    lines.append(line("Продано товаров в рекламной компании, руб.",
                      cur_total["ad_sales"], prev_total["ad_sales"],
                      fmt_f, better_is_higher=True))
    lines.append(line("Продано товаров ВСЕГО, руб.", cur_total["total_sales"], prev_total["total_sales"],
                      fmt_f, better_is_higher=True))
    lines.append(line("Расход, руб.", cur_total["expense"], prev_total["expense"],
                      fmt_f, better_is_higher=False))
    lines.append(line("ДРР в рекламной компании, %", cur_total["drr_ad"], prev_total["drr_ad"],
                      fmt_pct, better_is_higher=False))
    lines.append(line("ДРР ОБЩИЙ, %", cur_total["drr_total"], prev_total["drr_total"],
                      fmt_pct, better_is_higher=False))
    lines.append(line("Показы, кол-во.", cur_total["impressions"], prev_total["impressions"],
                      fmt_i, better_is_higher=True))
    lines.append(line("Клики, кол-во.", cur_total["clicks"], prev_total["clicks"],
                      fmt_i, better_is_higher=True))
    lines.append(line("Добавления в корзину, кол-во.", cur_total["carts"], prev_total["carts"],
                      fmt_i, better_is_higher=True))
    lines.append(line("CTR, %", cur_total["ctr"], prev_total["ctr"],
                      fmt_pct, better_is_higher=True))
    lines.append("")
    lines.append("ПО КОМПАНИЯМ")
    lines.append("")

    prev_map = {c["campaign_id"]: c for c in prev_camps}

    for c in cur_camps:
        p = prev_map.get(c["campaign_id"], {})
        lines.append(f"Название рекламной компании: {c['campaign_name']}")
        sku_str = ", ".join(c["skus"][:5]) if c["skus"] else "—"
        if len(c["skus"]) > 5:
            sku_str += f" … (+{len(c['skus']) - 5})"
        lines.append(f"СКЮ/АРТИКУЛ: {sku_str}")
        lines.append("")
        lines.append(line("Ваша ставка, руб.", c["avg_bid"], p.get("avg_bid"),
                          fmt_f, better_is_higher=False, no_indicator=True))
        lines.append(line("Средняя стоимость клика, руб.", c["avg_cpc"], p.get("avg_cpc"),
                          fmt_f, better_is_higher=False))
        lines.append(line("Продано товаров в рекламной компании, руб.",
                          c["ad_sales"], p.get("ad_sales"), fmt_f, better_is_higher=True))
        lines.append(line("Продано товаров ВСЕГО, руб.",
                          c["total_sales"], p.get("total_sales"), fmt_f, better_is_higher=True))
        lines.append(line("Расход, руб.", c["expense"], p.get("expense"),
                          fmt_f, better_is_higher=False))
        lines.append(line("ДРР в рекламной компании, %", c["drr_ad"], p.get("drr_ad"),
                          fmt_pct, better_is_higher=False))
        lines.append(line("ДРР ОБЩИЙ, %", c["drr_total"], p.get("drr_total"),
                          fmt_pct, better_is_higher=False))
        lines.append(line("Показы, кол-во.", c["impressions"], p.get("impressions"),
                          fmt_i, better_is_higher=True))
        lines.append(line("Клики, кол-во.", c["clicks"], p.get("clicks"),
                          fmt_i, better_is_higher=True))
        lines.append(line("Добавления в корзину, кол-во.", c["carts"], p.get("carts"),
                          fmt_i, better_is_higher=True))
        lines.append(line("CTR, %", c["ctr"], p.get("ctr"),
                          fmt_pct, better_is_higher=True))
        lines.append("")

    return "\n".join(lines)


# ==================== ОСНОВНАЯ ФУНКЦИЯ ====================
async def generate_ad_report_month():
    await init_session()
    try:
        today = datetime.datetime.now(MOSCOW_TZ).date()
        yesterday = today - datetime.timedelta(days=1)
        cur_from = today.replace(day=1)
        cur_to = yesterday

        # Аналогичный период предыдущего месяца
        days_count = (cur_to - cur_from).days + 1
        prev_month_end = cur_from - datetime.timedelta(days=1)
        prev_from = prev_month_end.replace(day=1)
        prev_to = prev_from + datetime.timedelta(days=days_count - 1)
        if prev_to > prev_month_end:
            prev_to = prev_month_end

        log(f"Текущий:   {cur_from} – {cur_to}")
        log(f"Предыдущий: {prev_from} – {prev_to}")

        token = await get_perf_token()
        if not token:
            return "❌ Не удалось получить токен Performance API"

        campaigns = await fetch_campaigns(token)
        log(f"📋 Всего кампаний: {len(campaigns)}")
        if not campaigns:
            return "❌ Кампании не найдены"

        # Собираем SKU по каждой кампании
        skus_by_campaign = {}
        for c in campaigns:
            cid = str(c.get("id", ""))
            if not cid:
                continue
            objs = await fetch_campaign_objects(token, cid)
            if objs:
                skus_by_campaign[cid] = objs
        log(f"🗂 SKU собраны для {len(skus_by_campaign)} кампаний")

        campaign_ids = [str(c.get("id")) for c in campaigns if c.get("id")]

        # Параллельно: статистика + postings за оба периода
        cur_rows, prev_rows, cur_postings, prev_postings = await asyncio.gather(
            fetch_stats(token, campaign_ids, cur_from.isoformat(), cur_to.isoformat()),
            fetch_stats(token, campaign_ids, prev_from.isoformat(), prev_to.isoformat()),
            fetch_postings(cur_from.isoformat(), cur_to.isoformat()),
            fetch_postings(prev_from.isoformat(), prev_to.isoformat()),
        )

        cur_total = aggregate_stats_rows(cur_rows, skus_by_campaign, cur_postings)
        prev_total = aggregate_stats_rows(prev_rows, skus_by_campaign, prev_postings)
        cur_camps = aggregate_by_campaign(cur_rows, skus_by_campaign, cur_postings)
        prev_camps = aggregate_by_campaign(prev_rows, skus_by_campaign, prev_postings)

        # Метка даты
        period_label = cur_to.strftime("%d.%m.%Y")
        return build_report(cur_total, prev_total, cur_camps, prev_camps, period_label)
    finally:
        await close_session()


if __name__ == "__main__":
    report = asyncio.run(generate_ad_report_month())
    print("\n" + "=" * 70)
    print(report)
    print("=" * 70)
