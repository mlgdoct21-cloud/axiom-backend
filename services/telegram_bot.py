import os
import re
import time
import html
import requests
import asyncio
from collections import defaultdict
from typing import Optional
from urllib.parse import quote
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

# ── Rate limiting: kullanıcı başına komut cooldown'u ───────────────────────────
# Pahalı komutlar (/haber, /report) AI ve harici API kotalarını tüketir.
# Aynı kullanıcının kısa sürede çok kez çağırmasını engelle.
_HEAVY_CMD_COOLDOWN_SEC = int(os.getenv("BOT_HEAVY_CMD_COOLDOWN_SEC", "30"))
_LIGHT_CMD_COOLDOWN_SEC = int(os.getenv("BOT_LIGHT_CMD_COOLDOWN_SEC", "3"))
# {(user_id, command): unix_ts_resume_at}
_user_cmd_cooldowns: dict = defaultdict(float)


def _check_rate_limit(user_id: int, command: str, cooldown_sec: int) -> Optional[int]:
    """Cooldown aktifse kalan saniyeyi döner; aksi halde None ve cooldown'u set eder."""
    key = (user_id, command)
    now = time.time()
    resume_at = _user_cmd_cooldowns.get(key, 0)
    if now < resume_at:
        return int(resume_at - now)
    _user_cmd_cooldowns[key] = now + cooldown_sec
    return None


# ── Sembol doğrulama ───────────────────────────────────────────────────────────
# Ticker sembolleri sadece harf, rakam, nokta ve tire içerebilir. Bu desen URL
# parametre injection'ı engeller (örn. "AAPL&mode=full" reddedilir).
_VALID_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")


def _is_valid_symbol(symbol: str) -> bool:
    return bool(_VALID_SYMBOL_RE.match(symbol))

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

