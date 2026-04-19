import os
import time
import html
import requests
import asyncio
from dotenv import load_dotenv

from core.database import AsyncSessionLocal
from core.logger import get_logger
from models.user import User
from sqlalchemy.future import select
from services.rss_service import fetch_all_feeds
from services.ai_service import generate_summary
from services.validation import validate_tags, validate_custom_follows

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
logger = get_logger("telegram_bot")

AVAILABLE_TAGS = ["BTC", "Altın", "BIST", "Dolar", "Faiz", "Fed", "Euro", "Petrol", "Kripto", "Hisse"]

# ── Telegram API yardımcıları ──────────────────────────────────────────────────

def send_telegram_message(chat_id, text):
    """HTML formatında mesaj gönderir."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            logger.warning(f"Mesaj Gonderim Hatasi: {r.text}")
    except Exception as e:
        logger.error(f"Baglanti Hatasi: {e}")

def send_message_with_keyboard(chat_id, text, keyboard):
    """Inline keyboard ekli mesaj gönderir."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": keyboard
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            logger.warning(f"Keyboard Mesaj Hatasi: {r.text}")
    except Exception as e:
        logger.error(f"Baglanti Hatasi: {e}")

def answer_callback_query(callback_query_id):
    """Callback sorguya boş yanıt verir (yükleniyor ikonunu kaldırır)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": callback_query_id}, timeout=10)
    except Exception:
        pass

def edit_message_reply_markup(chat_id, message_id, keyboard):
    """Mevcut mesajın klavyesini günceller."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": keyboard
        }, timeout=15)
    except Exception:
        pass

def edit_message_text(chat_id, message_id, text):
    """Mevcut mesajın metnini günceller (klavyeyi kaldırır)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=15)
    except Exception:
        pass

# ── Tag keyboard ───────────────────────────────────────────────────────────────

def build_tag_keyboard(user_tags_str: str) -> dict:
    """Kullanıcının seçili tag'lerine göre inline keyboard oluşturur."""
    selected = set(t.strip() for t in user_tags_str.split(",") if t.strip())
    keyboard = []
    row = []
    for i, tag in enumerate(AVAILABLE_TAGS):
        label = f"✅ {tag}" if tag in selected else tag
        row.append({"text": label, "callback_data": f"tag_{tag}"})
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([{"text": "💾 Kaydet & Kapat", "callback_data": "tag_done"}])
    return {"inline_keyboard": keyboard}

# ── Komut işleyicileri ─────────────────────────────────────────────────────────

