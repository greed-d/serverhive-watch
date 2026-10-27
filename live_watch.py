#!/usr/bin/env python3
"""Event-driven ServerHive giveaway watcher for an always-on host.

Unlike ``watcher.py``, this process stays connected to Telegram with a user
session. ``events.NewMessage`` handlers fire as soon as @ServerHive or
@ServerRentals receives an update, and the giveaway page is polled every few
seconds. ntfy pushes normally leave within about one second.

GitHub Actions cannot provide second-level latency; run this on a VPS,
Raspberry Pi, or an always-on PC instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
import time
from datetime import timezone
from typing import Any

from telethon import TelegramClient, events, utils
from telethon.errors import UserAlreadyParticipantError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest

import watcher as base


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


API_ID = _env("TG_API_ID")
API_HASH = _env("TG_API_HASH")
SESSION = _env("TG_SESSION_STRING")
CHANNEL = _env("TG_CHANNEL", base.CHANNEL).lstrip("@")
GROUP = _env("TG_GROUP", base.GROUP).lstrip("@")
SENDER = _env("TG_SENDER", base.SENDER).lstrip("@")
SITE_URL = _env("SITE_URL", base.SITE_URL)
SITE_POLL_SECONDS = _env_int("SITE_POLL_SECONDS", 5, 2)
MAX_AGE_SECONDS = _env_int("LIVE_MAX_AGE_SECONDS", 600, 30)
SITE_CHANGE_GUARD_SECONDS = _env_int("LIVE_SITE_CHANGE_GUARD", 60, 10)
AUTO_JOIN = _env("LIVE_AUTO_JOIN", "1").lower() not in ("0", "false", "no", "off")
DRY_RUN = _env("DRY_RUN", "").lower() in ("1", "true", "yes", "on")

NTFY_TOPIC = _env("NTFY_TOPIC")
notifier = base.Notifier(
    NTFY_TOPIC,
    _env("NTFY_SERVER", "https://ntfy.sh"),
    dry_run=DRY_RUN,
)

TEXT_TTL_SECONDS = 900
ID_TTL_SECONDS = 3600
PENDING_TASKS: set[asyncio.Task[Any]] = set()
RECENT_TEXT: dict[str, float] = {}
RECENT_IDS: dict[tuple[int, int], float] = {}

TARGET_ID: int | None = None
CHANNEL_ID: int | None = None
CHANNEL_MARKED_ID: int | None = None
CHANNEL_OK = False


def _prune_recent(now: float | None = None) -> None:
    now = time.time() if now is None else now
    for key, seen_at in list(RECENT_TEXT.items()):
        if now - seen_at > TEXT_TTL_SECONDS:
            del RECENT_TEXT[key]
    for key, seen_at in list(RECENT_IDS.items()):
        if now - seen_at > ID_TTL_SECONDS:
            del RECENT_IDS[key]


def _seen_text(text: str) -> bool:
    if not (text or "").strip():
        return False
    return base.text_key(text) in RECENT_TEXT


def _mark_text(text: str) -> None:
    if (text or "").strip():
        RECENT_TEXT[base.text_key(text)] = time.time()


def _seen_id(chat_id: int | None, message_id: int) -> bool:
    return (int(chat_id or 0), int(message_id)) in RECENT_IDS


def _mark_id(chat_id: int | None, message_id: int) -> None:
    RECENT_IDS[(int(chat_id or 0), int(message_id))] = time.time()


def _age_seconds(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return (base.utcnow() - value.astimezone(timezone.utc)).total_seconds()
    except (AttributeError, ValueError):
        return None


def _too_old(value: Any) -> bool:
    age = _age_seconds(value)
    return age is not None and age > MAX_AGE_SECONDS


def _latency_ms(value: Any) -> int | None:
    age = _age_seconds(value)
    return int(age * 1000) if age is not None else None


def _task_done(task: asyncio.Task[Any]) -> None:
    PENDING_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        base.log("live: notification task failed", error=type(exc).__name__)


def _spawn(coro: Any) -> None:
    task = asyncio.create_task(coro)
    PENDING_TASKS.add(task)
    task.add_done_callback(_task_done)


async def notify(
    title: str,
    body: str,
    *,
    priority: str = "high",
    tags: list[str] | None = None,
    click: str | None = None,
    actions: str | None = None,
) -> bool:
    ok = await asyncio.to_thread(
        notifier.send,
        title,
        body,
        priority=priority,
        tags=tags,
        click=click,
        actions=actions,
    )
    if not ok:
        base.log("live: ntfy failed", title=title)
    return ok


def _handle_for(source: str) -> str:
    return CHANNEL if source == "channel" else GROUP


def _post_url(source: str, message_id: int) -> str:
    return f"https://t.me/{_handle_for(source)}/{int(message_id)}"


def _entity_label(entity: Any) -> str:
    if entity is None:
        return "unknown"
    name = " ".join(
        part
        for part in (
            getattr(entity, "first_name", None),
            getattr(entity, "last_name", None),
        )
        if part
    )
    return name or getattr(entity, "title", None) or getattr(entity, "username", None) or "unknown"


def message_body(
    source: str,
    message: Any,
    text: str,
    links: list[str],
    sender_label: str | None = None,
) -> str:
    lines = [f"{base.fmt_date(getattr(message, 'date', None))} \u00b7 @{_handle_for(source)}"]
    if source == "group" and sender_label:
        lines.append(f"From: {sender_label}")
    lines.append("")
    lines.append(base.truncate(text, 800) or "[media-only post \u2014 open in Telegram]")
    if links:
        lines.append("")
        lines.append("Links:")
        lines.extend(links[:4])
    lines.append("")
    lines.append(_post_url(source, message.id))
    return "\n".join(lines)


def _alert_actions(message_url: str) -> str:
    return base.ntfy_actions(
        ("Open Telegram", message_url),
        ("Giveaway page", SITE_URL),
    )


def _site_body(status: int, title: str, visible: str, challenge: bool) -> str:
    excerpt = re.sub(r"\s+", " ", visible)[:600].strip()
    lines = [f"HTTP {status} \u00b7 {SITE_URL}"]
    if title:
        lines.append(f"Title: {base.truncate(title, 120)}")
    if challenge:
        lines.append("Cloudflare challenge detected; open the page manually.")
    lines.append("")
    lines.append(excerpt or "[no visible text]")
    lines.append("")
    lines.append(SITE_URL)
    return "\n".join(lines)


async def check_site_once(
    previous: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch and compare the giveaway page once. Returns (state, alerts)."""
    status, body, _ = await asyncio.to_thread(
        base.http_get,
        SITE_URL,
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        retries=0,
    )
    page = body.decode("utf-8", "replace")
    visible = base.visible_text(page)
    title_match = base.TITLE_RE.search(page)
    title = base.strip_html(title_match.group(1)) if title_match else ""
    challenge = bool(base.CHALLENGE_RE.search(visible))
    reachable = 200 <= status < 300
    digest = hashlib.sha256(
        f"{status}\n{re.sub(r'\s+', ' ', visible)[:4000]}".encode("utf-8")
    ).hexdigest()

    now = time.time()
    prev_reachable = bool(previous and previous.get("reachable"))
    prev_hash = (previous or {}).get("hash")
    last_change = float((previous or {}).get("last_change_alert") or 0)
    alerts: list[dict[str, Any]] = []

    if reachable and not prev_reachable:
        alerts.append(
            {
                "title": "ServerHive giveaway page is UP",
                "body": _site_body(status, title, visible, challenge),
                "priority": "urgent",
                "tags": ["rotating_light"],
            }
        )
        last_change = now
    elif reachable and prev_reachable and digest != prev_hash:
        if now - last_change >= SITE_CHANGE_GUARD_SECONDS:
            alerts.append(
                {
                    "title": "ServerHive giveaway page changed",
                    "body": _site_body(status, title, visible, challenge),
                    "priority": "high",
                    "tags": ["gift"],
                }
            )
            last_change = now
    elif not reachable and prev_reachable:
        alerts.append(
            {
                "title": "ServerHive giveaway page went down",
                "body": _site_body(status, title, visible, challenge),
                "priority": "low",
                "tags": ["warning"],
            }
        )

    new_previous = {
        "status": status,
        "reachable": reachable,
        "challenge": challenge,
        "hash": digest,
        "title": base.truncate(title, 100),
        "last_change_alert": last_change,
        "checked": base.iso(base.utcnow()),
    }
    return new_previous, alerts