def send_telegram_message_get_id(chat_id, text) -> Optional[int]:
    """HTML formatında mesaj gönderir, message_id döner (edit için)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
        else:
            logger.warning(f"Mesaj Gonderim Hatasi: {r.text}")
    except Exception as e:
        logger.error(f"Baglanti Hatasi: {e}")
    return None

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

def send_telegram_photo(chat_id, photo_bytes: bytes, caption: str = "") -> bool:
    """Upload a PNG (in memory) via multipart sendPhoto, optional HTML caption.
    Returns True on 200, False otherwise — callers fall back to a text message.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", photo_bytes, "image/png")}
    data = {"chat_id": str(chat_id), "parse_mode": "HTML"}
    if caption:
        # Telegram caps captions at 1024 chars; trim defensively.
        data["caption"] = caption[:1000]
    try:
        r = requests.post(url, data=data, files=files, timeout=20)
        if r.status_code != 200:
            logger.warning(f"sendPhoto Hatasi: {r.text}")
            return False
        return True
    except Exception as e:
        logger.error(f"sendPhoto Baglanti Hatasi: {e}")
        return False


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
        r = requests.post(url, json={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=15)
        if r.status_code != 200:
            logger.warning(f"Edit mesaj hatası: {r.text}")
    except Exception as e:
        logger.error(f"Edit mesaj bağlantı hatası: {e}")

def edit_message_text_with_keyboard(chat_id, message_id, text, keyboard):
    """Mevcut mesajın metnini inline keyboard ile günceller."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": keyboard
        }, timeout=15)
        if r.status_code != 200:
            logger.warning(f"Edit mesaj+keyboard hatası: {r.text}")
    except Exception as e:
        logger.error(f"Edit mesaj+keyboard bağlantı hatası: {e}")

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
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.telegram_id == str(user_id)))
            user = result.scalars().first()
            if not user:
                user = User(telegram_id=str(user_id), username=username)
                session.add(user)
                await session.commit()
    except Exception as e:
        logger.error(f"DB hatası (/start) user={user_id}: {e}")
        send_telegram_message(chat_id, "⚠️ Veritabanı bağlantısı kurulamadı. Lütfen daha sonra tekrar /start yazın.")
        return

    welcome_msg = (
        "🚀 <b>Axiom'a Hoş Geldin</b>\n\n"
        "📊 Kripto & Finansal Piyasaların Gerçek Dedikodusu\n"
        "Başlayan her fırsatı kaçırma.\n\n"
        "⚡ <b>Ne Yapmak İstiyorsun?</b>\n"
        "/haber — Anlık pazar güncellemeleri\n"
        "/report AAPL — Hisse insider raporu (AI analiz)\n"
        "/tags — İlgi alanlarını seç (BTC, Altın, BIST...)\n"
        "/takip AAPL — Sembol takip et\n"
        "/takipcikar AAPL — Takipten çıkar\n"
        "/takiplistem — Takip listeni gör\n\n"
        "💳 <b>Üyelik:</b>\n"
        "/tier — Mevcut planını gör\n"
        "/upgrade — PREMIUM / ADVANCE planlarına yükselt"
    )
    send_telegram_message(chat_id, welcome_msg)

async def _enforce_tier_quota(chat_id, user_id, command: str) -> bool:
    """Returns True when the user is over today's quota (caller should
    skip work + the message is already sent)."""
    from services.tier_quota import check_and_consume
    tier = await _get_user_tier(user_id)
    q = check_and_consume(user_id, tier, command)
    if not q.allowed:
        upgrade_hint = (
            "PREMIUM ile günde 30/50, ADVANCE ile sınırsız: /upgrade"
            if q.tier == "free" else
            "ADVANCE ile sınırsız: /upgrade"
        )
        send_telegram_message(
            chat_id,
            f"🛑 Günlük <b>{html.escape(command)}</b> limitiniz doldu "
            f"({q.used}/{q.limit}).\n\n{upgrade_hint}",
        )
        return True
    return False


async def process_haber_command(chat_id, user_id):
    """Anlık haber talebini karşılar."""
    # Tier quota — Free 10/day, Premium 50/day, Advance unlimited.
    if await _enforce_tier_quota(chat_id, user_id, "/haber"):
        return
    # Rate limit kontrolü — her kullanıcı her 30 saniyede bir /haber yapabilir
    remaining = _check_rate_limit(user_id, "/haber", _HEAVY_CMD_COOLDOWN_SEC)
    if remaining is not None:
        send_telegram_message(
            chat_id,
            f"⏳ Çok hızlı! <b>{remaining}s</b> sonra tekrar /haber deneyin."
        )
        return

    logger.info(f"📰 /haber KOMUTU: User {chat_id}")
    # Send loading message and keep its ID so we can edit it in-place
    loading_msg_id = await asyncio.to_thread(
        send_telegram_message_get_id,
        chat_id,
        "⚡ <b>Piyasalar analiz ediliyor...</b>\n\n⏳ Güncel haberler yükleniyor."
    )
    try:
        news_list = await fetch_all_feeds()
        if not news_list:
            if loading_msg_id:
                edit_message_text(chat_id, loading_msg_id, "⚠️ Yeni bir veri akışı bulunamadı.")
            return

        latest_news = news_list[0]

        # Guard against None values from RSS
        title = latest_news.get('title') or 'Başlık Yok'
        link = latest_news.get('link') or '#'
        source = latest_news.get('source') or 'Bilinmeyen Kaynak'

        logger.info(f"  📌 En yeni haber: '{title[:50]}...'")
        analiz = await generate_summary(title, link)

        # Handle case where summary is None (Gemini failed) or not a dict
        if not analiz or not isinstance(analiz, dict):
            logger.warning(f"  ❌ Haber özeti boş, mesaj gönderilemedi")
            if loading_msg_id:
                edit_message_text(chat_id, loading_msg_id, "⚠️ Haber özeti alınamadı, lütfen daha sonra tekrar deneyin.")
            return

        # 3-tier JSON'dan telegram_hook'u çıkar (bot için kısa versiyon)
        telegram_hook = analiz.get("telegram_hook", "").strip()
        if not telegram_hook:
            # Fallback: dashboard_summary veya axiom_analysis
            telegram_hook = analiz.get("dashboard_summary", "").strip() or analiz.get("axiom_analysis", "").strip()

        if not telegram_hook:
            logger.warning(f"  ❌ AI yanıtı boş, mesaj gönderilemedi")
            if loading_msg_id:
                edit_message_text(chat_id, loading_msg_id, "⚠️ Haber özeti alınamadı, lütfen daha sonra tekrar deneyin.")
            return

        # html.escape the href URL to handle & in query params (e.g. Yahoo Finance URLs)
        safe_link = html.escape(link, quote=True)
        final_message = (
            f"📌 <b>{html.escape(title)}</b>\n\n"
            f"{telegram_hook}\n\n"
            f"📰 <b>Kaynak:</b> <a href='{safe_link}'>{html.escape(source)}</a>"
        )
        logger.info(f"  ✉️ /haber mesajı gönderiliyor")
        if loading_msg_id:
            edit_message_text(chat_id, loading_msg_id, final_message)
        else:
            send_telegram_message(chat_id, final_message)
    except Exception as e:
        # Hata detayı sadece loglara — kullanıcıya generic mesaj
        logger.error(f"  ❌ /haber hatası: {str(e)}")
        user_msg = "⚠️ Haber alınamadı. Lütfen birkaç dakika sonra tekrar deneyin."
        if loading_msg_id:
            edit_message_text(chat_id, loading_msg_id, user_msg)
        else:
            send_telegram_message(chat_id, user_msg)

async def process_takip_command(chat_id, user_id, keyword: str):
    """Kullanıcının custom takip listesine yeni kelime ekler."""
    # Hafif rate limit — DB write yapan komutlar için spam koruması
    remaining = _check_rate_limit(user_id, "/takip", _LIGHT_CMD_COOLDOWN_SEC)
    if remaining is not None:
        return  # Sessiz drop — kullanıcıyı rahatsız etmemek için

    keyword = keyword.strip()
    if not keyword:
        send_telegram_message(chat_id, "⚠️ Kullanım: <b>/takip [kelime]</b>\nÖrnek: /takip AAPL")
        return

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.telegram_id == str(user_id)))
            user = result.scalars().first()
            if not user:
                send_telegram_message(chat_id, "❌ Önce /start komutunu kullanın.")
                return

            follows = [k.strip() for k in (user.custom_follows or "").split(",") if k.strip()]
            if keyword.lower() in [f.lower() for f in follows]:
                send_telegram_message(chat_id, f"ℹ️ <b>{html.escape(keyword)}</b> zaten takip listenizde.")
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
    except Exception as e:
        logger.error(f"DB hatası (/takip) user={user_id}: {e}")
        send_telegram_message(chat_id, "⚠️ Veritabanı bağlantısı kurulamadı. Lütfen tekrar deneyin.")
        return

    send_telegram_message(chat_id, f"✅ <b>{html.escape(keyword)}</b> takip listesine eklendi.\n\n/takiplistem ile tüm takiplerinizi görebilirsiniz.")

async def process_takip_cikar_command(chat_id, user_id, keyword: str):
    """Kullanıcının custom takip listesinden kelime çıkarır."""
    keyword = keyword.strip()
    if not keyword:
        send_telegram_message(chat_id, "⚠️ Kullanım: <b>/takipcikar [kelime]</b>\nÖrnek: /takipcikar AAPL")
        return

    try:
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
    except Exception as e:
        logger.error(f"DB hatası (/takipcikar) user={user_id}: {e}")
        send_telegram_message(chat_id, "⚠️ Veritabanı bağlantısı kurulamadı. Lütfen tekrar deneyin.")
        return

    send_telegram_message(chat_id, f"🗑 <b>{html.escape(keyword)}</b> takip listesinden çıkarıldı.")

async def process_takiplistem_command(chat_id, user_id):
    """Kullanıcının takip listesini gösterir."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.telegram_id == str(user_id)))
            user = result.scalars().first()
    except Exception as e:
        logger.error(f"DB hatası (/takiplistem) user={user_id}: {e}")
        send_telegram_message(chat_id, "⚠️ Veritabanı bağlantısı kurulamadı. Lütfen tekrar deneyin.")
        return

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