async def process_start_command(chat_id, user_id, username):
    """Kullanıcıyı veritabanına kaydeder ve hoş geldin mesajı atar."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == str(user_id)))
        user = result.scalars().first()
        if not user:
            user = User(telegram_id=str(user_id), username=username)
            session.add(user)
            await session.commit()

    welcome_msg = (
        "🚀 <b>Axiom'a Hoş Geldin</b>\n\n"
        "📊 Kripto & Finansal Piyasaların Gerçek Dedikodusu\n"
        "Başlayan her fırsatı kaçırma.\n\n"
        "⚡ <b>Ne Yapmak İstiyorsun?</b>\n"
        "/haber — Anlık pazar güncellemeleri\n"
        "/tags — İlgi alanlarını seç (BTC, Altın, BIST...)\n"
        "/takip AAPL — Sembol takip et\n"
        "/takipcikar AAPL — Takipten çıkar\n"
        "/takiplistem — Takip listeni gör"
    )
    send_telegram_message(chat_id, welcome_msg)

async def process_haber_command(chat_id):
    """Anlık haber talebini karşılar."""
    logger.info(f"📰 /haber KOMUTU: User {chat_id}")
    send_telegram_message(chat_id, "⚡ Piyasalar analiz ediliyor...\n\n⏳ 30 saniye içinde güncel haberler geliyor.")
    try:
        news_list = await fetch_all_feeds()
        if not news_list:
            send_telegram_message(chat_id, "⚠️ Yeni bir veri akışı bulunamadı.")
            return

        latest_news = news_list[0]
        logger.info(f"  📌 En yeni haber: '{latest_news['title'][:50]}...'")
        summary = await generate_summary(latest_news['title'], latest_news['link'])

        final_message = (
            f"📌 <b>{html.escape(latest_news['title'])}</b>\n\n"
            f"{html.escape(summary)}\n\n"
            f"📰 <b>Kaynak:</b> <a href='{latest_news['link']}'>{html.escape(latest_news['source'])}</a>"
        )
        logger.info(f"  ✉️ /haber mesajı gönderiliyor")
        send_telegram_message(chat_id, final_message)
    except Exception as e:
        logger.error(f"  ❌ /haber hatası: {str(e)}")
        send_telegram_message(chat_id, f"❌ Hata: {str(e)}")

async def process_takip_command(chat_id, user_id, keyword: str):
    """Kullanıcının custom takip listesine yeni kelime ekler."""
    keyword = keyword.strip()
    if not keyword:
        send_telegram_message(chat_id, "⚠️ Kullanım: <b>/takip [kelime]</b>\nÖrnek: /takip AAPL")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == str(user_id)))
        user = result.scalars().first()
        if not user:
            send_telegram_message(chat_id, "❌ Önce /start komutunu kullanın.")
            return

        follows = [k.strip() for k in (user.custom_follows or "").split(",") if k.strip()]
        if keyword.lower() in [f.lower() for f in follows]:
            send_telegram_message(chat_id, f"ℹ️ <b>{html.escape(keyword)}</b> zaten takip listenizdde.")
            return

        follows.append(keyword)
        new_follows_str = ",".join(follows)

        # Validate before saving
        is_valid, error_msg = validate_custom_follows(new_follows_str)
        if not is_valid:
            send_telegram_message(chat_id, f"❌ {error_msg}")
            return

        user.custom_follows = new_follows_str
        await session.commit()

    send_telegram_message(chat_id, f"✅ <b>{html.escape(keyword)}</b> takip listesine eklendi.\n\n/takiplistem ile tüm takiplerinizi görebilirsiniz.")

async def process_takip_cikar_command(chat_id, user_id, keyword: str):
    """Kullanıcının custom takip listesinden kelime çıkarır."""
    keyword = keyword.strip()
    if not keyword:
        send_telegram_message(chat_id, "⚠️ Kullanım: <b>/takipcikar [kelime]</b>\nÖrnek: /takipcikar AAPL")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == str(user_id)))
        user = result.scalars().first()
        if not user:
            send_telegram_message(chat_id, "❌ Önce /start komutunu kullanın.")
            return

        follows = [k.strip() for k in (user.custom_follows or "").split(",") if k.strip()]
        new_follows = [f for f in follows if f.lower() != keyword.lower()]

        if len(new_follows) == len(follows):
            send_telegram_message(chat_id, f"ℹ️ <b>{html.escape(keyword)}</b> takip listenizde bulunamadı.")
            return

        user.custom_follows = ",".join(new_follows)
        await session.commit()

    send_telegram_message(chat_id, f"🗑 <b>{html.escape(keyword)}</b> takip listesinden çıkarıldı.")

async def process_takiplistem_command(chat_id, user_id):
    """Kullanıcının takip listesini gösterir."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == str(user_id)))
        user = result.scalars().first()

    if not user:
        send_telegram_message(chat_id, "❌ Önce /start komutunu kullanın.")
        return

    follows = [k.strip() for k in (user.custom_follows or "").split(",") if k.strip()]
    tags = [t.strip() for t in (user.tags or "").split(",") if t.strip()]

    lines = ["📋 <b>Takip Listeniz</b>\n"]

    if tags:
        lines.append("🏷 <b>Konu Tag'leri:</b>")
        lines.append("  " + ", ".join(html.escape(t) for t in tags))
    else:
        lines.append("🏷 <b>Konu Tag'leri:</b> Seçilmedi (/tags ile seç)")

    lines.append("")

    if follows:
        lines.append("🔍 <b>Özel Takipler:</b>")
        for f in follows:
            lines.append(f"  • {html.escape(f)}")
        lines.append("\n/takipcikar [kelime] ile listeden çıkarabilirsiniz.")
    else:
        lines.append("🔍 <b>Özel Takipler:</b> Yok\n/takip [kelime] ile ekleyebilirsiniz.")

    send_telegram_message(chat_id, "\n".join(lines))