async def site_loop() -> None:
    previous: dict[str, Any] | None = None
    last_log = 0.0
    while True:
        try:
            previous, alerts = await check_site_once(previous)
            actions = base.ntfy_actions(
                ("Open giveaway page", SITE_URL),
                ("Open @ServerHive", f"https://t.me/{CHANNEL}"),
            )
            for alert in alerts:
                _spawn(
                    notify(
                        alert["title"],
                        alert["body"],
                        priority=alert["priority"],
                        tags=alert["tags"],
                        click=SITE_URL,
                        actions=actions,
                    )
                )
            now = time.time()
            if alerts or now - last_log >= 300:
                base.log(
                    "live: site",
                    status=previous.get("status"),
                    reachable=previous.get("reachable"),
                    alerts=len(alerts),
                )
                last_log = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            base.log("live: site error", error=type(exc).__name__)
        await asyncio.sleep(SITE_POLL_SECONDS)


async def on_channel(event: Any) -> None:
    message = event.message
    _prune_recent()
    if _seen_id(event.chat_id, message.id):
        return
    _mark_id(event.chat_id, message.id)
    if getattr(message, "action", None) is not None:
        return
    if _too_old(getattr(message, "date", None)):
        base.log("live: stale channel message skipped", id=message.id)
        return

    text = message.message or ""
    links = base.extract_message_links(message, text)
    if text and _seen_text(text):
        base.log("live: duplicate channel text skipped", id=message.id)
        return
    _mark_text(text)

    giveaway = base.is_giveaway(text, links)
    url = _post_url("channel", message.id)
    title = "ServerHive GIVEAWAY" if giveaway else "New @ServerHive post"
    _spawn(
        notify(
            title,
            message_body("channel", message, text, links),
            priority="urgent" if giveaway else "high",
            tags=["gift"] if giveaway else ["mega"],
            click=url,
            actions=_alert_actions(url),
        )
    )
    base.log(
        "live: channel alert",
        id=message.id,
        giveaway=giveaway,
        latency_ms=_latency_ms(getattr(message, "date", None)),
    )


