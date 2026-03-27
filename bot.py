"""
✈️ Uçak Bileti Takip Botu
Telegram üzerinden ucuz uçuş bildirimleri
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")

DATA_FILE = Path("routes.json")

# Conversation states
(
    ROUTE_ORIGIN,
    ROUTE_DEST,
    ROUTE_DATE,
    ROUTE_PRICE,
    ROUTE_ADULTS,
) = range(5)


# ──────────────────────────────────────────────
# Veri Yönetimi
# ──────────────────────────────────────────────

def load_routes() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_routes(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user_routes(user_id: int) -> list:
    data = load_routes()
    return data.get(str(user_id), [])


def add_route(user_id: int, route: dict):
    data = load_routes()
    uid = str(user_id)
    if uid not in data:
        data[uid] = []
    data[uid].append(route)
    save_routes(data)


def remove_route(user_id: int, index: int):
    data = load_routes()
    uid = str(user_id)
    if uid in data and 0 <= index < len(data[uid]):
        data[uid].pop(index)
        save_routes(data)
        return True
    return False


def update_last_price(user_id: int, index: int, price: float):
    data = load_routes()
    uid = str(user_id)
    if uid in data and 0 <= index < len(data[uid]):
        data[uid][index]["last_price"] = price
        data[uid][index]["last_checked"] = datetime.now().isoformat()
        save_routes(data)


# ──────────────────────────────────────────────
# Amadeus API
# ──────────────────────────────────────────────

_amadeus_token: dict = {"token": None, "expires": 0}


async def get_amadeus_token() -> str | None:
    if not AMADEUS_CLIENT_ID or not AMADEUS_CLIENT_SECRET:
        return None
    now = datetime.now().timestamp()
    if _amadeus_token["token"] and now < _amadeus_token["expires"]:
        return _amadeus_token["token"]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://test.api.amadeus.com/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": AMADEUS_CLIENT_ID,
                "client_secret": AMADEUS_CLIENT_SECRET,
            },
        )
        if resp.status_code == 200:
            d = resp.json()
            _amadeus_token["token"] = d["access_token"]
            _amadeus_token["expires"] = now + d["expires_in"] - 60
            return _amadeus_token["token"]
    return None


async def search_amadeus(origin: str, dest: str, date: str, adults: int = 1) -> list[dict]:
    token = await get_amadeus_token()
    if not token:
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://test.api.amadeus.com/v2/shopping/flight-offers",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "originLocationCode": origin.upper(),
                "destinationLocationCode": dest.upper(),
                "departureDate": date,
                "adults": adults,
                "currencyCode": "TRY",
                "max": 5,
            },
        )
        if resp.status_code == 200:
            offers = resp.json().get("data", [])
            results = []
            for o in offers:
                price = float(o["price"]["total"])
                airline = o["validatingAirlineCodes"][0] if o.get("validatingAirlineCodes") else "?"
                itinerary = o["itineraries"][0]
                seg = itinerary["segments"][0]
                dep = seg["departure"]["at"]
                arr = seg["arrival"]["at"]
                duration = itinerary["duration"].replace("PT", "").lower()
                results.append({
                    "price": price,
                    "airline": airline,
                    "departure": dep,
                    "arrival": arr,
                    "duration": duration,
                    "source": "Amadeus",
                })
            return results
    return []


# ──────────────────────────────────────────────
# RapidAPI / Skyscanner
# ──────────────────────────────────────────────

async def search_rapidapi(origin: str, dest: str, date: str) -> list[dict]:
    if not RAPIDAPI_KEY:
        return []
    # Skyscanner Flight Search via RapidAPI
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                "https://skyscanner-skyscanner-flight-search-v1.p.rapidapi.com/apiservices/browsequotes/v1.0/TR/TRY/tr-TR/"
                f"{origin.upper()}/{dest.upper()}/{date}",
                headers={
                    "X-RapidAPI-Key": RAPIDAPI_KEY,
                    "X-RapidAPI-Host": "skyscanner-skyscanner-flight-search-v1.p.rapidapi.com",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                quotes = data.get("Quotes", [])
                carriers = {c["CarrierId"]: c["Name"] for c in data.get("Carriers", [])}
                results = []
                for q in quotes[:5]:
                    price = q["MinPrice"]
                    carrier_id = q.get("OutboundLeg", {}).get("CarrierIds", [0])[0]
                    airline = carriers.get(carrier_id, "Bilinmiyor")
                    dep = q.get("OutboundLeg", {}).get("DepartureDate", "")
                    results.append({
                        "price": price,
                        "airline": airline,
                        "departure": dep,
                        "arrival": "",
                        "duration": "",
                        "source": "Skyscanner",
                    })
                return results
        except Exception as e:
            logger.warning(f"RapidAPI hatası: {e}")
    return []


# ──────────────────────────────────────────────
# En Ucuz Fiyat Bul
# ──────────────────────────────────────────────

async def find_cheapest(origin: str, dest: str, date: str, adults: int = 1) -> dict | None:
    results = []
    amadeus_results = await search_amadeus(origin, dest, date, adults)
    results.extend(amadeus_results)
    rapid_results = await search_rapidapi(origin, dest, date)
    results.extend(rapid_results)

    if not results:
        return None
    return min(results, key=lambda x: x["price"])


# ──────────────────────────────────────────────
# Komutlar
# ──────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "✈️ *Uçak Bileti Takip Botuna Hoş Geldin!*\n\n"
        "Sana istediğin rotalar için ucuz bilet bildirimleri göndereceğim.\n\n"
        "📌 *Komutlar:*\n"
        "/ekle — Yeni rota ekle\n"
        "/rotalar — Takip ettiğin rotalar\n"
        "/kontrol — Şimdi fiyatları kontrol et\n"
        "/sil — Rota sil\n"
        "/yardim — Yardım\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🆘 *Yardım*\n\n"
        "Bu bot seçtiğin uçuş rotalarını takip eder ve fiyat eşiğinin altına düştüğünde sana haber verir.\n\n"
        "📖 *Kullanım:*\n"
        "1. /ekle ile rota ekle\n"
        "2. Kalkış → Varış → Tarih → Max Fiyat → Yolcu bilgilerini gir\n"
        "3. Bot her gün otomatik kontrol eder\n"
        "4. Fiyat eşiğin altına düşünce bildirim alırsın ✅\n\n"
        "✈️ *IATA kodları örnekleri:*\n"
        "`IST` → İstanbul (Atatürk)\n"
        "`SAW` → İstanbul (Sabiha Gökçen)\n"
        "`AYT` → Antalya\n"
        "`ESB` → Ankara\n"
        "`ADB` → İzmir\n"
        "`LHR` → Londra Heathrow\n"
        "`CDG` → Paris Charles de Gaulle\n"
        "`DXB` → Dubai\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_routes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    routes = get_user_routes(user_id)
    if not routes:
        await update.message.reply_text(
            "📭 Henüz takip ettiğin bir rota yok.\n/ekle komutuyla başla!"
        )
        return

    text = "📋 *Takip Ettiğin Rotalar:*\n\n"
    for i, r in enumerate(routes, 1):
        last = f"Son fiyat: *{r['last_price']:.0f} TL*" if r.get("last_price") else "Henüz kontrol edilmedi"
        text += (
            f"*{i}. {r['origin']} → {r['dest']}*\n"
            f"📅 Tarih: {r['date']}\n"
            f"💰 Eşik: {r['max_price']} TL\n"
            f"👤 Yolcu: {r.get('adults', 1)}\n"
            f"📊 {last}\n\n"
        )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_check_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    routes = get_user_routes(user_id)
    if not routes:
        await update.message.reply_text("📭 Takip ettiğin rota yok. /ekle ile ekle!")
        return

    msg = await update.message.reply_text("🔍 Fiyatlar kontrol ediliyor...")
    results = []
    for i, r in enumerate(routes):
        best = await find_cheapest(r["origin"], r["dest"], r["date"], r.get("adults", 1))
        if best:
            update_last_price(user_id, i, best["price"])
            emoji = "🟢" if best["price"] <= r["max_price"] else "🔴"
            dep_str = best["departure"][:16].replace("T", " ") if best["departure"] else "—"
            results.append(
                f"{emoji} *{r['origin']} → {r['dest']}* ({r['date']})\n"
                f"   💺 {best['airline']} | {dep_str}\n"
                f"   💰 *{best['price']:.0f} TL* (eşik: {r['max_price']} TL)\n"
                f"   📡 Kaynak: {best['source']}\n"
            )
        else:
            results.append(f"⚠️ *{r['origin']} → {r['dest']}*: Sonuç bulunamadı\n")

    text = "📊 *Güncel Fiyatlar:*\n\n" + "\n".join(results)
    await msg.edit_text(text, parse_mode="Markdown")


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    routes = get_user_routes(user_id)
    if not routes:
        await update.message.reply_text("📭 Silinecek rota yok.")
        return

    buttons = []
    for i, r in enumerate(routes):
        buttons.append(
            [InlineKeyboardButton(
                f"🗑 {r['origin']} → {r['dest']} ({r['date']})",
                callback_data=f"del_{i}"
            )]
        )
    markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("Silmek istediğin rotayı seç:", reply_markup=markup)


async def callback_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    index = int(query.data.split("_")[1])
    routes = get_user_routes(user_id)
    if 0 <= index < len(routes):
        r = routes[index]
        remove_route(user_id, index)
        await query.edit_message_text(
            f"✅ *{r['origin']} → {r['dest']}* rotası silindi.", parse_mode="Markdown"
        )
    else:
        await query.edit_message_text("⚠️ Rota bulunamadı.")


# ──────────────────────────────────────────────
# Rota Ekleme — ConversationHandler
# ──────────────────────────────────────────────

async def conv_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✈️ *Yeni Rota Ekle*\n\n"
        "Kalkış havalimanı IATA kodunu gir:\n"
        "_(örnek: `IST`, `SAW`, `ESB`)_\n\n"
        "İptal için /iptal yaz.",
        parse_mode="Markdown",
    )
    return ROUTE_ORIGIN


async def conv_origin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    origin = update.message.text.strip().upper()
    if len(origin) != 3 or not origin.isalpha():
        await update.message.reply_text("⚠️ Geçersiz IATA kodu. 3 harf gir (örn: IST)")
        return ROUTE_ORIGIN
    ctx.user_data["origin"] = origin
    await update.message.reply_text(
        f"✅ Kalkış: *{origin}*\n\n"
        "Varış havalimanı IATA kodunu gir:\n_(örnek: `LHR`, `DXB`, `AYT`)_",
        parse_mode="Markdown",
    )
    return ROUTE_DEST


async def conv_dest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    dest = update.message.text.strip().upper()
    if len(dest) != 3 or not dest.isalpha():
        await update.message.reply_text("⚠️ Geçersiz IATA kodu. 3 harf gir (örn: LHR)")
        return ROUTE_DEST
    ctx.user_data["dest"] = dest
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    await update.message.reply_text(
        f"✅ Varış: *{dest}*\n\n"
        f"Uçuş tarihini gir (YYYY-AA-GG):\n_örnek: `{tomorrow}`_",
        parse_mode="Markdown",
    )
    return ROUTE_DATE


async def conv_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.strip()
    try:
        date = datetime.strptime(date_str, "%Y-%m-%d")
        if date.date() < datetime.now().date():
            await update.message.reply_text("⚠️ Geçmiş tarih girilemez. Gelecek bir tarih gir.")
            return ROUTE_DATE
    except ValueError:
        await update.message.reply_text("⚠️ Format hatalı. YYYY-AA-GG formatında gir. Örn: 2025-06-15")
        return ROUTE_DATE
    ctx.user_data["date"] = date_str
    await update.message.reply_text(
        f"✅ Tarih: *{date_str}*\n\n"
        "Maksimum fiyat eşiğini TL olarak gir:\n_(bu fiyatın altında bilet bulunca haber vereceğim)_\n"
        "_örnek: `2500`_",
        parse_mode="Markdown",
    )
    return ROUTE_PRICE


async def conv_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip().replace(",", "."))
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Geçerli bir sayı gir. Örn: 2500")
        return ROUTE_PRICE
    ctx.user_data["max_price"] = price
    await update.message.reply_text(
        f"✅ Eşik: *{price:.0f} TL*\n\n"
        "Kaç yolcu? (1-9)\n_örnek: `1`_",
        parse_mode="Markdown",
    )
    return ROUTE_ADULTS


async def conv_adults(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        adults = int(update.message.text.strip())
        if not 1 <= adults <= 9:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ 1 ile 9 arasında bir sayı gir.")
        return ROUTE_ADULTS

    route = {
        "origin": ctx.user_data["origin"],
        "dest": ctx.user_data["dest"],
        "date": ctx.user_data["date"],
        "max_price": ctx.user_data["max_price"],
        "adults": adults,
        "last_price": None,
        "last_checked": None,
        "added": datetime.now().isoformat(),
    }
    add_route(update.effective_user.id, route)

    await update.message.reply_text(
        f"🎉 *Rota eklendi!*\n\n"
        f"✈️ {route['origin']} → {route['dest']}\n"
        f"📅 {route['date']}\n"
        f"💰 Eşik: {route['max_price']:.0f} TL\n"
        f"👤 {adults} yolcu\n\n"
        "Fiyatları şimdi kontrol etmek için /kontrol komutunu kullan.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def conv_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ İptal edildi.")
    return ConversationHandler.END


# ──────────────────────────────────────────────
# Zamanlanmış Kontrol
# ──────────────────────────────────────────────

async def scheduled_check(app: Application):
    data = load_routes()
    logger.info(f"Zamanlanmış kontrol başladı. {len(data)} kullanıcı.")
    for uid_str, routes in data.items():
        user_id = int(uid_str)
        for i, r in enumerate(routes):
            try:
                best = await find_cheapest(r["origin"], r["dest"], r["date"], r.get("adults", 1))
                if not best:
                    continue
                update_last_price(user_id, i, best["price"])
                if best["price"] <= r["max_price"]:
                    dep_str = best["departure"][:16].replace("T", " ") if best["departure"] else "—"
                    msg = (
                        f"🚨 *UCUZ BİLET BULUNDU!*\n\n"
                        f"✈️ *{r['origin']} → {r['dest']}*\n"
                        f"📅 {r['date']}\n"
                        f"💺 {best['airline']}\n"
                        f"🕐 Kalkış: {dep_str}\n"
                        f"💰 *{best['price']:.0f} TL* _(eşiğin: {r['max_price']:.0f} TL)_\n"
                        f"📡 Kaynak: {best['source']}\n\n"
                        f"🔗 Rezervasyon için Google Flights veya havayolu sitesini kontrol et!"
                    )
                    await app.bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")
                    logger.info(f"Bildirim gönderildi: user={user_id}, rota={r['origin']}-{r['dest']}, fiyat={best['price']}")
            except Exception as e:
                logger.error(f"Kontrol hatası: user={user_id}, rota={r}: {e}")
        await asyncio.sleep(1)  # rate limit


# ──────────────────────────────────────────────
# Ana Uygulama
# ──────────────────────────────────────────────

def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN eksik! .env dosyasını kontrol et.")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("ekle", conv_start)],
        states={
            ROUTE_ORIGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_origin)],
            ROUTE_DEST:   [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_dest)],
            ROUTE_DATE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_date)],
            ROUTE_PRICE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_price)],
            ROUTE_ADULTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_adults)],
        },
        fallbacks=[CommandHandler("iptal", conv_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("yardim", cmd_help))
    app.add_handler(CommandHandler("rotalar", cmd_routes))
    app.add_handler(CommandHandler("kontrol", cmd_check_now))
    app.add_handler(CommandHandler("sil", cmd_delete))
    app.add_handler(CallbackQueryHandler(callback_delete, pattern=r"^del_\d+$"))
    app.add_handler(conv_handler)

    # Günlük saat 08:00'de kontrol
    scheduler = AsyncIOScheduler(timezone="Europe/Istanbul")
    scheduler.add_job(
        lambda: asyncio.ensure_future(scheduled_check(app)),
        trigger="cron",
        hour=8,
        minute=0,
        id="daily_check",
    )
    scheduler.start()
    logger.info("⏰ Günlük kontrol 08:00'de çalışacak (İstanbul saati)")

    logger.info("🤖 Bot başlatılıyor...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
