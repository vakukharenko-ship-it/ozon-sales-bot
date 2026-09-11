# ============================================================
# ОТЧЁТ ПО РЕКЛАМЕ — раздел "Текущий месяц" (тестовая версия 2)
# ============================================================
import asyncio
import aiohttp
import datetime
import json
import os
from typing import List, Dict, Optional

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
    global _perf_last
    async with _perf_rate_lock:
        now = asyncio.get_event_loop().time()
        wait = 1.0 - (now - _perf_last)
        if wait > 0:
            await asyncio.sleep(wait)
        _perf_last = asyncio.get_event_loop().time()


async def api_get(url, headers, params=None, kind='perf', silent_404=False):
    for attempt in range(3):
        try:
            if kind == 'perf':
                await _perf_rate_acquire()
            async with _http_session.get(url, headers=headers, params=params) as r:
                body = await r.text()
                if r.status == 404 and silent_404:
                    return None
                if r.status == 429:
                    w = 2 * (2 ** attempt)
                    log(f"⚠️ 429, ждём {w}с")
                    await asyncio.sleep(w)
                    continue
                if r.status >= 400:
                    if not silent_404:
                        log(f"❌ GET {url} → {r.status}: {body[:200]}")
                    return None
                return json.loads(body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == 2:
                if not silent_404:
                    log(f"❌ GET failed: {e}")
                return None
            await asyncio.sleep(2 * (attempt + 1))
    return None


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
                    return None
                return json.loads(body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == 2:
                log(f"❌ POST failed: {e}")
                return None
            await asyncio.sleep(2 * (attempt + 1))
    return None


# ==================== ПАРСИНГ ЧИСЕЛ ====================
def to_float(val, default=0.0):
    """'705,69' → 705.69, {'amount': '1496'} → 1496.0"""
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, dict):
        val = val.get("amount", val.get("value", 0))
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


def extract_price(product) -> float:
    """Правильно извлекает цену из товара: dict или строка."""
    if not isinstance(product, dict):
        return 0.0
    price_val = product.get("price", 0)
    return to_float(price_val)


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
    data = await api_post(url, {"Content-Type": "application/json"}, payload, kind='perf')
    if data and data.get("access_token"):
        log("✅ Performance токен получен")
        return data["access_token"]
    log("❌ Не удалось получить Performance токен")
    return None


async def fetch_campaigns(token) -> List[Dict]:
    headers = {"Authorization": f"Bearer {token}"}
    data = await api_get("https://api-performance.ozon.ru/api/client/campaign", headers)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("list", data.get("campaigns", []))
    return []


async def fetch_campaign_objects(token, campaign_id: str) -> List[str]:
    """SKU-объекты кампании. 404 (нет объектов) — не ошибка, тихо пропускаем."""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api-performance.ozon.ru/api/client/campaign/{campaign_id}/objects"
    data = await api_get(url, headers, silent_404=True)
    result = []
    if isinstance(data, dict):
        for item in data.get("list", []):
            if isinstance(item, dict) and item.get("id"):
                result.append(str(item["id"]))
    return result


async def fetch_stats(token, campaign_ids: List[str], date_from: str, date_to: str) -> List[Dict]:
    if not campaign_ids:
        return []
    headers = {"Authorization": f"Bearer {token}"}
    base = "https://api-performance.ozon.ru/api/client/statistics/campaign/product/json"
    parts = [f"dateFrom={date_from}", f"dateTo={date_to}"]
    for cid in campaign_ids:
        parts.append(f"campaignIds={cid}")
    url = base + "?" + "&".join(parts)
    data = await api_get(url, headers)
    rows = data.get("rows", []) if isinstance(data, dict) else []
    log(f"📊 Статистика: {len(rows)} строк за {date_from}–{date_to}")
    return rows


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
        data = await api_post("https://api-seller.ozon.ru/v3/posting/fbo/list",
                              headers, payload, kind='seller')
        if not data:
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


def sum_sales_by_sku_set(postings: List[Dict], skus: set) -> float:
    """Сумма продаж по SKU. Корректно работает с price как dict."""
    if not skus:
        return 0.0
    total = 0.0
    matched_skus = set()
    for p in postings:
        for prod in p.get("products", []):
            sku = str(prod.get("sku", ""))
            if sku in skus:
                matched_skus.add(sku)
                qty = int(prod.get("quantity", 0))
                price = extract_price(prod)
                total += price * qty
    if matched_skus:
        log(f"   найдено заказов по {len(matched_skus)}/{len(skus)} SKU")
    return total


# ==================== АГРЕГАЦИЯ ====================
def aggregate_stats_rows(rows, skus_by_campaign, postings):
    total = {
        "avg_bid": 0.0, "avg_cpc": 0.0, "ad_sales": 0.0, "total_sales": 0.0,
        "expense": 0.0, "drr_ad": None, "drr_total": None,
        "impressions": 0, "clicks": 0, "carts": 0, "ctr": 0.0, "orders": 0,
    }
    if not rows:
        return total

    total_bid = 0.0; bid_count = 0
    total_expense = 0.0; total_ad_sales = 0.0
    total_views = 0; total_clicks = 0; total_carts = 0; total_orders = 0
    all_skus = set()

    for r in rows:
        cid = str(r.get("id", ""))
        bid = to_float(r.get("clickPrice"))
        if bid > 0:
            total_bid += bid; bid_count += 1
        total_expense += to_float(r.get("moneySpent"))
        total_ad_sales += to_float(r.get("ordersMoney"))
        total_views += to_int(r.get("views"))
        total_clicks += to_int(r.get("clicks"))
        total_carts += to_int(r.get("toCart"))
        total_orders += to_int(r.get("orders"))
        all_skus.update(skus_by_campaign.get(cid, []))

    total_sales_all = sum_sales_by_sku_set(postings, all_skus)

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


def aggregate_by_campaign(rows, skus_by_campaign, postings):
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
        total_sales = sum_sales_by_sku_set(postings, set(skus))
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


# ==================== ФОРМАТИРОВАНИЕ ====================
def indicator(cur, prev, better_is_higher=True):
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


def fmt_f(val):
    if val is None:
        return "—"
    return f"{val:,.2f}".replace(",", " ")


def fmt_i(val):
    if val is None:
        return "—"
    return f"{int(val):,}".replace(",", " ")


def fmt_pct(val):
    if val is None:
        return "—"
    return f"{val:.2f}%"


def line(label, cur, prev, fmt_func, better_is_higher=True, no_indicator=False, has_prev_data=True):
    """Строка отчёта. Если prev данных нет — выводим 'нет данных'."""
    cur_str = fmt_func(cur)

    if not has_prev_data:
        # За предыдущий период нет данных
        return f"   {label}: {cur_str} vs — (нет данных)"

    prev_str = fmt_func(prev)
    if no_indicator:
        return f"   {label}: {cur_str} vs {prev_str}"
    ind = indicator(cur, prev, better_is_higher)
    prefix = f"{ind} " if ind else "   "
    return f"{prefix}{label}: {cur_str} vs {prev_str}"


# ==================== ОТЧЁТ ====================
def build_report(cur_total, prev_total, cur_camps, prev_camps,
                 period_label, has_prev_data=True):
    lines = []
    lines.append(f"📊 Статистика за текущий месяц на {period_label} (без сегодня):")
    lines.append("Суммарные данные по всем рекламным кампаниям")
    lines.append("")

    def L(label, cur_key, fmt_func, better_is_higher=True, no_ind=False):
        return line(label, cur_total.get(cur_key), prev_total.get(cur_key),
                    fmt_func, better_is_higher, no_ind, has_prev_data)

    lines.append(L("Ваша ставка, руб.", "avg_bid", fmt_f, False, no_ind=True))
    lines.append(L("Средняя стоимость клика, руб.", "avg_cpc", fmt_f, False))
    lines.append(L("Продано товаров в рекламной компании, руб.", "ad_sales", fmt_f, True))
    lines.append(L("Продано товаров ВСЕГО, руб.", "total_sales", fmt_f, True))
    lines.append(L("Расход, руб.", "expense", fmt_f, False))
    lines.append(L("ДРР в рекламной компании, %", "drr_ad", fmt_pct, False))
    lines.append(L("ДРР ОБЩИЙ, %", "drr_total", fmt_pct, False))
    lines.append(L("Показы, кол-во.", "impressions", fmt_i, True))
    lines.append(L("Клики, кол-во.", "clicks", fmt_i, True))
    lines.append(L("Добавления в корзину, кол-во.", "carts", fmt_i, True))
    lines.append(L("CTR, %", "ctr", fmt_pct, True))
    lines.append("")
    lines.append("ПО КОМПАНИЯМ")
    lines.append("")

    prev_map = {c["campaign_id"]: c for c in prev_camps}

    # Показываем только кампании с активностью (показы > 0 или расход > 0)
    active = [c for c in cur_camps if c["impressions"] > 0 or c["expense"] > 0]

    if not active:
        lines.append("Нет активных кампаний в этом периоде.")
        return "\n".join(lines)

    for c in active:
        p = prev_map.get(c["campaign_id"], {})
        lines.append(f"Название рекламной компании: {c['campaign_name']}")
        sku_str = ", ".join(c["skus"][:5]) if c["skus"] else "—"
        if len(c["skus"]) > 5:
            sku_str += f" … (+{len(c['skus']) - 5})"
        lines.append(f"СКЮ/АРТИКУЛ: {sku_str}")
        lines.append("")

        def Lc(label, key, fmt_func, better_is_higher=True, no_ind=False):
            return line(label, c.get(key), p.get(key), fmt_func,
                        better_is_higher, no_ind, has_prev_data)

        lines.append(Lc("Ваша ставка, руб.", "avg_bid", fmt_f, False, no_ind=True))
        lines.append(Lc("Средняя стоимость клика, руб.", "avg_cpc", fmt_f, False))
        lines.append(Lc("Продано товаров в рекламной компании, руб.", "ad_sales", fmt_f, True))
        lines.append(Lc("Продано товаров ВСЕГО, руб.", "total_sales", fmt_f, True))
        lines.append(Lc("Расход, руб.", "expense", fmt_f, False))
        lines.append(Lc("ДРР в рекламной компании, %", "drr_ad", fmt_pct, False))
        lines.append(Lc("ДРР ОБЩИЙ, %", "drr_total", fmt_pct, False))
        lines.append(Lc("Показы, кол-во.", "impressions", fmt_i, True))
        lines.append(Lc("Клики, кол-во.", "clicks", fmt_i, True))
        lines.append(Lc("Добавления в корзину, кол-во.", "carts", fmt_i, True))
        lines.append(Lc("CTR, %", "ctr", fmt_pct, True))
        lines.append("")

    return "\n".join(lines)


# ==================== ГЛАВНАЯ ====================
async def generate_ad_report_month():
    await init_session()
    try:
        today = datetime.datetime.now(MOSCOW_TZ).date()
        yesterday = today - datetime.timedelta(days=1)
        cur_from = today.replace(day=1)
        cur_to = yesterday

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

        # SKU по кампаниям
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

        # Параллельно 4 запроса
        cur_rows, prev_rows, cur_postings, prev_postings = await asyncio.gather(
            fetch_stats(token, campaign_ids, cur_from.isoformat(), cur_to.isoformat()),
            fetch_stats(token, campaign_ids, prev_from.isoformat(), prev_to.isoformat()),
            fetch_postings(cur_from.isoformat(), cur_to.isoformat()),
            fetch_postings(prev_from.isoformat(), prev_to.isoformat()),
        )

        has_prev_data = bool(prev_rows) and any(to_int(r.get("views")) > 0 for r in prev_rows)

        cur_total = aggregate_stats_rows(cur_rows, skus_by_campaign, cur_postings)
        prev_total = aggregate_stats_rows(prev_rows, skus_by_campaign, prev_postings)
        cur_camps = aggregate_by_campaign(cur_rows, skus_by_campaign, cur_postings)
        prev_camps = aggregate_by_campaign(prev_rows, skus_by_campaign, prev_postings)

        period_label = cur_to.strftime("%d.%m.%Y")
        return build_report(cur_total, prev_total, cur_camps, prev_camps,
                           period_label, has_prev_data)
    finally:
        await close_session()


if __name__ == "__main__":
    report = asyncio.run(generate_ad_report_month())
    print("\n" + "=" * 70)
    print(report)
    print("=" * 70)