async def on_group(event: Any) -> None:
    message = event.message
    _prune_recent()
    if _seen_id(event.chat_id, message.id):
        return
    _mark_id(event.chat_id, message.id)
    if getattr(message, "action", None) is not None:
        return
    if _too_old(getattr(message, "date", None)):
        base.log("live: stale group message skipped", id=message.id)
        return

    text = message.message or ""
    links = base.extract_message_links(message, text)
    sender = getattr(event, "sender", None)
    username = (getattr(sender, "username", None) or "").lower()
    marked_sender_id = getattr(event, "sender_id", None)

    is_target = TARGET_ID is not None and marked_sender_id == TARGET_ID
    is_channel_post = (
        CHANNEL_MARKED_ID is not None
        and marked_sender_id == CHANNEL_MARKED_ID
    )
    site_link = "giveaway.serverhive.in" in (text + " " + " ".join(links)).lower()

    # If an ID lookup failed, fall back to the username. This requires the
    # sender entity, which is normally already cached on the event.
    if sender is None and (TARGET_ID is None or CHANNEL_MARKED_ID is None):
        try:
            sender = await event.get_sender()
        except Exception:
            sender = None
        username = (getattr(sender, "username", None) or "").lower()
    if TARGET_ID is None:
        is_target = username == SENDER.lower()
    if CHANNEL_MARKED_ID is None:
        is_channel_post = username == CHANNEL.lower()

    if is_channel_post and CHANNEL_OK:
        return
    if not (is_target or is_channel_post or site_link):
        return

    if sender is None:
        try:
            sender = await event.get_sender()
        except Exception:
            sender = None

    if text and _seen_text(text):
        base.log("live: duplicate group text skipped", id=message.id)
        return
    _mark_text(text)

    giveaway = base.is_giveaway(text, links) or site_link
    url = _post_url("group", message.id)
    sender_label = _entity_label(sender)
    if is_target:
        title = f"GIVEAWAY from @{SENDER}" if giveaway else f"New from @{SENDER}"
        priority = "urgent" if giveaway else "high"
        tags = ["gift"] if giveaway else ["speech_balloon"]
    elif is_channel_post:
        title = f"Giveaway post in @{GROUP}" if giveaway else f"New post in @{GROUP}"
        priority = "high"
        tags = ["gift"] if giveaway else ["mega"]
    else:
        title = f"Giveaway link in @{GROUP}"
        priority = "urgent"
        tags = ["gift"]

    _spawn(
        notify(
            title,
            message_body("group", message, text, links, sender_label),
            priority=priority,
            tags=tags,
            click=url,
            actions=_alert_actions(url),
        )
    )
    base.log(
        "live: group alert",
        id=message.id,
        target=is_target,
        channel_post=is_channel_post,
        giveaway=giveaway,
        latency_ms=_latency_ms(getattr(message, "date", None)),
    )


async def ensure_joined(
    client: TelegramClient,
    entity: Any,
    label: str,
    auto_join: bool,
) -> bool:
    if not auto_join:
        return False
    try:
        await client(JoinChannelRequest(entity))
        base.log("live: joined chat", chat=label)
        return True
    except UserAlreadyParticipantError:
        base.log("live: already a member", chat=label)
        return True
    except Exception as exc:
        base.log("live: join failed", chat=label, error=type(exc).__name__)
        return False


