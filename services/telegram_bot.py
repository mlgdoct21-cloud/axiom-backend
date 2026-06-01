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

def send_telegram_message(chat_id, text, disable_web_page_preview: bool = False):
    """HTML formatında mesaj gönderir.

    disable_web_page_preview=True kullan: link Telegram tarafından pre-fetch
    edilmemeli (örn. tek-kullanımlık /auth/telegram?token=... gibi linkler;
    önizleme tarayıcıdan önce token'ı tüketir → kullanıcı tıkladığında 401).
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_web_page_preview,
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

# getMe sonucunu cache'le — deep-link (t.me/<bot>?start=<payload>) için kullanılıyor.
_BOT_USERNAME_CACHE: Optional[str] = None


def get_bot_username() -> str:
    """Telegram bot kullanıcı adını lazy-fetch ile döner; başarısızsa '' (caller
    deep-link butonu eklemeyi atlar). getMe idempotent + auth'lu — ilk
    çağrıda çağrılır, sonra modül-seviye cache."""
    global _BOT_USERNAME_CACHE
    if _BOT_USERNAME_CACHE:
        return _BOT_USERNAME_CACHE
    if not TELEGRAM_BOT_TOKEN:
        return ""
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe",
            timeout=5,
        )
        if r.status_code == 200:
            uname = r.json().get("result", {}).get("username")
            if uname:
                _BOT_USERNAME_CACHE = uname
                return uname
    except Exception as e:
        logger.warning(f"getMe baglanti hatasi: {e}")
    return ""


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

async def process_start_command(chat_id, user_id, username, start_payload: str = ""):
    """Kullanıcıyı veritabanına kaydeder ve hoş geldin mesajı atar.

    `start_payload`: `/start <payload>` deep-link parametresi — örn.
    `t.me/<bot>?start=upgrade_premium` butonu kullanıcıyı tıkladığında
    gelen payload. `upgrade_premium` ise welcome'ı atlayıp doğrudan
    /upgrade akışını tetikler (free broadcast'taki [💎 Anında al] butonu)."""
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

    # Deep-link payload routing — niyet net olduğunda welcome'ı atla.
    if start_payload == "upgrade_premium":
        await process_upgrade_command(chat_id, user_id)
        return
    # 2026-06-01: Dashboard "Giriş Yap" butonu t.me/<bot>?start=login açar.
    # Welcome'ı atla, direkt magic link gönder — kullanıcı manuel /login
    # yazmak zorunda kalmasın (mobile UX).
    if start_payload == "login":
        await process_login_command(chat_id, user_id, username)
        return

    welcome_msg = (
        "🚀 <b>Axiom'a Hoş Geldin</b>\n\n"
        "📊 Kripto & Finansal Piyasaların Gerçek Dedikodusu\n"
        "Başlayan her fırsatı kaçırma.\n\n"
        "⚡ <b>Ne Yapmak İstiyorsun?</b>\n"
        "/haber — Anlık pazar güncellemeleri\n"
        "/report AAPL — Hisse insider raporu (AI analiz)\n"
        "/onchain — BTC on-chain sinyaller (PREMIUM)\n"
        "/sentez — Haftalık kurumsal makro sentez\n"
        "/tags — İlgi alanlarını seç (BTC, Altın, BIST...)\n"
        "/takip AAPL — Sembol takip et\n"
        "/takipcikar AAPL — Takipten çıkar\n"
        "/takiplistem — Takip listeni gör\n\n"
        "💳 <b>Üyelik:</b>\n"
        "/tier — Mevcut planını gör\n"
        "/upgrade — PREMIUM / ADVANCE planlarına yükselt\n"
        "/login — Dashboard'a tek-tıkla giriş linki"
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
        dashboard_url = os.getenv("DASHBOARD_URL", "https://axiom-dashboard-sigma.vercel.app").rstrip("/")
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


async def process_sentez_command(chat_id, user_id):
    """Haftalık Kurumsal Sentez — tier-gated. DB read (Gemini yok, ağır
    quota yok). free → teaser + /upgrade CTA; premium/advance → tam md."""
    from core.database import engine
    from sqlalchemy import text

    tier = await _get_user_tier(user_id)
    want = "advance" if tier == "advance" else "premium"
    sql = text(
        "SELECT tier, week_start, synthesis_md FROM corporate_syntheses "
        "WHERE tier = :t ORDER BY week_start DESC, generated_at DESC LIMIT 1"
    )
    row = None
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(sql, {"t": want})).mappings().first()
            if not row and want == "advance":
                row = (await conn.execute(
                    sql, {"t": "premium"}
                )).mappings().first()
    except Exception as e:  # noqa: BLE001
        logger.error(f"/sentez DB hatası user={user_id}: {e}")
        send_telegram_message(
            chat_id,
            "⚠️ Kurumsal sentez şu an alınamadı, lütfen daha sonra tekrar deneyin."
        )
        return

    if not row:
        send_telegram_message(
            chat_id,
            "🏛️ <b>Haftalık Kurumsal Sentez</b> henüz üretilmedi.\n"
            "Her Pazartesi 08:30'da yayınlanır."
        )
        return

    dashboard_url = os.getenv(
        "DASHBOARD_URL", "https://axiom-dashboard-sigma.vercel.app"
    ).rstrip("/")
    md = row.get("synthesis_md") or ""
    week_s = html.escape(str(row.get("week_start")))

    if tier == "free":
        teaser = md[:400].rstrip()
        if len(md) > 400:
            teaser += "…"
        send_telegram_message(
            chat_id,
            f"🏛️ <b>AXIOM Kurumsal Sentez</b> · <i>Hafta {week_s}</i>\n\n"
            f"{html.escape(teaser)}\n\n"
            f"🔒 Tam karşılaştırmalı sentez Premium/Advance üyelere açıktır.\n"
            f"💎 /upgrade ile yükseltin · {dashboard_url}/pricing?ref=telegram"
        )
        return

    badge = "🚀 ADVANCE" if tier == "advance" else "💎 PREMIUM"
    body = md
    if len(body) > 3600:
        body = body[:3600].rstrip() + "\n…(devamı dashboard'da)"
    send_telegram_message(
        chat_id,
        f"🏛️ <b>AXIOM Kurumsal Sentez</b> · {badge}\n"
        f"<i>Hafta {week_s}</i>\n\n{html.escape(body)}\n\n"
        f"📊 {dashboard_url}/dashboard?ref=tg_sentez"
    )


# ── /onchain hikâye anlatıcı katmanı ──────────────────────────────────────────
# Power-user için 14+ metrik dökümü değil, normal kullanıcı için "büyük resim
# + 3 ana hareket + içeri çekecek hook" formatı. Tam tablo [📊 Tam Tablo]
# butonunun arkasında saklı (callback ile mesajı edit ediyoruz, yeni mesaj
# açmıyoruz — ekran kirlenmesin).

# Her metrik için "neden önemli" tek-cümle taglines (BULLISH / BEARISH).
# label_tr veriyi söyler, why_tr ne anlama geldiğini söyler. Bu mapping
# presentation katmanı — contract'lara karşı doğrulamaya gerek yok.
_ONCHAIN_WHY: dict[str, dict[str, str]] = {
    "exchange_netflow": {
        "BULLISH": "Borsadan çıkış = satış baskısı düşüyor",
        "BEARISH": "Borsaya akış = satış için pozisyonlanılıyor",
    },
    "whale_ratio": {
        "BULLISH": "Büyük cüzdanlar borsada sessiz",
        "BEARISH": "Balinalar borsada aktif — hareketli sular",
    },
    "mpi": {
        "BULLISH": "Madenciler eldeki BTC'yi tutuyor → arz kıtlığı",
        "BEARISH": "Madenci satış baskısı eşik üstünde",
    },
    "stablecoin_inflow": {
        "BULLISH": "Stablecoin alıma hazırlanıyor",
        "BEARISH": "Stablecoin çıkıyor — alıcı çekiliyor",
    },
    "coinbase_premium": {
        "BULLISH": "Coinbase tezgahında alış baskın",
        "BEARISH": "Coinbase tezgahında satış baskın",
    },
    "funding_rates": {
        "BULLISH": "Türev piyasası sakin, sağlıklı yükseliş",
        "BEARISH": "Funding aşırı — sıkışma riski",
    },
    "leverage_ratio": {
        "BULLISH": "Kaldıraç düşük, sağlam zemin",
        "BEARISH": "Kaldıraç yüksek — likidasyon riski",
    },
    "mvrv": {
        "BULLISH": "Tarihsel olarak ucuz bölgede",
        "BEARISH": "Tarihsel olarak pahalı — kâr realizasyonu yakın",
    },
    "nupl": {
        "BULLISH": "Henüz aşırı kâr/öfori yok",
        "BEARISH": "Aşırı kâr bölgesi — dikkat",
    },
    "sopr": {
        "BULLISH": "Eldeki coinler tutuluyor",
        "BEARISH": "Kâr realizasyonu başlıyor",
    },
    "sopr_ratio": {
        "BULLISH": "Uzun vadeli sahipler satmıyor",
        "BEARISH": "Uzun vadeli sahipler kâr alıyor",
    },
    "puell": {
        "BULLISH": "Madenci geliri baskı altında — dipler yakın",
        "BEARISH": "Madenci geliri zirve — satış baskısı yakın",
    },
    "btc_liquidations": {
        "BULLISH": "Likidasyonlar düştü — sağlıklı seyir",
        "BEARISH": "Likidasyon spike — yüksek volatilite",
    },
    "korean_premium": {
        "BULLISH": "Asya talebi güçlü",
        "BEARISH": "Asya tarafında satış baskısı",
    },
    "spot_taker": {
        "BULLISH": "Spot alıcı agresif",
        "BEARISH": "Spot satıcı agresif",
    },
    "hash_rate": {
        "BULLISH": "Ağ güvenliği güçleniyor",
        "BEARISH": "Hash düşüyor — madenci stresi",
    },
    "active_addresses": {
        "BULLISH": "Aktif adres artıyor — kullanım yükseliyor",
        "BEARISH": "Aktif adres düşüyor — ilgi azalıyor",
    },
    "realized_price": {
        "BULLISH": "Piyasa ortalama maliyetin üstünde",
        "BEARISH": "Piyasa ortalama maliyetin altında",
    },
}

# Her metriğe görsel emoji (hikâye satırlarının başında).
_ONCHAIN_EMOJI: dict[str, str] = {
    "exchange_netflow": "📥",
    "whale_ratio": "🐋",
    "mpi": "⛏️",
    "stablecoin_inflow": "💵",
    "coinbase_premium": "🇺🇸",
    "funding_rates": "⚡",
    "leverage_ratio": "🌡️",
    "mvrv": "📐",
    "nupl": "🧠",
    "sopr": "📊",
    "sopr_ratio": "📊",
    "puell": "💎",
    "btc_liquidations": "💥",
    "korean_premium": "🇰🇷",
    "spot_taker": "🛒",
    "hash_rate": "⛓️",
    "active_addresses": "👥",
    "realized_price": "🏷️",
}

# Metrik adı (Türkçe, başlık olarak kullanılır — sinyal status etiketi DEĞİL).
# KISA TUT — uzun açıklamalar zone-hint parantezini bozar. Örnek:
#   ✓ "MPI -0.4 (eşik altı)"   ✗ "MPI (Madenci Pozisyon Endeksi) -0.4 (eşik altı)"
_METRIC_NAME_TR: dict[str, str] = {
    "exchange_netflow": "Borsa Net Akışı",
    "whale_ratio": "Balina Oranı",
    "mpi": "MPI",
    "stablecoin_inflow": "Stablecoin Net Akışı",
    "coinbase_premium": "Coinbase Primi",
    "funding_rates": "Funding",
    "leverage_ratio": "Kaldıraç",
    "mvrv": "MVRV",
    "nupl": "NUPL",
    "sopr": "SOPR",
    "sopr_ratio": "SOPR Ratio",
    "puell": "Puell",
    "btc_liquidations": "Likidasyon",
    "korean_premium": "Kore Primi",
    "spot_taker": "Spot Taker",
    "hash_rate": "Hash Rate",
    "active_addresses": "Aktif Adres",
    "realized_price": "Realized Price",
}

# Zone hint = kullanıcının istediği "(eşik altı)" / "(orta bölge)" tarzı
# pozisyonel descriptor. label_tr ('🟢 Madenci Güveni') sinyal-statüsü
# söyler, oysa zone hint NEREDE olduğunu söyler. Per-metric × per-signal,
# 1-3 kelime, parantez İÇİNDE değil — render parantez ekler.
_METRIC_ZONE_HINT: dict[str, dict[str, str]] = {
    "exchange_netflow": {"BULLISH": "net çıkış", "NEUTRAL": "yatay", "BEARISH": "net giriş"},
    "whale_ratio":      {"BULLISH": "sakin", "NEUTRAL": "normal", "BEARISH": "yüksek aktivite"},
    "mpi":              {"BULLISH": "eşik altı", "NEUTRAL": "hafif satış", "BEARISH": "eşik üstü"},
    "stablecoin_inflow":{"BULLISH": "alıma hazırlanıyor", "NEUTRAL": "yatay", "BEARISH": "çıkış baskın"},
    "coinbase_premium": {"BULLISH": "alış baskın", "NEUTRAL": "dengeli", "BEARISH": "satış baskın"},
    "funding_rates":    {"BULLISH": "short sıkışması", "NEUTRAL": "dengeli", "BEARISH": "hafif aşırı"},
    "leverage_ratio":   {"BULLISH": "düşük risk", "NEUTRAL": "normal", "BEARISH": "yüksek risk"},
    "mvrv":             {"BULLISH": "dip bölge", "NEUTRAL": "orta bölge", "BEARISH": "tepe bölge"},
    "nupl":             {"BULLISH": "korku bölgesi", "NEUTRAL": "iyimserlik", "BEARISH": "öfori"},
    "sopr":             {"BULLISH": "dip", "NEUTRAL": "başabaş", "BEARISH": "kâr realizasyonu"},
    "sopr_ratio":       {"BULLISH": "dengede", "NEUTRAL": "hafif sapma", "BEARISH": "LTH dağıtım"},
    "puell":            {"BULLISH": "dip bölge", "NEUTRAL": "normal", "BEARISH": "tepe bölge"},
    "btc_liquidations": {"BULLISH": "long tasfiye sonrası", "NEUTRAL": "düşük", "BEARISH": "spike"},
    "korean_premium":   {"BULLISH": "Asya alış", "NEUTRAL": "dengeli", "BEARISH": "Asya satış"},
    "spot_taker":       {"BULLISH": "alıcı agresif", "NEUTRAL": "dengeli", "BEARISH": "satıcı agresif"},
    "hash_rate":        {"BULLISH": "yükseliyor", "NEUTRAL": "stabil", "BEARISH": "düşüyor"},
    "active_addresses": {"BULLISH": "yükseliyor", "NEUTRAL": "stabil", "BEARISH": "düşüyor"},
}


def _strip_value_parens(value_str: str) -> str:
    """value_str içindeki '(günlük)' / '(... vs ...)' gibi parantezleri kaldır
    — zone hint parantezi ile çakışmasın. '+0.0184% (günlük)' → '+0.0184%'."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", value_str).strip()


def _format_value_with_zone(metric: str, value_str: str, signal: str) -> str:
    """Hero/watchlist için '0.018% (hafif aşırı)' formatı.
    value_str'den eski parantezi kaldır, zone hint ekle."""
    base = _strip_value_parens(value_str)
    hint = _METRIC_ZONE_HINT.get(metric, {}).get(signal)
    return f"{base} ({hint})" if hint else base

# Üst-satır kısa özet için 2-3 kelimelik durum descriptor'ları.
# `score_summary` snapshot'tan geliyor ama label_tr (status word) kullanıyor —
# kullanıcı için anlamsız ("Normal, Kohortlar Dengede, Madenci Güveni"). Burada
# direksiyon başına insan diliyle kısa ifade üretiyoruz.
_METRIC_STATE_SHORT: dict[str, dict[str, str]] = {
    "exchange_netflow": {"BULLISH": "borsadan çıkış", "BEARISH": "borsaya akış"},
    "whale_ratio":      {"BULLISH": "balina hareketi sakin", "BEARISH": "balinalar aktif"},
    "mpi":              {"BULLISH": "madenci tutuyor", "BEARISH": "madenci satıyor"},
    "stablecoin_inflow":{"BULLISH": "stablecoin alıma hazır", "BEARISH": "stablecoin çıkıyor"},
    "coinbase_premium": {"BULLISH": "Coinbase'de alış", "BEARISH": "Coinbase'de satış"},
    "funding_rates":    {"BULLISH": "funding sağlıklı", "BEARISH": "funding aşırı"},
    "leverage_ratio":   {"BULLISH": "kaldıraç düşük", "BEARISH": "kaldıraç yüksek"},
    "mvrv":             {"BULLISH": "ucuz bölge", "BEARISH": "pahalı bölge"},
    "nupl":             {"BULLISH": "öfori yok", "BEARISH": "aşırı kâr"},
    "sopr":             {"BULLISH": "tutuluyor", "BEARISH": "kâr realizasyonu"},
    "sopr_ratio":       {"BULLISH": "uzun vade tutuyor", "BEARISH": "uzun vade satıyor"},
    "puell":            {"BULLISH": "dip bölge", "BEARISH": "zirve bölge"},
    "btc_liquidations": {"BULLISH": "likidasyon düşük", "BEARISH": "likidasyon spike"},
    "korean_premium":   {"BULLISH": "Asya talebi güçlü", "BEARISH": "Asya'da satış"},
    "spot_taker":       {"BULLISH": "spot alıcı agresif", "BEARISH": "spot satıcı agresif"},
    "hash_rate":        {"BULLISH": "ağ güçleniyor", "BEARISH": "hash düşüyor"},
    "active_addresses": {"BULLISH": "kullanım artıyor", "BEARISH": "kullanım düşüyor"},
    "realized_price":   {"BULLISH": "maliyetin üstünde", "BEARISH": "maliyetin altında"},
}


def _build_short_summary(snap: dict, max_pos: int = 2, max_neg: int = 2) -> str:
    """Üst-satır özeti — score_summary'i override eder. Top katkıları
    `_METRIC_STATE_SHORT`'tan kısa ifadelerle birleştirir.
    Örn: 'Güç: balina hareketi sakin, madenci tutuyor · Baskı: funding aşırı'
    """
    breakdown = snap.get("score_breakdown") or []
    if not breakdown:
        return ""
    pos = sorted([b for b in breakdown if b.get("contribution", 0) > 0],
                 key=lambda x: -x["contribution"])[:max_pos]
    neg = sorted([b for b in breakdown if b.get("contribution", 0) < 0],
                 key=lambda x: x["contribution"])[:max_neg]
    parts = []
    if pos:
        words = [_METRIC_STATE_SHORT.get(p["metric"], {}).get("BULLISH") or p["label_tr"]
                 for p in pos]
        parts.append("Güç: " + ", ".join(words))
    if neg:
        words = [_METRIC_STATE_SHORT.get(n["metric"], {}).get("BEARISH") or n["label_tr"]
                 for n in neg]
        parts.append("Baskı: " + ", ".join(words))
    return " · ".join(parts) if parts else ""


def _pick_hero_stories(snap: dict, count: int = 3) -> list[dict]:
    """Score breakdown'dan en güçlü 3 sinyali (mutlak katkı) dön. Karışık
    durumda çoğunluk yönünden seç + 1 karşı görüşü göster ('izleme
    listesinde' bölümü için ayrı çağrıda)."""
    breakdown = snap.get("score_breakdown") or []
    sigs = snap.get("signals") or {}
    if not breakdown:
        return []

    # Her breakdown item → enrich with why_tr from sigs
    enriched = []
    for b in breakdown:
        if b.get("contribution", 0) == 0:
            continue
        sig = b.get("signal", "NEUTRAL")
        why = _ONCHAIN_WHY.get(b["metric"], {}).get(sig, "")
        emoji = _ONCHAIN_EMOJI.get(b["metric"], "•")
        enriched.append({
            **b,
            "why_tr": why,
            "emoji": emoji,
        })

    # Mutlak katkıya göre sırala, en güçlü 3'ü al
    enriched.sort(key=lambda x: -abs(x["contribution"]))
    return enriched[:count]


def _pick_watchlist_signal(snap: dict, hero_metrics: set[str]) -> Optional[dict]:
    """Hero stories'in dışında kalan EN güçlü karşı sinyal — 'baskı' satırı.
    Yoksa None. (Hero hepsi BULLISH ise bir BEARISH bul, tersi de geçerli.)"""
    breakdown = snap.get("score_breakdown") or []
    if not breakdown:
        return None
    hero_signals = {b["signal"] for b in breakdown if b["metric"] in hero_metrics}
    # Karşı yön tek değer ise (hepsi BULLISH veya hepsi BEARISH) ters tarafı bul
    if hero_signals == {"BULLISH"}:
        target = "BEARISH"
    elif hero_signals == {"BEARISH"}:
        target = "BULLISH"
    else:
        return None  # zaten karışık, watchlist'e gerek yok
    candidates = [b for b in breakdown if b["signal"] == target and b["metric"] not in hero_metrics]
    if not candidates:
        return None
    candidates.sort(key=lambda x: -abs(x["contribution"]))
    b = candidates[0]
    return {
        **b,
        "why_tr": _ONCHAIN_WHY.get(b["metric"], {}).get(target, ""),
        "emoji": _ONCHAIN_EMOJI.get(b["metric"], "•"),
    }


def _build_onchain_keyboard(symbol: str) -> dict:
    """[📊 Tam Tablo] (callback) + [💎 Dashboard'da Tam Analiz →] (URL)."""
    dashboard_url = os.getenv("DASHBOARD_URL", "https://axiom-dashboard-sigma.vercel.app").rstrip("/")
    deep_link = f"{dashboard_url}/dashboard/crypto?symbol={symbol}&tab=onchain&ref=tg_onchain"
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Tam Tablo", "callback_data": f"onchain_full:{symbol}"},
            ],
            [
                {"text": "💎 Dashboard'da Tam AXIOM Analizi →", "url": deep_link},
            ],
        ]
    }