async def process_report_command(chat_id, user_id, symbol: str):
    """Insider raporu teaser'ını gönderir, butonlarla dashboard'a yönlendirir."""
    symbol = symbol.strip().upper()
    if not symbol:
        send_telegram_message(
            chat_id,
            "⚠️ Kullanım: <b>/report [SEMBOL]</b>\n\n"
            "Örnek:\n"
            "• <code>/report AAPL</code>\n"
            "• <code>/report MSFT</code>\n"
            "• <code>/report TSLA</code>"
        )
        return

    # Tier quota — Free 5/day, Premium 30/day, Advance unlimited.
    if await _enforce_tier_quota(chat_id, user_id, "/report"):
        return

    # Sembol formatı doğrulaması — URL parametre injection'ı engeller
    if not _is_valid_symbol(symbol):
        send_telegram_message(
            chat_id,
            f"❌ Geçersiz sembol: <code>{html.escape(symbol[:20])}</code>\n\n"
            f"Sembol sadece harf, rakam, nokta ve tire içerebilir (max 10 karakter)."
        )
        return

    # Rate limit — /report pahalı bir komut (FMP + Gemini API çağrıları)
    remaining = _check_rate_limit(user_id, "/report", _HEAVY_CMD_COOLDOWN_SEC)
    if remaining is not None:
        send_telegram_message(
            chat_id,
            f"⏳ Çok hızlı! <b>{remaining}s</b> sonra tekrar /report deneyin."
        )
        return

    logger.info(f"📊 /report KOMUTU: User {chat_id} → {symbol}")

    # Loading mesajı
    loading_msg_id = await asyncio.to_thread(
        send_telegram_message_get_id,
        chat_id,
        f"📊 <b>{html.escape(symbol)}</b> için rapor oluşturuluyor...\n\n"
        f"⏳ FMP verileri + Gemini AI analizi"
    )

    try:
        dashboard_url = os.getenv("DASHBOARD_URL", "https://axiom-dashboard.vercel.app").rstrip("/")
        # quote() ile URL encoding — symbol artık _is_valid_symbol'dan geçtiği için
        # zaten güvenli ama defense-in-depth için ekstra koruma
        api_url = (
            f"{dashboard_url}/api/stock/analysis/insider-report"
            f"?symbol={quote(symbol)}&mode=teaser&locale=tr"
        )

        r = await asyncio.to_thread(requests.get, api_url, timeout=60)

        if r.status_code != 200:
            try:
                err_body = r.json()
                err = err_body.get("error", f"HTTP {r.status_code}")
            except Exception:
                err = f"HTTP {r.status_code}"
            logger.warning(f"  ❌ Insider-report API hatası ({symbol}): {err}")
            # Kullanıcıya ham hata gösterme — generic mesaj
            if loading_msg_id:
                edit_message_text(
                    chat_id, loading_msg_id,
                    f"❌ <b>{html.escape(symbol)}</b> için rapor alınamadı. "
                    f"Sembol geçerli değil veya servis geçici olarak kullanılamıyor."
                )
            return

        data = r.json()
        teaser = (data.get("teaser") or "").strip()

        if not teaser:
            if loading_msg_id:
                edit_message_text(
                    chat_id, loading_msg_id,
                    f"⚠️ <b>{html.escape(symbol)}</b> için teaser üretilemedi."
                )
            return

        dashboard_deeplink = f"{dashboard_url}/tr/report/{symbol}"
        subscribe_url = f"{dashboard_url}/pricing?ref=telegram"

        # Telegram inline keyboard public URL ister; localhost kabul etmez.
        is_public_url = dashboard_url.startswith("https://") or (
            dashboard_url.startswith("http://") and "localhost" not in dashboard_url and "127.0.0.1" not in dashboard_url
        )

        final_message = (
            f"📊 <b>{html.escape(symbol)} — Insider Raporu</b>\n\n"
            f"{html.escape(teaser)}\n\n"
        )

        if is_public_url:
            final_message += "<i>Tam raporu görmek için aşağıdaki butonu kullan 👇</i>"
            keyboard = {
                "inline_keyboard": [[
                    {"text": "📖 Tam Raporu Oku", "url": dashboard_deeplink},
                    {"text": "🔔 Abone Ol", "url": subscribe_url}
                ]]
            }
            if loading_msg_id:
                edit_message_text_with_keyboard(chat_id, loading_msg_id, final_message, keyboard)
            else:
                send_message_with_keyboard(chat_id, final_message, keyboard)
        else:
            # Lokal test modu: buton yok, sadece teaser + link metin
            final_message += (
                f"<i>🔧 Lokal test modu (buton atlanıyor)</i>\n"
                f"Dashboard: <code>{dashboard_deeplink}</code>"
            )
            if loading_msg_id:
                edit_message_text(chat_id, loading_msg_id, final_message)
            else:
                send_telegram_message(chat_id, final_message)

        logger.info(f"  ✅ /report mesajı gönderildi: {symbol}")

    except Exception as e:
        # Hata detayı sadece loglara — kullanıcıya generic mesaj
        logger.error(f"  ❌ /report hatası ({symbol}): {e}")
        user_msg = "❌ Rapor oluşturulurken bir sorun oluştu. Lütfen birkaç dakika sonra tekrar deneyin."
        if loading_msg_id:
            edit_message_text(chat_id, loading_msg_id, user_msg)
        else:
            send_telegram_message(chat_id, user_msg)

