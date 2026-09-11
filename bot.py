import asyncio
import aiohttp
import datetime
import json
import os

CID = os.getenv("OZON_PERFORMANCE_CLIENT_ID")
CSECRET = os.getenv("OZON_PERFORMANCE_CLIENT_SECRET")
MOSCOW_TZ = datetime.timezone(datetime.timedelta(hours=3))


def log(msg):
    print(msg, flush=True)


async def main():
    log("=== ДИАГНОСТИКА OZON PERFORMANCE API ===")
    log(f"CLIENT_ID: {CID[:4]}***")
    log(f"CLIENT_SECRET: {CSECRET[:4] if CSECRET else '—'}***")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        # === 1. Токен ===
        try:
            async with session.post(
                "https://api-performance.ozon.ru/api/client/token",
                json={"client_id": CID, "client_secret": CSECRET, "grant_type": "client_credentials"}
            ) as r:
                body = await r.text()
                log(f"\n=== [1] POST /api/client/token — status {r.status} ===")
                log(body[:500])
                if r.status != 200:
                    log("❌ Токен не получен, останавливаемся")
                    return
                token = json.loads(body)["access_token"]
        except Exception as e:
            log(f"❌ Ошибка токена: {e}")
            return

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        today = datetime.datetime.now(MOSCOW_TZ).date()
        yesterday = today - datetime.timedelta(days=1)
        date_from = today.replace(day=1).isoformat()
        date_to = yesterday.isoformat()
        log(f"\nПериод для статистики: {date_from} – {date_to}")

        # === 2. Список кампаний ===
        campaigns = []
        try:
            async with session.get(
                "https://api-performance.ozon.ru/api/client/campaign",
                headers=headers
            ) as r:
                body = await r.text()
                log(f"\n=== [2] GET /api/client/campaign — status {r.status} ===")
                log(body[:3000])
                try:
                    data = json.loads(body)
                    if isinstance(data, list):
                        campaigns = data
                    elif isinstance(data, dict):
                        campaigns = data.get("list", data.get("campaigns", data.get("data", [])))
                except Exception as e:
                    log(f"⚠️ Не удалось разобрать JSON: {e}")
        except Exception as e:
            log(f"❌ Ошибка кампаний: {e}")

        if not campaigns:
            log("❌ Кампаний нет, останавливаемся")
            return

        log(f"\n📋 Всего кампаний: {len(campaigns)}")
        log(f"🔎 Первая кампания целиком (JSON):")
        log(json.dumps(campaigns[0], ensure_ascii=False, indent=2))

        # ID первой кампании
        first_id = (campaigns[0].get("id")
                    or campaigns[0].get("campaignId")
                    or campaigns[0].get("campaign_id"))
        log(f"\n🆔 ID первой кампании: {first_id}")

        # === 3. Объекты кампании ===
        try:
            async with session.get(
                f"https://api-performance.ozon.ru/api/client/campaign/{first_id}/objects",
                headers=headers
            ) as r:
                body = await r.text()
                log(f"\n=== [3] GET /api/client/campaign/{first_id}/objects — status {r.status} ===")
                log(body[:3000])
        except Exception as e:
            log(f"❌ Ошибка objects: {e}")

        # === 4. Статистика по товарам в кампании ===
        for param_name in ["campaignIds", "campaignId"]:
            try:
                async with session.get(
                    "https://api-performance.ozon.ru/api/client/statistics/campaign/product/json",
                    headers=headers,
                    params={param_name: str(first_id), "dateFrom": date_from, "dateTo": date_to}
                ) as r:
                    body = await r.text()
                    log(f"\n=== [4] GET /statistics/campaign/product/json?{param_name}={first_id} — status {r.status} ===")
                    log(body[:3000])
            except Exception as e:
                log(f"❌ Ошибка stats/product ({param_name}): {e}")

        # === 5. Общая статистика по кампании ===
        try:
            async with session.get(
                "https://api-performance.ozon.ru/api/client/statistics/json",
                headers=headers,
                params={"campaignIds": str(first_id), "dateFrom": date_from, "dateTo": date_to}
            ) as r:
                body = await r.text()
                log(f"\n=== [5] GET /statistics/json?campaignIds={first_id} — status {r.status} ===")
                log(body[:3000])
        except Exception as e:
            log(f"❌ Ошибка statistics/json: {e}")

        # === 6. Статистика по конкретной кампании ===
        try:
            async with session.get(
                f"https://api-performance.ozon.ru/api/client/statistics/{first_id}",
                headers=headers,
                params={"dateFrom": date_from, "dateTo": date_to}
            ) as r:
                body = await r.text()
                log(f"\n=== [6] GET /statistics/{first_id} — status {r.status} ===")
                log(body[:3000])
        except Exception as e:
            log(f"❌ Ошибка statistics/{first_id}: {e}")

    log("\n=== ДИАГНОСТИКА ЗАВЕРШЕНА ===")


if __name__ == "__main__":
    asyncio.run(main())