def _render_onchain_brief(snap: dict, symbol: str) -> str:
    """Yeni hikâye-anlatıcı format: verdict + 3 büyük hareket + watchlist
    + dashboard upsell. Telegram HTML.

    NOT: Hero başlığı = METRİK ADI (`_METRIC_NAME_TR`) + value_str.
    `label_tr` sinyal status etiketi olduğu için ('Normal', 'Kohortlar
    Dengede') başlık olarak KULLANILMAZ — onun yerini why_tr ('└' satırı)
    insan diliyle anlatım yapar.
    """
    overall_tr = snap.get("overall_tr", "❓ Veri Yok")
    axiom_score = snap.get("axiom_score")
    score_zone_tr = snap.get("score_zone_tr", "")

    # Header — verdict + score
    if axiom_score is not None:
        header = (
            f"🔗 <b>{symbol} ON-CHAIN — {score_zone_tr}</b>\n"
            f"<b>AXIOM Skor: {axiom_score}/100</b>  ·  {overall_tr}\n"
        )
    else:
        header = f"🔗 <b>{symbol} ON-CHAIN — {overall_tr}</b>\n"

    # Üst-satır kısa özet — score_summary override (jargon yerine descriptor)
    short_summary = _build_short_summary(snap, max_pos=2, max_neg=1)
    summary_line = f"<i>{html.escape(short_summary)}</i>\n" if short_summary else ""

    # 3 hero story — METRİK ADI başlık + value_str + why_tr alt satır
    heroes = _pick_hero_stories(snap, count=3)
    if not heroes:
        stories_block = "<i>Şu an aktif sinyal yok — piyasa nötr.</i>\n"
        hero_metrics = set()
    else:
        lines = ["", "<b>📈 SAHNEDEKİ 3 BÜYÜK HAREKET</b>"]
        for h in heroes:
            metric_name = _METRIC_NAME_TR.get(h["metric"], h["label_tr"])
            value_with_zone = _format_value_with_zone(
                h["metric"], str(h["value_str"]), h.get("signal", "NEUTRAL")
            )
            lines.append(
                f"\n{h['emoji']} <b>{html.escape(metric_name)}</b>"
                f"  <code>{html.escape(value_with_zone)}</code>"
            )
            if h.get("why_tr"):
                lines.append(f"   └ <i>{html.escape(h['why_tr'])}</i>")
        stories_block = "\n".join(lines) + "\n"
        hero_metrics = {h["metric"] for h in heroes}

    # Watchlist (karşı sinyal) — aynı kural: metrik adı başlık + zone hint
    watch = _pick_watchlist_signal(snap, hero_metrics)
    watch_block = ""
    if watch:
        metric_name = _METRIC_NAME_TR.get(watch["metric"], watch["label_tr"])
        value_with_zone = _format_value_with_zone(
            watch["metric"], str(watch["value_str"]), watch.get("signal", "NEUTRAL")
        )
        watch_block = (
            f"\n👀 <b>İzleme listesinde</b>\n"
            f"{watch['emoji']} <b>{html.escape(metric_name)}</b>"
            f"  <code>{html.escape(value_with_zone)}</code>\n"
            f"   └ <i>{html.escape(watch.get('why_tr', ''))}</i>\n"
        )

    # Upsell — dashboard'a çek
    upsell = (
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 <b>Bu mesajda göremedikleriniz:</b>\n"
        f"  • Storyteller AI: bu sinyaller bir araya gelince ne anlama geliyor?\n"
        f"  • Döngü Pusulası: bull/bear döngüsünün neresindeyiz?\n"
        f"  • Balina Radarı + Risk Isı haritası\n"
        f"  • Tarihsel benzerlik: bu seviye en son ne zaman görüldü?\n\n"
        f"<i>Veri: CryptoQuant Pro · 20+ metrik · canlı</i>"
    )

    return header + summary_line + stories_block + watch_block + upsell