_TIER_LABELS = {
    "free": "🆓 FREE",
    "premium": "💎 PREMIUM",
    "advance": "🚀 ADVANCE",
}

# Admin handle for upgrade requests until Stripe billing lands. Override
# via env so we don't have to redeploy when the contact changes.
_UPGRADE_CONTACT = os.getenv("UPGRADE_CONTACT", "@axiom_destek")


async def _get_user_tier(user_id) -> str:
    """Read the caller's tier; defaults to 'free' for unknown / new users."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.telegram_id == str(user_id)))
            user = result.scalars().first()
            if not user:
                return "free"
            return (getattr(user, "tier", "free") or "free").lower()
    except Exception as e:
        logger.error(f"DB hatası (_get_user_tier) user={user_id}: {e}")
        return "free"


async def process_tier_command(chat_id, user_id):
    """Show the caller their current tier + today's quota usage."""
    from services.tier_quota import peek
    tier = await _get_user_tier(user_id)
    label = _TIER_LABELS.get(tier, tier.upper())
    report_q = peek(user_id, tier, "/report")
    haber_q = peek(user_id, tier, "/haber")

    def _line(name: str, q) -> str:
        if q.limit is None:
            return f"  {name}: <b>{q.used}</b> kullanım (sınırsız)"
        return f"  {name}: <b>{q.used}/{q.limit}</b>"

    msg = (
        f"📋 <b>Mevcut planınız:</b> {label}\n\n"
        f"<b>Bugünkü kullanım:</b>\n"
        f"{_line('/report', report_q)}\n"
        f"{_line('/haber', haber_q)}\n\n"
        f"Yükseltmek için: /upgrade"
    )
    send_telegram_message(chat_id, msg)


