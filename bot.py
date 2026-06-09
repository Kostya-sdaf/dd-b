"""
Duel Duck — Telegram Duel Idea Generator Bot (v3 — Full API Integration)

Flow:
  /duel → Twitter handle → Sorsa fetches tweets (last 2 days)
  → Claude Haiku generates duels → creates duels via DuelDuck API
  → returns links to live duels

Hardcoded: 2 USDC ticket, +10h UTC deadline, 5% commission, platform resolves
"""

import os
import re
import json
import io
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import requests as http_requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from anthropic import Anthropic

# ── State ─────────────────────────────────────────────────────────
ASK_HANDLE = 0

# ── Config ────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
SORSA_API_KEY = os.environ["SORSA_API_KEY"]
DUELDUCK_REFRESH_TOKEN = os.environ["DUELDUCK_REFRESH_TOKEN"]

SORSA_BASE = "https://api.sorsa.io/v3"
DD_API_BASE = "https://xapi.duelduck.com"
DD_SITE_BASE = "https://duelduck.com"

# ── Duel defaults ─────────────────────────────────────────────────
DUEL_SYMBOL = "USDC"
DUEL_PRICE = 2
DUEL_COMMISSION = 5
DUEL_DEADLINE_HOURS = 10

SYSTEM_PROMPT = """You are a Duel Duck duel idea generator. Duel Duck is a PvP predictions platform on Solana where users create yes/no prediction duels.

You will receive the latest tweets (last 2 days) from a Twitter account with full text and engagement metrics. Generate 4-5 creative duel ideas that start with "Will..." and relate directly to those tweets.

Rules:
- Every duel MUST start with "Will"
- Duels must be yes/no answerable
- Keep them short (under 120 chars)
- Make them specific to the actual tweet content — reference real numbers, claims, announcements, or events
- Be creative — price predictions, milestones, shipping deadlines, community reactions
- No generic duels — every idea must clearly connect to something from the tweets

CRITICAL: Respond ONLY with a JSON array. No markdown, no backticks, no extra text.
Each object: {"duel": "Will ...", "context": "short explanation (max 80 chars)"}

Example:
[{"duel":"Will @example hit 100k followers before July?","context":"They tweeted about rapid follower growth"}]"""

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else Anthropic()

# ── DuelDuck Auth ─────────────────────────────────────────────────

_tokens = {
    "access_token": None,
    "refresh_token": DUELDUCK_REFRESH_TOKEN,
}