def _render_onchain_full_table(snap: dict, symbol: str) -> str:
    """Eski format — power-user için 14+ metriklik tam döküm. [📊 Tam Tablo]
    callback'inde mesajı bununla edit ediyoruz."""
    sigs = snap.get("signals", {})
    overall_tr = snap.get("overall_tr", "❓ Veri Yok")
    axiom_score = snap.get("axiom_score")
    score_zone_tr = snap.get("score_zone_tr", "")
    score_summary = snap.get("score_summary", "")

    def _line(emoji: str, label: str, key: str) -> str:
        s = sigs.get(key)
        if not s:
            return ""
        return f"{emoji} <b>{label}:</b> <code>{html.escape(str(s['value_str']))}</code>  {html.escape(s['label_tr'])}\n"

    score_line = ""
    if axiom_score is not None:
        score_line = (
            f"\n🎯 <b>AXIOM SKOR: {axiom_score}/100</b>  {score_zone_tr}\n"
            f"<i>{html.escape(score_summary)}</i>\n"
        )

    body = (
        f"🔗 <b>{symbol} — TAM TABLO</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━{score_line}\n"
        f"<b>Akıllı Para:</b>\n"
        f"{_line('📥', 'Borsa Akışı', 'exchange_netflow')}"
        f"{_line('🐋', 'Balina Oranı', 'whale_ratio')}"
        f"{_line('⛏️', 'MPI (Madenci)', 'mpi')}"
        f"{_line('💵', 'Stablecoin Net', 'stablecoin_inflow')}"
        f"\n<b>Döngü Pusulası:</b>\n"
        f"{_line('📐', 'MVRV', 'mvrv')}"
        f"{_line('🧠', 'NUPL', 'nupl')}"
        f"{_line('📊', 'SOPR', 'sopr')}"
        f"{_line('📊', 'SOPR Ratio', 'sopr_ratio')}"
        f"{_line('💎', 'Puell', 'puell')}"
    )

    fr = sigs.get("funding_rates")
    oi = sigs.get("open_interest")
    lev = sigs.get("leverage_ratio")
    cb = sigs.get("coinbase_premium")
    liq = sigs.get("btc_liquidations")
    kp = sigs.get("korean_premium")
    st = sigs.get("spot_taker")
    if fr or oi or lev or cb or liq or kp or st:
        body += "\n<b>Risk & Türev:</b>\n"
        if lev: body += _line('🌡️', 'Kaldıraç', 'leverage_ratio')
        if fr:  body += _line('⚡', 'Funding (24s)', 'funding_rates')
        if oi:  body += f"  📈 Open Interest: <code>{html.escape(str(oi['value_str']))}</code>\n"
        if cb:  body += _line('🇺🇸', 'Coinbase Primi', 'coinbase_premium')
        if liq: body += _line('💥', 'Likidasyonlar', 'btc_liquidations')
        if kp:  body += _line('🇰🇷', 'Kore Primi', 'korean_premium')
        if st:  body += _line('🛒', 'Spot Taker', 'spot_taker')

    rp = sigs.get("realized_price")
    hr = sigs.get("hash_rate")
    if rp or hr:
        body += "\n<b>Bağlam:</b>\n"
        if rp: body += f"  🏷️ Piyasa Ortalama Maliyet: <code>{html.escape(str(rp['value_str']))}</code>\n"
        if hr: body += _line('⛓️', 'Hash Rate', 'hash_rate')

    body += (
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Genel:</b> {overall_tr}\n"
        f"<i>Veri: CryptoQuant Pro · /onchain ile özet görünüme dön</i>"
    )
    return body


