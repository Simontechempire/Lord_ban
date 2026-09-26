"""simon — ᴀᴜᴛʜᴏʀɪᴢᴇᴅ sᴜᴘᴘᴏʀᴛ ʀᴇᴘᴏʀᴛɪɴɢ ʙᴏᴛ.

Premium-styled Telegram UI with an owner-configured Gmail OAuth sender.
The bot only accepts authorized users and requires explicit confirmation
before sending a support report.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

TYPE, DESCRIPTION, REFERENCE, EVIDENCE, PREVIEW = range(5)
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
LOGGER = logging.getLogger("bahirava")

BOT_NAME = os.getenv("BOT_NAME", "ʙᴀʜɪʀᴀᴠᴀ")
BOT_TAGLINE = os.getenv("BOT_TAGLINE", "ᴀᴜᴛʜᴏʀɪᴢᴇᴅ sᴜᴘᴘᴏʀᴛ ʀᴇᴘᴏʀᴛɪɴɢ")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "@DG_BAHIRAVA")

MAX_FILES = max(1, int(os.getenv("MAX_EVIDENCE_FILES", "5")))
MAX_BYTES = max(1, int(os.getenv("MAX_EVIDENCE_MB", "10"))) * 1024 * 1024
COOLDOWN_SECONDS = max(0, int(os.getenv("MIN_SECONDS_BETWEEN_REPORTS", "60")))
REFERRAL_LEVELS = min(6, max(1, int(os.getenv("REFERRAL_LEVELS", "6"))))
STORAGE = Path(os.getenv("REPORT_STORAGE_DIR", "report_storage"))
REFERRAL_FILE = Path(os.getenv("REFERRAL_DATA_FILE", "data/referrals.json"))
LAST_SENT: dict[int, float] = {}


@dataclass
class Draft:
    case_type: str = ""
    description: str = ""
    reference: str = ""
    evidence: list[Path] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def authorized_ids() -> set[int]:
    return {
        int(v.strip())
        for v in os.getenv("AUTHORIZED_USER_IDS", "").split(",")
        if re.fullmatch(r"-?\d+", v.strip())
    }


def is_owner(user_id: int) -> bool:
    raw = os.getenv("OWNER_ID", "").strip()
    return bool(raw and str(user_id) == raw)


def allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in authorized_ids())


def box(title: str, body: str) -> str:
    return f"╔═━〔 {title} 〕━═╗\n\n{body}\n\n╚═━━━━━━━━━━━━━━═╝"


async def unauthorized(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            box(
                "⛔ ᴀᴄᴄᴇss ᴅᴇɴɪᴇᴅ",
                f"🔐 ᴛʜɪs ʙᴏᴛ ɪs ʀᴇsᴛʀɪᴄᴛᴇᴅ ᴛᴏ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀs.\n\n"
                f"👑 ᴏᴡɴᴇʀ : {OWNER_USERNAME}",
            )
        )


def main_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⟬ 📝 ʀᴇᴘᴏʀᴛ ⟭", callback_data="menu:report"),
            InlineKeyboardButton("⟬ 🔗 ʀᴇғᴇʀʀᴀʟ ⟭", callback_data="menu:referral"),
        ],
        [
            InlineKeyboardButton("⟬ 🆔 ᴍʏ ɪᴅ ⟭", callback_data="menu:id"),
            InlineKeyboardButton("⟬ ℹ️ ʜᴇʟᴘ ⟭", callback_data="menu:help"),
        ],
        [
            InlineKeyboardButton("⟬ 👑 ᴏᴡɴᴇʀ ⟭", url="https://t.me/DG_BAHIRAVA"),
        ],
    ])


def cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data="cancel")]
    ])


def type_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 ᴘᴇʀᴍᴀɴᴇɴᴛ", callback_data="type:permanent"),
            InlineKeyboardButton("🟡 ᴛᴇᴍᴘᴏʀᴀʀʏ", callback_data="type:temporary"),
        ],
        [InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data="cancel")],
    ])


def preview_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ ᴄᴏɴғɪʀᴍ", callback_data="confirm"),
            InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data="cancel"),
        ]
    ])


def cooldown_remaining(user_id: int) -> int:
    return max(0, int(COOLDOWN_SECONDS - (time.time() - LAST_SENT.get(user_id, 0))))


def home_text(user_id: int) -> str:
    role = "ᴏᴡɴᴇʀ" if is_owner(user_id) else "ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ"
    return box(
        f"🚀 {BOT_NAME.upper()}",
        f"✨ {BOT_TAGLINE}\n\n"
        f"👤 ʀᴏʟᴇ : {role}\n"
        f"👑 ᴏᴡɴᴇʀ : {OWNER_USERNAME}\n\n"
        "📝 /report — sᴜʙᴍɪᴛ ᴀ sᴜᴘᴘᴏʀᴛ ᴄᴀsᴇ\n"
        "🔗 /referral — ᴠɪᴇᴡ ʀᴇғᴇʀʀᴀʟ ᴘʀᴏғɪʟᴇ\n"
        "🆔 /id — ᴠɪᴇᴡ ʏᴏᴜʀ ᴛᴇʟᴇɢʀᴀᴍ ɪᴅ\n"
        "ℹ️ /help — ᴠɪᴇᴡ ᴄᴏᴍᴍᴀɴᴅs\n\n"
        "🔐 ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇ ᴏɴʟʏ.",
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await unauthorized(update)
        return
    parent_code = context.args[0] if context.args else None
    profile = await referral_profile(update.effective_user.id, parent_code)
    await update.effective_message.reply_text(
        home_text(update.effective_user.id)
        + f"\n\n🔗 ʏᴏᴜʀ ʀᴇғᴇʀʀᴀʟ ᴄᴏᴅᴇ : `{profile['code']}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_markup(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await unauthorized(update)
        return
    await update.effective_message.reply_text(
        box(
            f"ℹ️ {BOT_NAME.upper()} ʜᴇʟᴘ",
            "📝 /report — ᴄʀᴇᴀᴛᴇ ᴀ sᴜᴘᴘᴏʀᴛ ᴄᴀsᴇ\n"
            "🔗 /referral — ʀᴇғᴇʀʀᴀʟ ᴘʀᴏғɪʟᴇ\n"
            "🆔 /id — ᴛᴇʟᴇɢʀᴀᴍ ᴜsᴇʀ ɪᴅ\n"
            "📊 /status — ʙᴏᴛ sᴛᴀᴛᴜs\n"
            "❌ /cancel — ᴄᴀɴᴄᴇʟ ᴄᴜʀʀᴇɴᴛ ᴄᴀsᴇ\n\n"
            f"👑 ᴏᴡɴᴇʀ : {OWNER_USERNAME}",
        )
    )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await unauthorized(update)
        return
    user = update.effective_user
    await update.effective_message.reply_text(
        box("🆔 ʏᴏᴜʀ ɪᴅ", f"👤 ɴᴀᴍᴇ : {user.first_name or '—'}\n"
        f"🆔 ᴜsᴇʀ ɪᴅ : `{user.id}`\n"
        f"🔗 ᴜsᴇʀɴᴀᴍᴇ : @{user.username}" if user.username else
        f"👤 ɴᴀᴍᴇ : {user.first_name or '—'}\n🆔 ᴜsᴇʀ ɪᴅ : `{user.id}`"),
        parse_mode=ParseMode.MARKDOWN,
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await unauthorized(update)
        return
    await update.effective_message.reply_text(
        box(
            "📊 sᴛᴀᴛᴜs",
            "🟢 ʙᴏᴛ : ᴏɴʟɪɴᴇ\n"
            "🟢 ɢᴍᴀɪʟ : ᴏᴀᴜᴛʜ ᴄᴏɴғɪɢᴜʀᴀᴛɪᴏɴ ʀᴇᴀᴅʏ\n"
            f"📎 ᴍᴀx ᴇᴠɪᴅᴇɴᴄᴇ : {MAX_FILES}\n"
            f"⏳ ᴄᴏᴏʟᴅᴏᴡɴ : {COOLDOWN_SECONDS}s\n"
            f"👑 ᴏᴡɴᴇʀ : {OWNER_USERNAME}",
        )
    )


async def begin_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not allowed(update):
        await unauthorized(update)
        return ConversationHandler.END
    remaining = cooldown_remaining(update.effective_user.id)
    if remaining:
        await update.effective_message.reply_text(
            box("⏳ ᴄᴏᴏʟᴅᴏᴡɴ", f"ᴘʟᴇᴀsᴇ ᴡᴀɪᴛ {remaining} sᴇᴄᴏɴᴅs.")
        )
        return ConversationHandler.END
    context.user_data["draft"] = Draft()
    await update.effective_message.reply_text(
        box(
            "🧾 ᴄᴀsᴇ ᴛʏᴘᴇ",
            "ᴄʜᴏᴏsᴇ ᴛʜᴇ ᴛʏᴘᴇ ᴏғ ʏᴏᴜʀ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ sᴜᴘᴘᴏʀᴛ ᴄᴀsᴇ.",
        ),
        reply_markup=type_markup(),
    )
    return TYPE


async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ ʀᴇᴘᴏʀᴛ ᴄᴀɴᴄᴇʟʟᴇᴅ.")
        return ConversationHandler.END
    draft: Draft = context.user_data["draft"]
    draft.case_type = query.data.split(":", 1)[1]
    context.user_data["state"] = "description"
    await query.edit_message_text(
        box(
            "📝 ᴅᴇsᴄʀɪᴘᴛɪᴏɴ",
            "ᴇɴᴛᴇʀ ᴀ ᴄʟᴇᴀʀ ᴅᴇsᴄʀɪᴘᴛɪᴏɴ ᴏғ ᴛʜᴇ ɪssᴜᴇ.\n\n"
            "ɪɴᴄʟᴜᴅᴇ ᴡʜᴀᴛ ʜᴀᴘᴘᴇɴᴇᴅ, ᴡʜᴇɴ, ᴀɴᴅ ᴡʜᴀᴛ sᴜᴘᴘᴏʀᴛ ʏᴏᴜ ɴᴇᴇᴅ.",
        ),
        reply_markup=cancel_markup(),
    )
    return DESCRIPTION


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not allowed(update):
        await unauthorized(update)
        return ConversationHandler.END
    draft: Draft = context.user_data["draft"]
    text = (update.effective_message.text or "").strip()

    if context.user_data.get("state") == "evidence" and text == "/done":
        names = "\n".join(f"• {p.name}" for p in draft.evidence) or "• ɴᴏɴᴇ"
        context.user_data["state"] = "preview"
        await update.effective_message.reply_text(
            box(
                "🔎 ᴘʀᴇᴠɪᴇᴡ",
                f"🧾 ᴛʏᴘᴇ : {draft.case_type}\n"
                f"📱 ʀᴇғᴇʀᴇɴᴄᴇ : {draft.reference}\n"
                f"📎 ᴇᴠɪᴅᴇɴᴄᴇ :\n{names}\n\n"
                f"📝 ᴅᴇsᴄʀɪᴘᴛɪᴏɴ :\n{draft.description}",
            ),
            reply_markup=preview_markup(),
        )
        return PREVIEW

    if context.user_data.get("state") == "description":
        if len(text) < 10:
            await update.effective_message.reply_text(
                "⚠️ ᴘʟᴇᴀsᴇ ᴇɴᴛᴇʀ ᴀ ʟɪᴛᴛʟᴇ ᴍᴏʀᴇ ᴅᴇᴛᴀɪʟ.",
                reply_markup=cancel_markup(),
            )
            return DESCRIPTION
        draft.description = text
        context.user_data["state"] = "reference"
        await update.effective_message.reply_text(
            box(
                "📱 ʀᴇғᴇʀᴇɴᴄᴇ",
                "ᴇɴᴛᴇʀ ʏᴏᴜʀ ᴏᴡɴ ᴀᴄᴄᴏᴜɴᴛ ʀᴇғᴇʀᴇɴᴄᴇ ᴏʀ ᴄᴀsᴇ ɪᴅ.\n\n"
                "ᴘʜᴏɴᴇ ɴᴜᴍʙᴇʀs ᴜsᴇ ɪɴᴛᴇʀɴᴀᴛɪᴏɴᴀʟ ғᴏʀᴍᴀᴛ.",
            ),
            reply_markup=cancel_markup(),
        )
        return REFERENCE
    return EVIDENCE


async def receive_evidence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not allowed(update):
        await unauthorized(update)
        return ConversationHandler.END
    draft: Draft = context.user_data["draft"]
    if len(draft.evidence) >= MAX_FILES:
        await update.effective_message.reply_text(
            f"⚠️ ᴍᴀxɪᴍᴜᴍ {MAX_FILES} ᴇᴠɪᴅᴇɴᴄᴇ ғɪʟᴇs ᴀʟʟᴏᴡᴇᴅ."
        )
        return EVIDENCE

    message = update.effective_message
    if message.document:
        if (message.document.file_size or 0) > MAX_BYTES:
            await message.reply_text("⚠️ ᴛʜᴀᴛ ғɪʟᴇ ɪs ᴛᴏᴏ ʟᴀʀɢᴇ.")
            return EVIDENCE
        telegram_file = await message.document.get_file()
        name = Path(message.document.file_name or "evidence.bin").name
        mime = message.document.mime_type or "application/octet-stream"
    elif message.photo:
        telegram_file = await message.photo[-1].get_file()
        name = f"evidence_{len(draft.evidence) + 1}.jpg"
        mime = "image/jpeg"
    else:
        await message.reply_text("📎 sᴇɴᴅ ᴀ ᴘʜᴏᴛᴏ ᴏʀ ᴅᴏᴄᴜᴍᴇɴᴛ, ᴏʀ /done.")
        return EVIDENCE

    user_dir = STORAGE / str(update.effective_user.id) / uuid.uuid4().hex
    user_dir.mkdir(parents=True, exist_ok=True)
    target = user_dir / re.sub(r"[^A-Za-z0-9._-]", "_", name)[:100]
    await telegram_file.download_to_drive(custom_path=str(target))
    if target.stat().st_size > MAX_BYTES:
        target.unlink(missing_ok=True)
        await message.reply_text("⚠️ ᴛʜᴀᴛ ғɪʟᴇ ɪs ᴛᴏᴏ ʟᴀʀɢᴇ.")
        return EVIDENCE
    draft.evidence.append(target)
    await message.reply_text(
        f"✅ ᴇᴠɪᴅᴇɴᴄᴇ ᴀᴅᴅᴇᴅ — {len(draft.evidence)}/{MAX_FILES}\n"
        "ᴛʏᴘᴇ /done ᴡʜᴇɴ ʏᴏᴜ ᴀʀᴇ ғɪɴɪsʜᴇᴅ."
    )
    return EVIDENCE


def gmail_service():
    credentials = Credentials(
        token=None,
        refresh_token=required("GMAIL_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required("GOOGLE_CLIENT_ID"),
        client_secret=required("GOOGLE_CLIENT_SECRET"),
        scopes=[GMAIL_SCOPE],
    )
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def send_email(draft: Draft, user_id: int) -> str:
    message = EmailMessage()
    message["From"] = required("GMAIL_SENDER_EMAIL")
    message["To"] = required("REPORT_RECIPIENT_EMAIL")
    message["Subject"] = f"ʙᴀʜɪʀᴀᴠᴀ — authorized support case ({draft.case_type})"
    message.set_content(
        "ʙᴀʜɪʀᴀᴠᴀ authorized support case\n\n"
        f"Case type: {draft.case_type}\n"
        f"Reference: {draft.reference}\n"
        f"Telegram user ID: {user_id}\n"
        f"Created at: {draft.created_at}\n\n"
        f"Description:\n{draft.description}\n"
    )
    for path in draft.evidence:
        mime, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (mime or "application/octet-stream").split("/", 1)
        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
    result = (
        gmail_service().users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
    )
    return str(result.get("id", "sent"))


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    draft: Draft | None = context.user_data.get("draft")
    if not draft:
        await query.edit_message_text("❌ sᴇssɪᴏɴ ᴇxᴘɪʀᴇᴅ.")
        return ConversationHandler.END

    await query.edit_message_text("⏳ ᴠᴇʀɪғʏɪɴɢ ᴏᴀᴜᴛʜ ᴀɴᴅ sᴇɴᴅɪɴɢ...")
    try:
        message_id = await asyncio.to_thread(
            send_email, draft, update.effective_user.id
        )
        LAST_SENT[update.effective_user.id] = time.time()
        await query.edit_message_text(
            box(
                "✅ sᴇɴᴛ",
                f"ʀᴇᴘᴏʀᴛ sᴇɴᴛ sᴜᴄᴄᴇssғᴜʟʟʏ.\n\n"
                f"🆔 ᴍᴇssᴀɢᴇ ɪᴅ : {message_id}\n"
                f"👑 ᴏᴡɴᴇʀ : {OWNER_USERNAME}",
            )
        )
    except Exception:
        LOGGER.exception("Bahirava report send failed")
        await query.edit_message_text(
            box(
                "❌ sᴇɴᴅ ғᴀɪʟᴇᴅ",
                "ᴛʜᴇ ʀᴇᴘᴏʀᴛ ᴄᴏᴜʟᴅ ɴᴏᴛ ʙᴇ sᴇɴᴛ.\n"
                "ᴄʜᴇᴄᴋ ᴛʜᴇ ᴏᴡɴᴇʀ ɢᴍᴀɪʟ ᴏᴀᴜᴛʜ sᴇᴛᴛɪɴɢs.",
            )
        )
    finally:
        context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
    await update.effective_message.reply_text("❌ ʀᴇᴘᴏʀᴛ ᴄᴀɴᴄᴇʟʟᴇᴅ.")
    return ConversationHandler.END


async def read_referrals() -> dict:
    try:
        return json.loads(REFERRAL_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"users": {}, "codes": {}}


async def referral_profile(user_id: int, parent_code: str | None = None) -> dict:
    data = await read_referrals()
    key = str(user_id)
    if key not in data["users"]:
        parent = data["codes"].get(parent_code) if parent_code else None
        chain = data["users"].get(str(parent), {}).get("chain", []) if parent else []
        profile = {
            "code": f"BH{base64.urlsafe_b64encode(str(user_id).encode()).decode()[:10]}",
            "parent": parent,
            "chain": ([parent] + chain)[:REFERRAL_LEVELS] if parent else [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        data["users"][key] = profile
        data["codes"][profile["code"]] = user_id
        REFERRAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        REFERRAL_FILE.write_text(json.dumps(data, indent=2))
    return data["users"][key]


async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await unauthorized(update)
        return
    profile = await referral_profile(update.effective_user.id)
    await update.effective_message.reply_text(
        box(
            "🔗 ʀᴇғᴇʀʀᴀʟ",
            f"ᴄᴏᴅᴇ : `{profile['code']}`\n"
            f"ᴛʀᴀᴄᴋᴇᴅ ʟᴇᴠᴇʟs : {REFERRAL_LEVELS}\n"
            f"ᴅᴏᴡɴʟɪɴᴋ : {len(profile['chain'])}\n\n"
            "ᴜsᴇ ʏᴏᴜʀ /start ʟɪɴᴋ ᴛᴏ ᴛʀᴀᴄᴋ ɪɴᴠɪᴛᴇ ᴀᴛᴛʀɪʙᴜᴛɪᴏɴ.",
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not allowed(update):
        await query.edit_message_text("⛔ ᴀᴄᴄᴇss ᴅᴇɴɪᴇᴅ.")
        return

    action = query.data.split(":", 1)[1]
    if action == "report":
        await query.message.reply_text("📝 ᴜsᴇ /report ᴛᴏ sᴛᴀʀᴛ ᴀ ɴᴇᴡ ᴄᴀsᴇ.")
    elif action == "referral":
        await referral_command(update, context)
    elif action == "id":
        await id_command(update, context)
    elif action == "help":
        await help_command(update, context)


async def main() -> None:
    load_dotenv()
    if not authorized_ids():
        raise RuntimeError("AUTHORIZED_USER_IDS must contain at least one Telegram ID")

    application = Application.builder().token(required("TELEGRAM_BOT_TOKEN")).build()

    flow = ConversationHandler(
        entry_points=[CommandHandler("report", begin_report)],
        states={
            TYPE: [CallbackQueryHandler(choose_type, pattern=r"^(type:|cancel)")],
            DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text)
            ],
            REFERENCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text)
            ],
            EVIDENCE: [
                CommandHandler("done", receive_text),
                MessageHandler(filters.PHOTO | filters.Document.ALL, receive_evidence),
            ],
            PREVIEW: [
                CallbackQueryHandler(confirm, pattern=r"^confirm$"),
                CallbackQueryHandler(cancel, pattern=r"^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("referral", referral_command))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    application.add_handler(flow)

    LOGGER.info("%s is starting...", BOT_NAME)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())