def dd_get_access_token() -> str | None:
    """Refresh JWT tokens and return a valid access_token."""
    try:
        resp = http_requests.post(
            f"{DD_API_BASE}/auth/refresh",
            headers={"Authorization": f"Bearer {_tokens['refresh_token']}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        jwt = data.get("jwt_info", {})
        _tokens["access_token"] = jwt.get("access_token")
        _tokens["refresh_token"] = jwt.get("refresh_token", _tokens["refresh_token"])
        logger.info("DuelDuck tokens refreshed")
        return _tokens["access_token"]
    except Exception:
        logger.exception("Failed to refresh DuelDuck token")
        return None


def dd_auth_headers() -> dict:
    """Get Authorization headers, refreshing token if needed."""
    token = _tokens["access_token"] or dd_get_access_token()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── DuelDuck API ──────────────────────────────────────────────────

def dd_upload_avatar(image_url: str) -> str | None:
    """Download avatar from Twitter and upload to DuelDuck. Returns logo_url."""
    try:
        # Download image
        img_resp = http_requests.get(image_url, timeout=10)
        img_resp.raise_for_status()

        # Upload to DuelDuck
        token = _tokens["access_token"] or dd_get_access_token()
        resp = http_requests.get(
            f"{DD_API_BASE}/admin/duel/upload-image",
            headers={"Authorization": f"Bearer {token}"},
            files={"duel_logo": ("avatar.jpg", io.BytesIO(img_resp.content), "image/jpeg")},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # Response is a dict with the URL somewhere
        logo_url = data.get("duel_logo_url") or data.get("url") or ""
        if not logo_url:
            # Try to find any string value that looks like a URL
            for v in data.values():
                if isinstance(v, str) and v.startswith("http"):
                    logo_url = v
                    break
        logger.info(f"Avatar uploaded: {logo_url}")
        return logo_url
    except Exception:
        logger.exception("Failed to upload avatar")
        return None


def dd_create_duel(question: str, logo_url: str = "") -> dict | None:
    """Create a duel via POST /admin/duel. Returns duel data or None."""
    deadline = datetime.now(timezone.utc) + timedelta(hours=DUEL_DEADLINE_HOURS)
    deadline_iso = deadline.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    body = {
        "question": question,
        "symbol": DUEL_SYMBOL,
        "duel_price": DUEL_PRICE,
        "commission_rate": DUEL_COMMISSION,
        "deadline": deadline_iso,
        "is_owner_resolving": False,
        "answer": 0,
        "source_of_truth": "",
        "duel_info": {},
    }
    if logo_url:
        body["logo_url"] = logo_url

    headers = dd_auth_headers()

    try:
        resp = http_requests.post(
            f"{DD_API_BASE}/admin/duel",
            headers=headers,
            json=body,
            timeout=15,
        )

        # If 401, try refreshing token once
        if resp.status_code == 401:
            dd_get_access_token()
            headers = dd_auth_headers()
            resp = http_requests.post(
                f"{DD_API_BASE}/admin/duel",
                headers=headers,
                json=body,
                timeout=15,
            )

        resp.raise_for_status()
        duel = resp.json()
        logger.info(f"Duel created: {duel.get('id')} — {question}")
        return duel

    except Exception:
        logger.exception(f"Failed to create duel: {question}")
        return None


# ── Sorsa API ─────────────────────────────────────────────────────

def fetch_recent_tweets(username: str, max_tweets: int = 20) -> tuple[list[dict] | None, str]:
    """Fetch tweets (last 2 days) + avatar from Sorsa. Returns (tweets, avatar_url)."""
    try:
        resp = http_requests.post(
            f"{SORSA_BASE}/user-tweets",
            headers={"ApiKey": SORSA_API_KEY},
            json={"username": username, "limit": max_tweets},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        tweets_raw = data.get("tweets", [])
        if not tweets_raw:
            return None, ""

        avatar = tweets_raw[0].get("user", {}).get("profile_image_url", "")
        if avatar:
            avatar = avatar.replace("_normal.", "_400x400.")

        cutoff = datetime.now(timezone.utc) - timedelta(days=2)
        result = []

        for t in tweets_raw:
            date_str = t.get("created_at", "")
            tweet_dt = None
            for fmt in (
                "%a %b %d %H:%M:%S %z %Y",
                "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%SZ",
            ):
                try:
                    tweet_dt = datetime.strptime(date_str, fmt)
                    if tweet_dt.tzinfo is None:
                        tweet_dt = tweet_dt.replace(tzinfo=timezone.utc)
                    break
                except (ValueError, TypeError):
                    continue

            if tweet_dt and tweet_dt < cutoff:
                continue

            result.append({
                "text": t.get("full_text") or t.get("text", ""),
                "likes": t.get("likes_count", 0),
                "retweets": t.get("retweet_count", 0),
                "replies": t.get("reply_count", 0),
                "views": t.get("view_count", 0),
                "date": date_str,
            })

        return (result if result else None), avatar

    except Exception:
        logger.exception(f"Sorsa /user-tweets error for @{username}")
        return None, ""


# ── Helpers ───────────────────────────────────────────────────────

def extract_handle(text: str) -> str | None:
    text = text.strip().rstrip("/")
    m = re.search(r"(?:twitter\.com|x\.com)/(@?[\w]+)", text, re.I)
    if m:
        return m.group(1).lstrip("@")
    m = re.match(r"^@?([\w]{1,15})$", text)
    if m:
        return m.group(1)
    return None


def format_tweets_for_prompt(username: str, tweets: list[dict]) -> str:
    lines = [f"Latest tweets from @{username} (last 2 days):\n"]
    for i, t in enumerate(tweets, 1):
        lines.append(f"Tweet {i} ({t['date']}):")
        lines.append(f"  \"{t['text']}\"")
        lines.append(
            f"  {t['likes']} likes, {t['retweets']} RTs, "
            f"{t['replies']} replies, {t['views']} views"
        )
        lines.append("")
    return "\n".join(lines)


# ── Bot handlers ──────────────────────────────────────────────────

async def cmd_duel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🦆 *Duel Duck Idea Generator*\n\n"
        "📎 Надішли Twitter/X хендл або посилання:\n"
        "Приклад: `@solana` або `https://x.com/solana`",
        parse_mode="Markdown",
    )
    return ASK_HANDLE


async def received_handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    handle = extract_handle(update.message.text or "")
    if not handle:
        await update.message.reply_text(
            "❌ Не розпізнав хендл. Спробуй ще: @handle або посилання."
        )
        return ASK_HANDLE

    status = await update.message.reply_text(
        f"🔍 Завантажую твіти @{handle} за останні 2 дні..."
    )

    # ── Fetch tweets + avatar ─────────────────────────────────────
    tweets, avatar_url = fetch_recent_tweets(handle)
    if not tweets:
        await status.edit_text(
            f"❌ Не знайшов свіжих твітів (за 2 дні) для @{handle}.\n"
            "Спробуй інший акаунт або /duel заново."
        )
        return ConversationHandler.END

    await status.edit_text(
        f"🎯 {len(tweets)} твітів знайдено. Генерую дуелі..."
    )

    # ── Upload avatar to DuelDuck ─────────────────────────────────
    logo_url = ""
    if avatar_url:
        logo_url = dd_upload_avatar(avatar_url) or ""

    # ── Generate duels via Claude Haiku ───────────────────────────
    tweet_context = format_tweets_for_prompt(handle, tweets)

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    f"{tweet_context}\n\n"
                    f"Generate 4-5 duel ideas based on these tweets."
                ),
            }],
        )

        raw = "".join(b.text for b in resp.content if b.type == "text")
        logger.info(f"Raw: {raw[:500]}")

        cleaned = re.sub(r"```json|```", "", raw).strip()
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if not match:
            await status.edit_text("❌ Не вдалося розпарсити відповідь.")
            return ConversationHandler.END

        ideas = json.loads(match.group())
        if not ideas:
            await status.edit_text("❌ Ідей не згенеровано. /duel")
            return ConversationHandler.END

        # ── Create duels via API ──────────────────────────────────
        await status.edit_text(
            f"🚀 Створюю {len(ideas)} дуелей на DuelDuck..."
        )

        created_count = 0
        for idea in ideas:
            duel = dd_create_duel(idea["duel"], logo_url)
            if duel:
                created_count += 1
                duel_id = duel.get("id", "")
                slug = duel.get("slug", "")
                duel_link = f"{DD_SITE_BASE}/duel/{slug or duel_id}"

                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎯 Open Duel", url=duel_link)
                ]])
                await update.message.reply_text(
                    f"✅ *{idea['duel']}*\n_{idea.get('context', '')}_",
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
            else:
                await update.message.reply_text(
                    f"❌ Не вдалось створити: _{idea['duel']}_",
                    parse_mode="Markdown",
                )

        await update.message.reply_text(
            f"🦆 Створено {created_count}/{len(ideas)} дуелей! /duel для ще"
        )

    except json.JSONDecodeError:
        logger.exception("JSON parse error")
        await status.edit_text("❌ Помилка парсингу. /duel")
    except Exception as e:
        logger.exception("Error generating duels")
        await status.edit_text(f"❌ Помилка: {e}")

    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Скасовано. /duel")
    return ConversationHandler.END


# ── Main ──────────────────────────────────────────────────────────

def main():
    # Pre-authenticate on startup
    dd_get_access_token()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_duel),
            CommandHandler("duel", cmd_duel),
        ],
        states={
            ASK_HANDLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_handle)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    logger.info("Bot started (v3 — Full API, auto-create duels)")
    app.run_polling()


if __name__ == "__main__":
    main()