async def process_onchain_command(chat_id, user_id, symbol: str = "BTC"):
    """On-chain snapshot — verdict + 3 hero stories + dashboard upsell.
    Free users: stronger pull-in teaser (örnek output preview + /upgrade)."""
    cooldown = _check_rate_limit(user_id, "/onchain", _LIGHT_CMD_COOLDOWN_SEC)
    if cooldown:
        send_telegram_message(chat_id, f"⏳ Lütfen {cooldown}s bekle ve tekrar dene.")
        return

    symbol = (symbol or "BTC").strip().upper() or "BTC"
    if not _is_valid_symbol(symbol):
        send_telegram_message(chat_id, "❌ Geçersiz sembol. Şu an yalnızca <b>BTC</b> destekleniyor.")
        return

    tier = await _get_user_tier(user_id)
    if tier == "free":
        # Daha güçlü teaser — somut örnekle merak uyandır, sadece "PREMIUM"
        # demek yerine ne kaçırdıklarını göster.
        send_telegram_message(
            chat_id,
            "🔒 <b>On-chain sinyaller PREMIUM özelliktir</b>\n\n"
            "Şu an piyasada büyük oyuncular ne yapıyor?\n"
            "  🐋 Balinalar dün borsadan kaç BTC çekti?\n"
            "  🇺🇸 ETF'lere ne kadar para girdi?\n"
            "  ⛏️ Madenciler satıyor mu, tutuyor mu?\n"
            "  ⚡ Türev tarafı sıkışıyor mu?\n\n"
            "AXIOM Skor 0-100 — tek bakışta karar.\n"
            "Dashboard'da tam analiz: storyteller AI, döngü pusulası, "
            "balina radarı, tarihsel benzerlik.\n\n"
            "💎 Yükseltmek için: /upgrade  (sadece $1.99/ay)"
        )
        return

    from services.cryptoquant_service import get_onchain_snapshot, _is_configured
    if not _is_configured():
        send_telegram_message(chat_id, "⚠️ On-chain entegrasyonu şu an aktif değil. Daha sonra tekrar deneyin.")
        return

    send_telegram_message(chat_id, f"🔗 <b>{symbol}</b> on-chain hazırlanıyor...")

    try:
        snap = await get_onchain_snapshot(symbol)
    except Exception as e:
        logger.error(f"on-chain snapshot fetch error: {e}")
        send_telegram_message(chat_id, "⚠️ Veri alınamadı. Birkaç dakika sonra tekrar deneyin.")
        return

    if not snap or snap.get("error"):
        send_telegram_message(chat_id, "⚠️ Şu an on-chain veriye ulaşılamıyor (CryptoQuant). Birkaç dakika sonra tekrar deneyin.")
        return

    body = _render_onchain_brief(snap, symbol)
    keyboard = _build_onchain_keyboard(symbol)
    send_message_with_keyboard(chat_id, body, keyboard)