async def process_upgrade_command(chat_id, user_id):
    """Show tier comparison + checkout buttons (Stripe) or admin-contact
    fallback (when Stripe env vars aren't set)."""
    tier = await _get_user_tier(user_id)
    current_label = _TIER_LABELS.get(tier, tier.upper())

    # Check if Stripe is configured before building the message; affects the
    # call-to-action footer + whether we attach the inline keyboard.
    try:
        from services.stripe_billing import is_configured as _stripe_configured
        stripe_ready = _stripe_configured()
    except Exception:
        stripe_ready = False

    body = (
        f"💳 <b>Plan Yükseltme</b>\n"
        f"Mevcut planınız: {current_label}\n\n"

        f"🆓 <b>FREE</b> — $0\n"
        f"  • Makro yayınları (5 dk gecikmeli)\n"
        f"  • Sınırlı /haber + /report kullanımı\n\n"

        f"💎 <b>PREMIUM</b> — $2/ay\n"
        f"  • Anlık makro yayınları (gecikme yok)\n"
        f"  • Tüm tarihsel chart + sektör hisse listeleri\n"
        f"  • Dashboard tam erişim\n\n"

        f"🚀 <b>ADVANCE</b> — $5/ay\n"
        f"  • PREMIUM tüm özellikler\n"
        f"  • Sınırsız /report + /haber\n"
        f"  • Erken yeni özellik erişimi\n\n"
    )

    if stripe_ready:
        # Generate both checkout URLs upfront so the buttons are deep links —
        # one tap takes the user straight to Stripe-hosted checkout in their
        # mobile browser. Fall back to admin contact for whichever fails.
        from services.stripe_billing import create_checkout_session
        premium_res = await create_checkout_session(str(user_id), "premium")
        advance_res = await create_checkout_session(str(user_id), "advance")
        keyboard_rows = []
        if premium_res.url:
            keyboard_rows.append([{"text": "💎 PREMIUM ($2/ay)", "url": premium_res.url}])
        if advance_res.url:
            keyboard_rows.append([{"text": "🚀 ADVANCE ($5/ay)", "url": advance_res.url}])
        if keyboard_rows:
            body += "⚡ Aşağıdaki butona tıklayarak güvenli ödeme sayfasına geçebilirsin."
            send_message_with_keyboard(chat_id, body, {"inline_keyboard": keyboard_rows})
            return
        # Both failed — drop through to admin-contact fallback.

    contact = html.escape(_UPGRADE_CONTACT)
    body += (
        f"⚡ <b>Yükseltmek için</b>: {contact} ile iletişime geçin.\n"
        f"<i>(Otomatik ödeme yakında — Stripe entegrasyonu beta'da.)</i>"
    )
    send_telegram_message(chat_id, body)


