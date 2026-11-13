#!/usr/bin/env python3
"""Stateless Telegram support relay bot (aiogram 3.x).

No database, filesystem persistence, external storage, or permanent message map.
The only in-memory state is a short-lived media-album collection buffer.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sys
import time
from typing import Any, Awaitable, Callable, TypeVar

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramRetryAfter,
)
from aiogram.types import BotCommand, Message, ReplyParameters, ReactionTypeEmoji
from dotenv import load_dotenv

# Load local .env files when present. Render injects Environment variables directly.
load_dotenv()


# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _parse_admin_ids(raw: str) -> set[int]:
    values: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            admin_id = int(item)
        except ValueError as exc:
            raise SystemExit(f"Invalid ADMIN_IDS entry: {item!r}") from exc
        if admin_id <= 0:
            raise SystemExit(f"ADMIN_IDS must contain positive Telegram user IDs: {item!r}")
        values.add(admin_id)
    if not values:
        raise SystemExit("ADMIN_IDS must contain at least one numeric Telegram user ID")
    return values


BOT_TOKEN = _required_env("BOT_TOKEN")
try:
    ADMIN_GROUP_ID = int(_required_env("ADMIN_GROUP_ID"))
except ValueError as exc:
    raise SystemExit("ADMIN_GROUP_ID must be a numeric group/supergroup ID") from exc

ADMIN_IDS = _parse_admin_ids(_required_env("ADMIN_IDS"))
BOT_NAME = os.getenv("BOT_NAME", "Support Bot").strip() or "Support Bot"
WELCOME_MESSAGE = os.getenv(
    "WELCOME_MESSAGE",
    "Welcome!\n\nSend your message here and our support team will get back to you.\n"
    "You can send text, photos, videos, documents, voice messages and other Telegram media.\n\n"
    "Use /help to see the available commands.",
)

# Telegram sends album items as separate updates. This short-lived buffer only
# batches those updates so Bot API forwardMessages/copyMessages can keep album
# grouping. It is NOT a user/message routing database.
ALBUM_DEBOUNCE_SECONDS = 1.0

BOT_STARTED_AT = time.monotonic()
BOT_USER_ID: int | None = None

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("stateless-support-bot")

router = Router(name="support-relay")

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------


async def telegram_call(operation: Callable[[], Awaitable[T]]) -> T:
    """Retry once after Telegram's explicit flood-wait instruction."""
    try:
        return await operation()
    except TelegramRetryAfter as exc:
        delay = max(1, int(exc.retry_after))
        log.warning("Telegram flood limit: retrying after %s seconds", delay)
        await asyncio.sleep(delay + 0.25)
        return await operation()


def _user_label(message: Message) -> tuple[str, str]:
    user = message.from_user
    if user is None:
        return "Unknown user", "—"
    name = html.escape(user.full_name or "Unknown user")
    username = f"@{html.escape(user.username)}" if user.username else "No username"
    return name, username


def service_event_description(message: Message) -> str:
    """Return a brief label for common service events Telegram will not forward."""
    service_fields = (
        ("new_chat_members", "new chat member event"),
        ("left_chat_member", "chat member left event"),
        ("new_chat_title", "chat title changed event"),
        ("new_chat_photo", "chat photo changed event"),
        ("delete_chat_photo", "chat photo removed event"),
        ("group_chat_created", "group created event"),
        ("supergroup_chat_created", "supergroup created event"),
        ("users_shared", "users shared with the bot event"),
        ("chat_shared", "chat shared with the bot event"),
        ("write_access_allowed", "write access granted event"),
        ("successful_payment", "successful payment event"),
        ("refunded_payment", "payment refunded event"),
        ("passport_data", "Telegram Passport service event"),
        ("proximity_alert_triggered", "proximity alert event"),
        ("boost_added", "chat boost event"),
        ("gift", "gift service event"),
        ("unique_gift", "unique gift service event"),
        ("forum_topic_created", "forum topic created event"),
        ("forum_topic_edited", "forum topic edited event"),
        ("forum_topic_closed", "forum topic closed event"),
        ("forum_topic_reopened", "forum topic reopened event"),
        ("pinned_message", "pinned message service event"),
    )
    for field_name, description in service_fields:
        if getattr(message, field_name, None):
            return f"Telegram service event: {description}."
    if message.has_protected_content:
        return "This message is protected; Telegram does not allow it to be forwarded or copied."
    return "Telegram did not allow this message type to be forwarded or copied."