async def process_onchain_full_callback(callback_query_id, chat_id, message_id, user_id, symbol: str):
    """[📊 Tam Tablo] callback — mesajı 14+ metriklik tam dökümle edit eder.
    Yeni mesaj göndermiyor, mevcut baloncuğu güncelliyor (ekran kirlenmesin)."""
    answer_callback_query(callback_query_id)

    tier = await _get_user_tier(user_id)
    if tier == "free":
        # Edge case — free user butona bastı (mesaj orijinal olarak premium'a
        # gönderildi ama tier sonradan değişmiş olabilir).
        return

    symbol = (symbol or "BTC").strip().upper() or "BTC"
    if not _is_valid_symbol(symbol):
        return

    from services.cryptoquant_service import get_onchain_snapshot, _is_configured
    if not _is_configured():
        return

    try:
        snap = await get_onchain_snapshot(symbol)
    except Exception as e:
        logger.error(f"on-chain full callback fetch error: {e}")
        return

    if not snap or snap.get("error"):
        return

    body = _render_onchain_full_table(snap, symbol)
    # Tam tablo'dan brief'e dönüş için aynı keyboard'u koru
    keyboard = _build_onchain_keyboard(symbol)
    edit_message_text_with_keyboard(chat_id, message_id, body, keyboard)