async def process_tags_command(chat_id, user_id):
    """Kullanıcıya tag seçim klavyesi gönderir."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.telegram_id == str(user_id)))
            user = result.scalars().first()
            user_tags = user.tags if user else ""
    except Exception as e:
        logger.error(f"DB hatası (/tags) user={user_id}: {e}")
        send_telegram_message(chat_id, "⚠️ Veritabanı bağlantısı kurulamadı. Lütfen tekrar deneyin.")
        return

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
    try:
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
    except Exception as e:
        logger.error(f"DB hatası (tag_callback) user={user_id}: {e}")
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
                    try:
                        # Normal mesaj komutları
                        if "message" in update and "text" in update["message"]:
                            chat_id = update["message"]["chat"]["id"]
                            user_id = update["message"]["from"]["id"]
                            username = update["message"]["from"].get("username", "Bilinmeyen")
                            text = update["message"]["text"]

                            if text.startswith("/start"):
                                await process_start_command(chat_id, user_id, username)
                            elif text.lower().startswith("/haber"):
                                await process_haber_command(chat_id, user_id)
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
                            elif text.lower().startswith("/report"):
                                symbol = text[len("/report"):].strip()
                                await process_report_command(chat_id, user_id, symbol)
                            elif text.lower().startswith("/upgrade"):
                                await process_upgrade_command(chat_id, user_id)
                            elif text.lower().startswith("/tier"):
                                await process_tier_command(chat_id, user_id)

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
                            elif cq_data.startswith("macro_hist:") or cq_data.startswith("macro_stocks:"):
                                from services.macro_callback import handle_callback
                                await handle_callback(cq_id, cq_chat_id, cq_data)

                    except Exception as cmd_err:
                        logger.error(f"Komut işleme hatası (update_id={update.get('update_id')}): {cmd_err}")

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