def _metadata_notice_text(message: Message, *, route_only: bool, detail: str = "") -> str:
    user = message.from_user
    if user is None:
        # A private user message normally always has from_user, but keep this
        # defensive path in case Telegram adds another message form.
        return "⚠️ A message arrived without a usable sender account."

    name, username = _user_label(message)
    lines = [
        "📩 <b>New support request</b>",
        f"👤 <b>Name:</b> {name}",
        f"🔖 <b>Username:</b> {username}",
        f"🆔 <b>User ID:</b> <code>{user.id}</code>",
        f"🌐 <b>Language:</b> {html.escape(user.language_code or 'Unknown')}",
        f"⭐ <b>Premium:</b> {'Yes' if user.is_premium else 'No'}",
        f"🤖 <b>Bot account:</b> {'Yes' if user.is_bot else 'No'}",
        f"💬 <b>Chat ID:</b> <code>{message.chat.id}</code>",
        f"🕒 <b>Message ID:</b> <code>{message.message_id}</code>",
        f"<code>SUPPORT_USER_ID={user.id}</code>",
    ]

    if route_only:
        lines.append(
            "\n↩️ <b>Reply to THIS BOT NOTICE</b> to contact the correct user. "
            "The submitted content was already forwarded by the user, so its "
            "forward-origin may identify someone else."
        )
    else:
        lines.append(
            "\n↩️ Reply directly to the forwarded message, or reply to this notice."
        )

    if detail:
        lines.append(f"\n{html.escape(detail)}")
    return "\n".join(lines)


async def post_admin_notice(
    message: Message,
    *,
    reply_to_message_id: int | None = None,
    route_only: bool = False,
    detail: str = "",
    chat_id: int = ADMIN_GROUP_ID,
) -> None:
    """Post a bot-authored, restart-safe reply anchor containing the user ID."""
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "text": _metadata_notice_text(message, route_only=route_only, detail=detail),
        "parse_mode": ParseMode.HTML,
        "disable_web_page_preview": True,
    }
    if reply_to_message_id is not None:
        kwargs["reply_parameters"] = ReplyParameters(
            message_id=reply_to_message_id,
            allow_sending_without_reply=True,
        )
    await telegram_call(lambda: bot.send_message(**kwargs))


async def acknowledge_user(message: Message, text: str) -> None:
    try:
        await telegram_call(lambda: message.answer(text))
    except TelegramAPIError:
        log.info("Could not send user acknowledgement for chat %s", message.chat.id)


async def add_reaction(chat_id: int, message_id: int, emoji: str) -> None:
    """Best-effort Telegram reaction; failures never stop message delivery."""
    try:
        await telegram_call(
            lambda: bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
                is_big=False,
            )
        )
    except TelegramAPIError as exc:
        log.debug("Reaction %s failed for %s/%s: %s", emoji, chat_id, message_id, exc)


async def notify_admin_private(message: Message) -> None:
    """Send the user's message to each admin with a stateless reply route."""
    for admin_id in ADMIN_IDS:
        try:
            if message.forward_origin is None:
                # Preserve the sender's Telegram user ID in forward_origin.
                delivered = await telegram_call(
                    lambda admin_id=admin_id: bot.forward_message(
                        chat_id=admin_id,
                        from_chat_id=message.chat.id,
                        message_id=message.message_id,
                    )
                )
                anchor_id = delivered.message_id
                route_only = False
            else:
                # A customer-submitted forward identifies its original author,
                # not the customer contacting support. Copy it and use our notice.
                copied = await telegram_call(
                    lambda admin_id=admin_id: bot.copy_message(
                        chat_id=admin_id,
                        from_chat_id=message.chat.id,
                        message_id=message.message_id,
                    )
                )
                anchor_id = copied.message_id
                route_only = True

            await post_admin_notice(
                message,
                reply_to_message_id=anchor_id,
                route_only=route_only,
                chat_id=admin_id,
                detail="Reply to this notice if the forwarded/copy item does not identify the customer.",
            )
        except TelegramAPIError as exc:
            log.warning("Could not deliver private admin notification to %s: %s", admin_id, exc)