async def process_login_command(chat_id, user_id, username):
    """Generate one-time deep-link token + DM dashboard URL.

    Production-correct alternative to direct /auth/login (which requires
    X-Bot-Secret). User clicks link → dashboard exchanges token → JWT.
    Token TTL 5 minutes, one-time use enforced server-side.
    """
    from services.telegram_login_token import create_token

    # Ensure user is registered
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.telegram_id == str(user_id)))
            user = result.scalars().first()
            if not user:
                user = User(telegram_id=str(user_id), username=username)
                session.add(user)
                await session.commit()
    except Exception as e:
        logger.error(f"DB error (/login) user={user_id}: {e}")
        send_telegram_message(chat_id, "⚠️ Veritabanı hatası, daha sonra tekrar deneyin.")
        return

    token = await create_token(str(user_id))
    if not token:
        send_telegram_message(chat_id, "⚠️ Token üretilemedi, lütfen tekrar deneyin.")
        return

    dashboard_url = os.getenv("DASHBOARD_URL", "https://axiom-dashboard-sigma.vercel.app").rstrip("/")
    deep_link = f"{dashboard_url}/auth/telegram?token={token}"

    msg = (
        "🔐 <b>Dashboard Giriş Linki</b>\n\n"
        f'<a href="{deep_link}">👉 Dashboard\'a giriş yap</a>\n\n'
        "Bu link <b>5 dakika</b> geçerlidir ve <b>tek kullanımlıktır</b>.\n"
        "Linki kimseyle paylaşmayın — Axiom hesabınıza erişim sağlar."
    )
    # disable_web_page_preview=True: kritik. Telegram normalde URL'leri preview
    # için pre-fetch eder; bu tek-kullanımlık token'ı kullanıcı tıklamadan
    # tüketir → click-time 401. Bu flag preview'i kapatıp token'ı korur.
    send_telegram_message(chat_id, msg, disable_web_page_preview=True)


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

    # TCMB anlık kuruyla TL yaklaşığı — fetch fail ise satır çıkmaz.
    from services.tcmb_rate import format_try_approx
    premium_try = await format_try_approx(1.99)
    advance_try = await format_try_approx(4.99)
    premium_price_label = f"$1.99/ay  ({premium_try})" if premium_try else "$1.99/ay"
    advance_price_label = f"$4.99/ay  ({advance_try})" if advance_try else "$4.99/ay"

    body = (
        f"💳 <b>Plan Yükseltme</b>\n"
        f"Mevcut planınız: {current_label}\n\n"

        f"🆓 <b>FREE</b> — $0\n"
        f"  • Makro yayınları (5 dk gecikmeli)\n"
        f"  • Sınırlı /haber + /report kullanımı\n\n"

        f"💎 <b>PREMIUM</b> — {premium_price_label}\n"
        f"  • Anlık makro yayınları (gecikme yok)\n"
        f"  • Tüm tarihsel chart + sektör hisse listeleri\n"
        f"  • Dashboard tam erişim\n\n"

        f"🚀 <b>ADVANCE</b> — {advance_price_label}\n"
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
            label = f"💎 PREMIUM ($1.99 {premium_try})" if premium_try else "💎 PREMIUM ($1.99/ay)"
            keyboard_rows.append([{"text": label, "url": premium_res.url}])
        if advance_res.url:
            label = f"🚀 ADVANCE ($4.99 {advance_try})" if advance_try else "🚀 ADVANCE ($4.99/ay)"
            keyboard_rows.append([{"text": label, "url": advance_res.url}])
        if keyboard_rows:
            footer = "⚡ Aşağıdaki butona tıklayarak güvenli ödeme sayfasına geçebilirsin."
            if premium_try:
                footer += "\n<i>TL tutarı TCMB anlık kuruyla yaklaşık; tahsilat USD üzerinden, bankan kendi kuruyla TL'ye çevirir.</i>"
            body += footer
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


# ── Opsiyon Akademisi (Faz 1) ─────────────────────────────────────────────────


def _acad_dashboard_link() -> str:
    """Dashboard'daki akademi sayfasına derin link — env'den alır, fallback Vercel URL.
    /akademi top-level route (AuthGate dışında — free kullanıcı da görür)."""
    import os
    base = os.getenv("DASHBOARD_URL", "https://axiom-dashboard-sigma.vercel.app").rstrip("/")
    return f"{base}/akademi"


def _acad_module_menu_keyboard(user_tier: str) -> dict:
    """4 modüllük menü. Kilitli olanlar 🔒 ile işaretlenir."""
    from services.academy_service import get_curriculum_summary
    data = get_curriculum_summary(user_tier)
    rows = []
    for m in data.get("modules", []):
        lock = "🔒 " if m.get("locked") else ""
        rows.append([{
            "text": f"{lock}{m['id']} — {m['title'].split('—')[0].strip()}",
            "callback_data": f"acad_mod:{m['id']}",
        }])
    rows.append([
        {"text": "📖 Sözlük (/opsiyon)", "callback_data": "acad_help_glossary"},
        {"text": "🌐 Dashboard'da aç", "url": _acad_dashboard_link()},
    ])
    if user_tier == "free":
        rows.append([{"text": "💎 Premium ile tüm modüller", "callback_data": "acad_upgrade"}])
    return {"inline_keyboard": rows}


def _acad_module_detail_keyboard(module_id: str) -> dict:
    """Modülün dersleri + geri dön butonu."""
    from services.academy_service import get_module
    mod = get_module(module_id, "advance")  # tier-agnostic list; lock kontrolü ders açılırken
    rows = []
    if mod:
        for les in mod.get("lessons", []):
            rows.append([{
                "text": f"{les['id']} — {les['title']}",
                "callback_data": f"acad_les:{les['id']}",
            }])
    rows.append([{"text": "⬅️ Modüllere dön", "callback_data": "acad_back"}])
    return {"inline_keyboard": rows}


def _acad_lesson_back_keyboard(module_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": f"⬅️ {module_id} dersleri", "callback_data": f"acad_mod:{module_id}"},
        {"text": "🏠 Modüller", "callback_data": "acad_back"},
    ]]}