async def process_tags_command(chat_id, user_id):
    """Kullanıcıya tag seçim klavyesi gönderir."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == str(user_id)))
        user = result.scalars().first()
        user_tags = user.tags if user else ""

    keyboard = build_tag_keyboard(user_tags)
    text = (
        "📊 <b>İlgi Alanlarınızı Seçin</b>\n\n"
        "Takip etmek istediğiniz konulara dokunun. "
        "Seçili olanlar ✅ ile gösterilir.\n\n"
        "Sadece seçtiğiniz konulardaki haberler size iletilecektir. "
        "Tag seçmezseniz tüm haberler gelir."
    )
    send_message_with_keyboard(chat_id, text, keyboard)

async def process_tag_callback(callback_query_id, chat_id, message_id, user_id, tag):
    """Tag toggle callback'ini işler."""
    if tag == "done":
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.telegram_id == str(user_id)))
            user = result.scalars().first()
            tags_str = user.tags if user else ""
        tag_list = [t.strip() for t in tags_str.split(",") if t.strip()]
        tag_display = ", ".join(tag_list) if tag_list else "Tümü (filtre yok)"
        edit_message_text(chat_id, message_id, f"✅ <b>Tercihleriniz kaydedildi.</b>\n\n🏷 Aktif tag'ler: {html.escape(tag_display)}")
        answer_callback_query(callback_query_id)
        return

    # Tag'i toggle et ve DB'ye kaydet
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == str(user_id)))
        user = result.scalars().first()
        if not user:
            answer_callback_query(callback_query_id)
            return
        tags = set(t.strip() for t in user.tags.split(",") if t.strip())
        if tag in tags:
            tags.discard(tag)
        else:
            tags.add(tag)
        new_tags_str = ",".join(sorted(tags))

        # Validate before saving
        is_valid, error_msg = validate_tags(new_tags_str)
        if not is_valid:
            logger.warning(f"Invalid tags for user {user_id}: {error_msg}")
            answer_callback_query(callback_query_id)
            return

        user.tags = new_tags_str
        await session.commit()
        updated_tags = user.tags

    keyboard = build_tag_keyboard(updated_tags)
    edit_message_reply_markup(chat_id, message_id, keyboard)
    answer_callback_query(callback_query_id)

# ── Ana bot döngüsü ────────────────────────────────────────────────────────────

async def start_telegram_bot():
    if not TELEGRAM_BOT_TOKEN or "buraya" in TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN eksik.")
        return

    try:
        await asyncio.to_thread(
            requests.post,
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook?drop_pending_updates=True",
            timeout=10
        )
    except Exception as e:
        logger.warning(f"Webhook temizleme başarısız: {e}")

    logger.info("Axiom Telegram Botu Dinlemede...")

    offset = 0
    error_count = 0
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=30"
            response = await asyncio.to_thread(
                requests.get,
                url,
                timeout=40
            )
            if response.status_code == 200:
                error_count = 0  # Başarılı istekte hata sayacını sıfırla
                data = response.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1

                    # Normal mesaj komutları
                    if "message" in update and "text" in update["message"]:
                        chat_id = update["message"]["chat"]["id"]
                        user_id = update["message"]["from"]["id"]
                        username = update["message"]["from"].get("username", "Bilinmeyen")
                        text = update["message"]["text"]

                        if text.startswith("/start"):
                            await process_start_command(chat_id, user_id, username)
                        elif text.lower().startswith("/haber"):
                            await process_haber_command(chat_id)
                        elif text.lower().startswith("/tags"):
                            await process_tags_command(chat_id, user_id)
                        elif text.lower().startswith("/takipcikar"):
                            keyword = text[len("/takipcikar"):].strip()
                            await process_takip_cikar_command(chat_id, user_id, keyword)
                        elif text.lower().startswith("/takiplistem"):
                            await process_takiplistem_command(chat_id, user_id)
                        elif text.lower().startswith("/takip"):
                            keyword = text[len("/takip"):].strip()
                            await process_takip_command(chat_id, user_id, keyword)

                    # Inline keyboard callback'leri
                    elif "callback_query" in update:
                        cq = update["callback_query"]
                        cq_id = cq["id"]
                        cq_data = cq.get("data", "")
                        cq_user_id = cq["from"]["id"]
                        cq_chat_id = cq["message"]["chat"]["id"]
                        cq_message_id = cq["message"]["message_id"]

                        if cq_data.startswith("tag_"):
                            tag = cq_data[4:]  # "tag_BTC" → "BTC"
                            await process_tag_callback(cq_id, cq_chat_id, cq_message_id, cq_user_id, tag)

            await asyncio.sleep(1)
            
        except asyncio.CancelledError:
            logger.info("Bot iptal edildi (Uygulama kapaniyor).")
            raise
        except Exception as e:
            error_count += 1
            # Artan (exponential) bekleme suresi: 5, 10, 15... max 60 saniye.
            wait_time = min(60, 5 * error_count)
            logger.error(f"Bot loop hatasi ({error_count}. deneme): {e}. {wait_time} sn sonra tekrar denenecek...")
            await asyncio.sleep(wait_time)