async def try_copy_incoming(message: Message) -> int | None:
    """Copy content when forwarding would expose an unrelated forward origin."""
    try:
        result = await telegram_call(
            lambda: bot.copy_message(
                chat_id=ADMIN_GROUP_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
        )
        return result.message_id
    except TelegramAPIError as exc:
        log.info("copyMessage failed for incoming message %s: %s", message.message_id, exc)
        return None


async def relay_single_user_message(message: Message) -> None:
    """Relay one private message to the group and admins' private inboxes."""
    await add_reaction(message.chat.id, message.message_id, "🫡")
    await notify_admin_private(message)
    user_sent_a_forward = message.forward_origin is not None

    if user_sent_a_forward:
        # A forwarded message's forward_origin describes its original author,
        # not reliably the person who sent it to this support bot. Copy it and
        # require replies to the bot-authored metadata notice.
        copied_id = await try_copy_incoming(message)
        if copied_id is not None:
            await post_admin_notice(
                message,
                reply_to_message_id=copied_id,
                route_only=True,
            )
            await acknowledge_user(message, "✅ Your message has been sent to the support team.")
        else:
            await post_admin_notice(
                message,
                route_only=True,
                detail="⚠️ Telegram did not allow this content to be copied. The user ID is still available for a reply.",
            )
            await acknowledge_user(
                message,
                "⚠️ Your message reached the support relay, but Telegram did not allow the original content to be copied.",
            )
        return

    # For a normal message sent directly by the user, forwardMessage gives the
    # admin-group copy a MessageOriginUser containing this user's Telegram ID.
    try:
        forwarded = await telegram_call(
            lambda: bot.forward_message(
                chat_id=ADMIN_GROUP_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
        )
        await post_admin_notice(
            message,
            reply_to_message_id=forwarded.message_id,
            route_only=False,
        )
        await acknowledge_user(message, "✅ Your message has been sent to the support team.")
        return
    except TelegramAPIError as exc:
        log.info("forwardMessage failed for incoming message %s: %s", message.message_id, exc)

    # Some Telegram message types (notably service messages and protected
    # content) cannot be forwarded. Try a copy as a best-effort fallback.
    copied_id = await try_copy_incoming(message)
    if copied_id is not None:
        await post_admin_notice(
            message,
            reply_to_message_id=copied_id,
            route_only=True,
            detail="⚠️ This item was copied rather than forwarded. Reply to this bot notice for correct routing.",
        )
        await acknowledge_user(message, "✅ Your message was received by the support team.")
    else:
        await post_admin_notice(
            message,
            route_only=True,
            detail=(
                f"⚠️ {service_event_description(message)} The support team can still reply to this notice."
            ),
        )
        await acknowledge_user(
            message,
            "⚠️ Telegram did not allow this message's original content to be relayed. "
            "The support team has received a notice and can still reply here.",
        )


# ---------------------------------------------------------------------------
# Temporary media-album batching (no persistent user/message mapping)
# ---------------------------------------------------------------------------


AlbumKey = tuple[int, str]
album_buffers: dict[AlbumKey, dict[int, Message]] = {}
album_tasks: dict[AlbumKey, asyncio.Task[None]] = {}
album_lock = asyncio.Lock()


async def relay_album(messages: list[Message]) -> None:
    if not messages:
        return
    messages.sort(key=lambda item: item.message_id)
    first = messages[0]
    for item in messages:
        await add_reaction(item.chat.id, item.message_id, "🫡")
        await notify_admin_private(item)
    ids = [item.message_id for item in messages]
    user_sent_forward = any(item.forward_origin is not None for item in messages)

    result_ids: list[Any] = []
    if user_sent_forward:
        # Do not accidentally route a reply to an album's original author.
        try:
            result_ids = await telegram_call(
                lambda: bot.copy_messages(
                    chat_id=ADMIN_GROUP_ID,
                    from_chat_id=first.chat.id,
                    message_ids=ids,
                )
            )
        except TelegramAPIError as exc:
            log.info("copyMessages failed for user-forwarded album: %s", exc)

        # Best-effort per-item copy if the batch method failed entirely.
        if not result_ids:
            for item in messages:
                copied_id = await try_copy_incoming(item)
                if copied_id is not None:
                    result_ids.append(copied_id)

        count = len(result_ids)
        detail = ""
        if count < len(messages):
            detail = f"⚠️ Telegram copied {count} of {len(messages)} album item(s)."
        if count:
            await post_admin_notice(
                first,
                reply_to_message_id=result_ids[0].message_id,
                route_only=True,
                detail=detail,
            )
            await acknowledge_user(first, "✅ Your album has been sent to the support team.")
        else:
            await post_admin_notice(
                first,
                route_only=True,
                detail="⚠️ Telegram did not allow this album to be copied. Reply to this notice to contact the user.",
            )
            await acknowledge_user(
                first,
                "⚠️ Telegram did not allow the album content to be copied. The support team has received a notice.",
            )
        return

    # forwardMessages keeps Telegram album grouping and each forwarded item's
    # forward_origin metadata. The returned list contains destination message IDs.
    try:
        result_ids = await telegram_call(
            lambda: bot.forward_messages(
                chat_id=ADMIN_GROUP_ID,
                from_chat_id=first.chat.id,
                message_ids=ids,
            )
        )
    except TelegramAPIError as exc:
        log.info("forwardMessages failed for album: %s", exc)

    # If the batch call failed, fall back to forwarding items one by one. This
    # loses album grouping but retains as much content and origin metadata as possible.
    if not result_ids:
        for item in messages:
            try:
                forwarded = await telegram_call(
                    lambda item=item: bot.forward_message(
                        chat_id=ADMIN_GROUP_ID,
                        from_chat_id=item.chat.id,
                        message_id=item.message_id,
                    )
                )
                result_ids.append(forwarded)
            except TelegramAPIError as exc:
                log.info("forwardMessage failed for album item %s: %s", item.message_id, exc)

    count = len(result_ids)
    detail = ""
    if count < len(messages):
        detail = f"⚠️ Telegram forwarded {count} of {len(messages)} album item(s)."
    if count:
        first_destination_id = result_ids[0].message_id
        await post_admin_notice(
            first,
            reply_to_message_id=first_destination_id,
            route_only=False,
            detail=detail,
        )
        await acknowledge_user(first, "✅ Your album has been sent to the support team.")
    else:
        await post_admin_notice(
            first,
            route_only=True,
            detail="⚠️ Telegram did not allow this album to be forwarded/copied. Reply to this notice to contact the user.",
        )
        await acknowledge_user(
            first,
            "⚠️ Telegram did not allow the album content to be relayed. The support team has received a notice.",
        )


async def _flush_album_after_delay(key: AlbumKey) -> None:
    try:
        await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
        async with album_lock:
            buffered = album_buffers.pop(key, {})
            album_tasks.pop(key, None)
        if buffered:
            await relay_album(list(buffered.values()))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Unexpected error while flushing media album %s", key)


async def queue_album(message: Message) -> None:
    key: AlbumKey = (message.chat.id, message.media_group_id or "")
    async with album_lock:
        bucket = album_buffers.setdefault(key, {})
        bucket[message.message_id] = message

        previous = album_tasks.get(key)
        if previous and not previous.done():
            previous.cancel()

        album_tasks[key] = asyncio.create_task(_flush_album_after_delay(key))


async def flush_pending_albums() -> None:
    """Try to flush any short-lived album buffers during a graceful shutdown."""
    async with album_lock:
        pending = [list(items.values()) for items in album_buffers.values() if items]
        album_buffers.clear()
        tasks = list(album_tasks.values())
        album_tasks.clear()

    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    for items in pending:
        try:
            await relay_album(items)
        except Exception:
            log.exception("Could not flush a pending album during shutdown")


# ---------------------------------------------------------------------------
# Stateless reply-target resolution
# ---------------------------------------------------------------------------


_TARGET_MARKER_RE = re.compile(r"(?:^|\n)SUPPORT_USER_ID=(\d+)(?:\n|$)")


def resolve_user_id_from_admin_reply(reply_to: Message) -> int | None:
    """Resolve a user from Bot API origin metadata or the bot's own notice.

    The target notice is authored by this bot and contains the user's ID. It is
    a restart-safe anchor, not a database key or in-memory message mapping.
    """
    if BOT_USER_ID is None:
        return None

    # A normal support forward retains the customer's user ID as its origin.
    origin = reply_to.forward_origin
    if origin is not None and getattr(origin, "type", None) == "user":
        sender = getattr(origin, "sender_user", None)
        if sender is not None and sender.id > 0:
            return sender.id

    # Fallback anchors are trusted only when authored by this bot.
    if reply_to.from_user is None or reply_to.from_user.id != BOT_USER_ID:
        return None

    # Copied items, hidden forward origins, and unsupported message types.
    text = reply_to.text or reply_to.caption or ""
    match = _TARGET_MARKER_RE.search(text)
    if match:
        target_id = int(match.group(1))
        if target_id > 0 and target_id != BOT_USER_ID:
            return target_id
    return None


async def deliver_admin_reply(message: Message, target_user_id: int) -> bool:
    """Copy an admin's reply to the target user; forward as a fallback."""
    try:
        await telegram_call(
            lambda: bot.copy_message(
                chat_id=target_user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
        )
        return True
    except TelegramAPIError as copy_error:
        log.info("copyMessage failed for admin reply %s: %s", message.message_id, copy_error)

    try:
        await telegram_call(
            lambda: bot.forward_message(
                chat_id=target_user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
        )
        return True
    except TelegramAPIError as forward_error:
        log.warning(
            "Could not deliver admin reply %s to %s: %s",
            message.message_id,
            target_user_id,
            forward_error,
        )
        return False


async def admin_feedback(message: Message, text: str) -> None:
    try:
        await telegram_call(
            lambda: bot.send_message(
                chat_id=message.chat.id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_parameters=ReplyParameters(
                    message_id=message.message_id,
                    allow_sending_without_reply=True,
                ),
            )
        )
    except TelegramAPIError:
        log.info("Could not post admin feedback for message %s", message.message_id)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start_command(message: Message) -> None:
    await add_reaction(message.chat.id, message.message_id, "🥳")
    await message.answer(WELCOME_MESSAGE)


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    text = (
        f"{BOT_NAME} — Help\n\n"
        "Users: send a message here to contact support. Text and Telegram media are accepted.\n\n"
        "Commands:\n"
        "/start — Start the bot\n"
        "/help — Show this help\n"
        "/id — Show your Telegram user ID\n"
        "/user — Show user details (or inspect a replied-to request)\n"
        "/admin — Show support administration status\n"
        "/stats — Show current process uptime\n\n"
        "Support admins: reply to a forwarded user message, or reply to the bot's user-info notice. "
        "For content that the user had already forwarded, reply to the bot's notice."
    )
    await message.answer(text)


@router.message(Command("id"))
async def id_command(message: Message) -> None:
    if message.from_user is None:
        await message.answer("I could not identify the sender of this message.")
        return
    await message.answer(f"Your Telegram user ID: {message.from_user.id}")


@router.message(Command("stats"))
async def stats_command(message: Message) -> None:
    uptime = max(0, int(time.monotonic() - BOT_STARTED_AT))
    hours, remainder = divmod(uptime, 3600)
    minutes, seconds = divmod(remainder, 60)
    await message.answer(
        "Runtime information\n"
        f"Status: running\n"
        f"Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}\n"
        "Storage: none\n"
        "Historical statistics: not stored"
    )


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if message.chat.id == ADMIN_GROUP_ID:
        if message.from_user is None or message.from_user.id not in ADMIN_IDS:
            await message.answer("This command is available to configured support admins only.")
            return
        await message.answer(
            "Support administration\n"
            f"Admin group ID: {ADMIN_GROUP_ID}\n"
            f"Configured admins: {len(ADMIN_IDS)}\n"
            "Reply permission: configured admin IDs only"
        )
        return

    await message.answer(
        "The support team is configured. Send your message in this chat and an admin can reply here."
    )


@router.message(Command("user"))
async def user_command(message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        user = message.from_user
        if user is None:
            await message.answer("User information is unavailable.")
            return
        username = f"@{user.username}" if user.username else "No username"
        await message.answer(
            "Your Telegram information\n"
            f"Name: {user.full_name}\n"
            f"Username: {username}\n"
            f"User ID: {user.id}"
        )
        return

    if message.chat.id != ADMIN_GROUP_ID:
        return
    if message.from_user is None or message.from_user.id not in ADMIN_IDS:
        await message.answer("You are not authorized to inspect support users.")
        return
    if message.reply_to_message is None:
        await message.answer("Reply to a forwarded user message or the bot's user-info notice, then use /user.")
        return

    target_id = resolve_user_id_from_admin_reply(message.reply_to_message)
    if target_id is None:
        await message.answer("I could not identify the user. Reply to the bot's user-info notice instead.")
        return

    origin = message.reply_to_message.forward_origin
    sender = getattr(origin, "sender_user", None) if origin else None
    if sender is not None:
        username = f"@{sender.username}" if sender.username else "No username"
        await message.answer(
            "Support user information\n"
            f"Name: {sender.full_name}\n"
            f"Username: {username}\n"
            f"User ID: {target_id}"
        )
    else:
        await message.answer(f"Support user ID: {target_id}")


# ---------------------------------------------------------------------------
# Incoming user messages and admin replies
# ---------------------------------------------------------------------------


@router.message(F.chat.type == ChatType.PRIVATE, F.reply_to_message)
async def admin_private_reply(message: Message) -> None:
    """Configured admins can reply to the bot-authored private user-info notice."""
    if message.from_user is None or message.from_user.id not in ADMIN_IDS:
        return
    if message.text and message.text.startswith("/"):
        return

    target_user_id = resolve_user_id_from_admin_reply(message.reply_to_message)
    if target_user_id is None or target_user_id in ADMIN_IDS:
        return

    await add_reaction(message.chat.id, message.message_id, "🌚")
    await add_reaction(message.chat.id, message.message_id, "🌚")
    delivered = await deliver_admin_reply(message, target_user_id)
    if delivered:
        await admin_feedback(message, f"✅ Reply delivered to user <code>{target_user_id}</code>.")
    else:
        await admin_feedback(message, f"❌ Delivery failed for user <code>{target_user_id}</code>.")


@router.message(F.chat.type == ChatType.PRIVATE)
async def incoming_private_message(message: Message) -> None:
    if message.from_user is None:
        return
    # Admins' own messages must never be relayed as customer messages.
    if message.from_user.id in ADMIN_IDS:
        return

    if message.media_group_id:
        await queue_album(message)
        return

    await relay_single_user_message(message)


@router.message(F.chat.id == ADMIN_GROUP_ID, F.reply_to_message)
async def admin_group_reply(message: Message) -> None:
    # Authorization is checked for every individual reply; group membership or
    # Telegram administrator status alone does not grant relay permission.
    if message.from_user is None or message.from_user.id not in ADMIN_IDS:
        return

    # Do not deliver arbitrary slash commands sent as replies.
    if message.text and message.text.startswith("/"):
        return

    replied_message = message.reply_to_message
    if replied_message is None:
        return

    target_user_id = resolve_user_id_from_admin_reply(replied_message)
    if target_user_id is None:
        await admin_feedback(
            message,
            "⚠️ I could not identify the recipient. Reply directly to a forwarded user message or to the bot's user-info notice.",
        )
        return

    if target_user_id in ADMIN_IDS:
        # Admins can technically be users too, but this guard prevents an
        # accidental support reply loop to an admin account.
        await admin_feedback(message, "⚠️ This request resolves to a configured admin account; delivery was stopped.")
        return

    delivered = await deliver_admin_reply(message, target_user_id)
    if delivered:
        await admin_feedback(message, f"✅ Reply delivered to user <code>{target_user_id}</code>.")
    else:
        await admin_feedback(
            message,
            f"❌ Delivery failed for user <code>{target_user_id}</code>. They may have blocked the bot, deleted the account, or sent a message type Telegram cannot relay.",
        )


@router.message(F.chat.id == ADMIN_GROUP_ID)
async def admin_group_non_reply(message: Message) -> None:
    if message.from_user is None or message.from_user.id not in ADMIN_IDS:
        return
    # Avoid reacting to every ordinary admin group message. Give a hint only
    # for non-command text that looks like an attempted support reply.
    if message.text and not message.text.startswith("/"):
        await message.answer("To contact a user, reply to their forwarded message or the bot's user-info notice.")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


async def main() -> None:
    global BOT_USER_ID

    me = await bot.get_me()
    BOT_USER_ID = me.id
    log.info("Starting %s as @%s (ID %s)", BOT_NAME, me.username, me.id)

    # Make sure polling can start even if a webhook was configured previously.
    # Pending updates are preserved; no database or external queue is created.
    await bot.delete_webhook(drop_pending_updates=False)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Start the support bot"),
            BotCommand(command="help", description="Show help"),
            BotCommand(command="id", description="Show your Telegram ID"),
            BotCommand(command="user", description="Show user information"),
            BotCommand(command="admin", description="Show support admin status"),
            BotCommand(command="stats", description="Show runtime information"),
        ]
    )

    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    try:
        log.info("Polling started. Admin group: %s; configured admins: %s", ADMIN_GROUP_ID, len(ADMIN_IDS))
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        await flush_pending_albums()
        await bot.session.close()
        log.info("Bot stopped")


bot = Bot(token=BOT_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception:
        log.exception("Fatal application error")
        sys.exit(1)