def _render_acad_welcome(user_tier: str) -> str:
    return (
        "🎓 <b>AXIOM Opsiyon Akademisi</b>\n\n"
        "Opsiyon ve <b>hedge kültürünü</b> Türkçe sezgi diliyle öğren.\n"
        "<i>Greek renaming</i>: Delta = Fiyat Duyarlılığı, Theta = Zaman Kaybı Hızı.\n"
        "<i>Saat çarkı (horology)</i> metaforuyla opsiyonu anlat.\n\n"
        f"<b>Tier:</b> {html.escape(user_tier)}\n"
        "<i>Eğitim amaçlıdır, yatırım tavsiyesi değildir (SPK).</i>\n\n"
        "Bir modül seç ↓"
    )


def _render_acad_module(mod: dict) -> str:
    return (
        f"<b>{html.escape(mod['title'])}</b>\n"
        f"<i>{html.escape(mod.get('tagline',''))}</i>\n\n"
        f"{html.escape(mod.get('summary',''))[:600]}\n\n"
        f"<b>{len(mod.get('lessons', []))} ders</b> — birini seç ↓"
    )


def _render_acad_lesson(payload: dict) -> str:
    """Ders gövdesini Telegram için kısalt — dashboard'da tam içerik var."""
    les = payload["lesson"]
    if les.get("locked"):
        return (
            f"<b>{html.escape(les['title'])}</b>\n"
            f"🔒 Bu ders <b>Premium</b> erişim gerektiriyor.\n\n"
            f"<i>{html.escape(les.get('learning_objective',''))}</i>\n\n"
            f"Tüm modüller için: /upgrade"
        )
    body = les.get("body", "")
    # Telegram limit 4096; tam gövde + worked example ozeti + glossary = ~2.5K hedef
    body_trim = body if len(body) <= 1800 else (body[:1800].rsplit("\n", 1)[0] + "\n…")
    out = [
        f"<b>{html.escape(les['title'])}</b>",
        f"<i>{html.escape(les.get('learning_objective',''))}</i>",
        "",
        html.escape(body_trim),
    ]
    ex = les.get("worked_examples") or []
    if ex:
        out.append("\n<b>Örnekler:</b>")
        for e in ex[:2]:
            sym = e.get("symbol", e.get("asset", ""))
            scen = (e.get("scenario") or "").strip()
            if scen:
                scen_short = scen if len(scen) <= 300 else (scen[:280].rsplit(" ", 1)[0] + "…")
                out.append(f"• <b>{html.escape(sym)}</b>: {html.escape(scen_short)}")
    if les.get("horology_note"):
        out.append(f"\n🕰️ <i>{html.escape(les['horology_note'])}</i>")
    if les.get("quiz"):
        out.append("\n📝 <i>Quiz dashboard'da</i> — cevapla ilerlemen kaydedilir.")
    out.append(f'\n<a href="{_acad_dashboard_link()}">Dashboard\'da tam ders + quiz →</a>')
    return "\n".join(out)