def _config_error() -> int | None:
    missing = []
    if not notifier.configured and not DRY_RUN:
        missing.append("NTFY_TOPIC")
    for name, value in (
        ("TG_API_ID", API_ID),
        ("TG_API_HASH", API_HASH),
        ("TG_SESSION_STRING", SESSION),
    ):
        if not value:
            missing.append(name)
    if missing:
        print(
            "live_watch: missing required environment: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2
    try:
        int(API_ID)
    except ValueError:
        print("live_watch: TG_API_ID must be an integer", file=sys.stderr)
        return 2
    return None


async def main() -> int:
    global TARGET_ID, CHANNEL_ID, CHANNEL_MARKED_ID, CHANNEL_OK

    config_error = _config_error()
    if config_error is not None:
        return config_error

    client = TelegramClient(
        StringSession(SESSION),
        int(API_ID),
        API_HASH,
        device_model="ServerHive Live",
        system_version="1.0",
        app_version="1.0",
        lang_code="en",
        system_lang_code="en",
        connection_retries=3,
        request_retries=3,
        timeout=20,
        auto_reconnect=True,
        sequential_updates=False,
        catch_up=True,
    )

    site_task: asyncio.Task[Any] | None = None
    try:
        try:
            await client.connect()
        except Exception as exc:
            base.log("live: connect failed", error=type(exc).__name__)
            return 1
        _install_signal_handlers(client)

        if not await client.is_user_authorized():
            base.log("live: Telegram session is not authorized")
            await notify(
                "ServerHive live watcher: session invalid",
                "The Telegram session is no longer authorized. Run login.py "
                "again and update TG_SESSION_STRING, then restart the service.",
                priority="high",
                tags=["warning"],
            )
            return 1

        me = await client.get_me()
        base.log(
            "live: connected",
            user=getattr(me, "username", None) or getattr(me, "id", None),
        )

        try:
            target = await client.get_entity(SENDER)
            TARGET_ID = int(target.id)
        except Exception:
            TARGET_ID = None
            base.log("live: sender lookup failed; filtering by username", sender=SENDER)

        try:
            channel_entity = await client.get_entity(CHANNEL)
            group_entity = await client.get_entity(GROUP)
        except Exception as exc:
            base.log("live: chat lookup failed", error=type(exc).__name__)
            await notify(
                "ServerHive live watcher: cannot find chats",
                f"Could not resolve @{CHANNEL} or @{GROUP}. Check TG_CHANNEL/TG_GROUP "
                "and that the account can see both chats.",
                priority="high",
                tags=["warning"],
            )
            return 1

        CHANNEL_ID = int(channel_entity.id)
        CHANNEL_MARKED_ID = utils.get_peer_id(channel_entity)
        if AUTO_JOIN:
            CHANNEL_OK = await ensure_joined(client, channel_entity, CHANNEL, True)
            group_ok = await ensure_joined(client, group_entity, GROUP, True)
        else:
            base.log("live: auto-join disabled; assuming memberships")
            CHANNEL_OK = True
            group_ok = True

        if not group_ok:
            await notify(
                "ServerHive live watcher: @ServerRentals unavailable",
                f"Could not join @{GROUP}. Group alerts may be missing. "
                "Join the group with this Telegram account or set LIVE_AUTO_JOIN=1.",
                priority="high",
                tags=["warning"],
            )

        client.add_event_handler(on_channel, events.NewMessage(chats=channel_entity))
        client.add_event_handler(on_group, events.NewMessage(chats=group_entity))
        site_task = asyncio.create_task(site_loop())

        await notify(
            "ServerHive LIVE watcher started",
            f"Event-driven Telegram watch: @{CHANNEL} and @{GROUP} "
            f"(messages from @{SENDER}).\n"
            f"Giveaway page poll: every {SITE_POLL_SECONDS}s.\n"
            f"Channel direct: {'yes' if CHANNEL_OK else 'no (group fallback)'}.\n"
            f"Group: {'yes' if group_ok else 'unavailable'}.\n\n"
            "Alerts are best-effort and can arrive in about a second.",
            priority="low",
            tags=["white_check_mark"],
            click=SITE_URL,
        )
        base.log(
            "live: watching",
            channel=CHANNEL,
            group=GROUP,
            sender=SENDER,
            site_poll=SITE_POLL_SECONDS,
            channel_ok=CHANNEL_OK,
            group_ok=group_ok,
        )

        await client.run_until_disconnected()
        return 0
    finally:
        if site_task is not None:
            site_task.cancel()
            try:
                await site_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                base.log("live: site task stopped", error=type(exc).__name__)
        pending = list(PENDING_TASKS)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await client.disconnect()


def _install_signal_handlers(client: TelegramClient) -> None:
    import signal

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig, lambda: asyncio.create_task(client.disconnect())
            )
        except (NotImplementedError, RuntimeError):
            pass


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
