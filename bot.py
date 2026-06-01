"""
Duel Duck — Telegram Duel Idea Generator Bot

Setup:
  1. pip install python-telegram-bot anthropic
  2. Create .env or export vars:
       TELEGRAM_BOT_TOKEN=your_telegram_bot_token
       ANTHROPIC_API_KEY=your_anthropic_api_key
  3. python bot.py

Usage:
  - Send a Twitter/X link or @handle to the bot
  - Bot analyzes recent tweets and returns "Will ..." duel ideas
  - Each idea has a "Create Duel" button linking to duelduck.com
"""

import os
import re
import json
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from anthropic import Anthropic

# ── Config ────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")  # optional if set globally

# Change this to your actual duel creation URL/route
DUEL_CREATE_URL = "https://duelduck.com/create-duel?title="

SYSTEM_PROMPT = """You are a Duel Duck duel idea generator. Duel Duck is a PvP predictions platform on Solana where users create yes/no prediction duels.

You will receive recent tweets/context about a Twitter account. Generate 4-5 creative duel ideas that start with "Will..." and relate to the account's recent activity.

Rules:
- Every duel MUST start with "Will"
- Duels must be yes/no answerable
- Keep them short (under 120 chars)
- Make them specific to the account's recent tweets, announcements, metrics, or claims
- Be creative — price predictions, milestones, shipping deadlines, community reactions
- No generic duels — every idea must clearly connect to something the account posted

Respond ONLY with a JSON array, no markdown, no backticks. Each object:
- "duel": the full duel question starting with "Will"
- "context": one short sentence explaining which tweet/topic inspired this (max 80 chars)

Example:
[{"duel":"Will @example hit 100k followers before July?","context":"They tweeted about rapid follower growth"}]"""

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else Anthropic()


# ── Helpers ───────────────────────────────────────────────────────

def extract_handle(text: str) -> str | None:
    """Extract Twitter handle from a link or @mention."""
    text = text.strip().rstrip("/")
    # URL pattern
    m = re.search(r"(?:twitter\.com|x\.com)/(@?[\w]+)", text, re.I)
    if m:
        return m.group(1).lstrip("@")
    # Plain @handle or handle
    m = re.match(r"^@?([\w]{1,15})$", text)
    if m:
        return m.group(1)
    return None


def encode_duel_url(duel_text: str) -> str:
    from urllib.parse import quote
    return DUEL_CREATE_URL + quote(duel_text, safe="")


# ── Bot handlers ──────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🦆 *Duel Duck Idea Generator*\n\n"
        "Send me a Twitter/X link or @handle — "
        "I'll analyze recent tweets and suggest duel ideas.\n\n"
        "Example: `@solana` or `https://x.com/solana`",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    handle = extract_handle(text)

    if not handle:
        await update.message.reply_text(
            "Send me a Twitter/X link or @handle to get started."
        )
        return

    status = await update.message.reply_text(
        f"🔍 Scanning @{handle}'s tweets..."
    )

    try:
        # Single API call: search tweets + generate duels
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Search for the latest tweets from @{handle} on Twitter/X. "
                        f"Then generate 4-5 duel ideas based on what you find."
                    ),
                }
            ],
        )

        await status.edit_text(f"🎯 Generating duels for @{handle}...")

        raw = "".join(
            b.text for b in resp.content if b.type == "text"
        )
        logger.info(f"Raw response: {raw[:500]}")
        cleaned = re.sub(r"```json|```", "", raw).strip()
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if not match:
            await status.edit_text(f"Couldn't parse response. Raw:\n{raw[:300]}")
            return
        ideas = json.loads(match.group())

        if not ideas:
            await status.edit_text("No ideas generated. Try a different account.")
            return

        # Send each idea as a message with a button
        await status.edit_text(
            f"🦆 *Duel ideas for @{handle}:*",
            parse_mode="Markdown",
        )

        for idea in ideas:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🎯 Create Duel",
                            url=encode_duel_url(idea["duel"]),
                        )
                    ]
                ]
            )
            await update.message.reply_text(
                f"*{idea['duel']}*\n_{idea.get('context', '')}_",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

    except json.JSONDecodeError:
        logger.exception("Failed to parse ideas JSON")
        await status.edit_text("Failed to parse duel ideas. Try again.")
    except Exception as e:
        logger.exception("Error generating duels")
        await status.edit_text(f"Something went wrong: {e}")


# ── Main ──────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