async def process_akademi_command(chat_id, user_id):
    """`/akademi` — Opsiyon Akademisi ana menüsü."""
    tier = await _get_user_tier(user_id)
    text = _render_acad_welcome(tier)
    keyboard = _acad_module_menu_keyboard(tier)
    send_message_with_keyboard(chat_id, text, keyboard)


async def process_opsiyon_command(chat_id, user_id, arg: str):
    """`/opsiyon <terim>` — sözlük araması. Arg boşsa kullanım ipucu."""
    from services.academy_service import search_glossary
    arg = (arg or "").strip()
    if not arg:
        send_telegram_message(
            chat_id,
            "📖 <b>Opsiyon Sözlüğü</b>\n\n"
            "Kullanım: <code>/opsiyon &lt;terim&gt;</code>\n"
            "Örnek: <code>/opsiyon theta</code>, <code>/opsiyon gamma flip</code>\n\n"
            "Sözlük 45+ terim içerir (Greeks, stratejiler, kurumsal akış)."
        )
        return
    hits = search_glossary(arg, limit=3)
    if not hits:
        send_telegram_message(
            chat_id,
            f"🔍 <b>'{html.escape(arg)}'</b> için sözlükte sonuç yok.\n\n"
            "Tam sözlük dashboard'da: " + _acad_dashboard_link()
        )
        return
    blocks = [f"📖 <b>'{html.escape(arg)}'</b> — {len(hits)} sonuç:\n"]
    for h in hits:
        blocks.append(
            f"<b>{html.escape(h.get('tr', h['slug']))}</b>\n"
            f"<i>{html.escape(h.get('intuition',''))}</i>\n"
            f"{html.escape(h.get('one_liner','') or h.get('metaphor','') or '')}\n"
        )
    blocks.append(f'<a href="{_acad_dashboard_link()}">Tam sözlük + dersler →</a>')
    send_telegram_message(chat_id, "\n".join(blocks))


async def process_acad_callback(callback_query_id, chat_id, message_id, user_id, data: str):
    """Inline callback dispatcher — acad_back / acad_mod:M1 / acad_les:M1L1 / acad_help_glossary / acad_upgrade."""
    answer_callback_query(callback_query_id)
    tier = await _get_user_tier(user_id)

    if data == "acad_back":
        text = _render_acad_welcome(tier)
        edit_message_text_with_keyboard(chat_id, message_id, text, _acad_module_menu_keyboard(tier))
        return

    if data == "acad_help_glossary":
        send_telegram_message(
            chat_id,
            "📖 <b>Sözlük araması</b>: <code>/opsiyon &lt;terim&gt;</code>\n"
            "Örnek: <code>/opsiyon theta</code>"
        )
        return

    if data == "acad_upgrade":
        await process_upgrade_command(chat_id, user_id)
        return

    if data.startswith("acad_mod:"):
        from services.academy_service import get_module
        mid = data[len("acad_mod:"):].strip()
        mod = get_module(mid, tier)
        if not mod:
            send_telegram_message(chat_id, "Modül bulunamadı.")
            return
        edit_message_text_with_keyboard(
            chat_id, message_id, _render_acad_module(mod), _acad_module_detail_keyboard(mid),
        )
        return

    if data.startswith("acad_les:"):
        from services.academy_service import get_lesson
        lid = data[len("acad_les:"):].strip()
        payload = get_lesson(lid, tier)
        if not payload:
            send_telegram_message(chat_id, "Ders bulunamadı.")
            return
        text = _render_acad_lesson(payload)
        edit_message_text_with_keyboard(
            chat_id, message_id, text, _acad_lesson_back_keyboard(payload["module_id"])
        )
        return


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
                                # `/start upgrade_premium` → payload routing
                                # (free broadcast'taki [💎 Anında al] butonu).
                                _payload = text[len("/start"):].strip()
                                await process_start_command(chat_id, user_id, username, _payload)
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
                            elif text.lower().startswith("/onchain"):
                                arg = text[len("/onchain"):].strip()
                                await process_onchain_command(chat_id, user_id, arg or "BTC")
                            elif text.lower().startswith("/login"):
                                await process_login_command(chat_id, user_id, username)
                            elif text.lower().startswith("/sentez"):
                                await process_sentez_command(chat_id, user_id)
                            elif text.lower().startswith("/akademi"):
                                await process_akademi_command(chat_id, user_id)
                            elif text.lower().startswith("/opsiyon"):
                                arg = text[len("/opsiyon"):].strip()
                                await process_opsiyon_command(chat_id, user_id, arg)

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
                                await handle_callback(cq_id, cq_chat_id, cq_data, user_id=cq_user_id)
                            elif cq_data.startswith("onchain_full:"):
                                sym = cq_data[len("onchain_full:"):].strip() or "BTC"
                                await process_onchain_full_callback(cq_id, cq_chat_id, cq_message_id, cq_user_id, sym)
                            elif cq_data.startswith("acad_"):
                                await process_acad_callback(cq_id, cq_chat_id, cq_message_id, cq_user_id, cq_data)

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
