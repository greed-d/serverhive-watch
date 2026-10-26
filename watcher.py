#!/usr/bin/env python3
"""Watch ServerHive giveaway sources and push alerts through ntfy.

Sources:
  * @ServerHive public channel (t.me/s web preview, no login)
  * @ServerRentals group (Telethon user session, filters @Shra1V32)
  * https://giveaway.serverhive.in/ (HTTP status + visible-content changes)

The script is intentionally dependency-light: Telethon is imported lazily so
the public channel and site checks still work if it is not installed.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CHANNEL = "ServerHive"
GROUP = "ServerRentals"
SENDER = "Shra1V32"
SITE_URL = "https://giveaway.serverhive.in/"
CHANNEL_URL = f"https://t.me/s/{CHANNEL}"
GROUP_URL = f"https://t.me/{GROUP}"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

GIVEAWAY_RE = re.compile(
    r"(?i)\b(?:"
    r"give[\s-]?away|"
    r"gift(?:ing)?|"
    r"winner(?:s)?|"
    r"claim|redeem|"
    r"randomly\s+chosen|"
    r"first[\s-]+come|"
    r"limited\s+(?:slots?|spots?|seats?)|"
    r"free\s+(?:server|vps|hosting|build|account|access)|"
    r"congrat(?:s|ulations)"
    r")\b"
)

WIDGET_RE = re.compile(
    r'<div class="tgme_widget_message(?:\s[^"]*)?"[^>]*data-post="([^"]+)"(.*?)'
    r'(?=<div class="tgme_widget_message(?:\s[^"]*)?"[^>]*data-post=|\Z)',
    re.S,
)
TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S
)
AUTHOR_RE = re.compile(
    r'<span class="tgme_widget_message_from_author"[^>]*>(.*?)</span>', re.S
)
OWNER_RE = re.compile(
    r'<a class="tgme_widget_message_owner_name"[^>]*>(.*?)</a>', re.S
)
DATE_RE = re.compile(r'<time[^>]+datetime="([^"]+)"')
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
CHALLENGE_RE = re.compile(
    r"just a moment|enable javascript and cookies|cf-chl|attention required",
    re.I,
)
URL_RE = re.compile(r'https?://[^\s<>"\']+')


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def fmt_date(value: datetime | None) -> str:
    if value is None:
        return "unknown time"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def truncate(value: str, limit: int) -> str:
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "\u2026"


def log(message: str, **fields: Any) -> None:
    parts = [f"{utcnow():%Y-%m-%dT%H:%M:%SZ}", message]
    for key, value in fields.items():
        if value is not None:
            parts.append(f"{key}={value}")
    print(" ".join(parts), flush=True)


def strip_html(value: str) -> str:
    value = re.sub(r"(?i)<br\s*/?>", "\n", value or "")
    value = re.sub(r"(?i)</(?:p|div|blockquote|li|h[1-6])>", "\n", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = html_lib.unescape(value)
    value = value.replace("\u200b", "").replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def is_giveaway(text: str, links: list[str] | tuple[str, ...] = ()) -> bool:
    haystack = f"{text or ''}\n" + "\n".join(links or ())
    if "giveaway.serverhive.in" in haystack.lower():
        return True
    return bool(GIVEAWAY_RE.search(text or ""))


def is_recent(value: datetime | str | None, hours: float) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return utcnow() - value.astimezone(timezone.utc) <= timedelta(hours=hours)


def text_key(text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def text_seen(state: dict[str, Any], text: str) -> bool:
    if not (text or "").strip():
        return False
    key = text_key(text)
    return any(item.get("h") == key for item in state.get("recent_text", []))


def mark_text(state: dict[str, Any], text: str) -> None:
    if not (text or "").strip():
        return
    state.setdefault("recent_text", []).append(
        {"h": text_key(text), "t": iso(utcnow())}
    )
    prune_state(state)


def prune_state(state: dict[str, Any]) -> None:
    cutoff = utcnow() - timedelta(hours=72)
    kept = []
    for item in state.get("recent_text", []):
        try:
            seen_at = datetime.fromisoformat(str(item.get("t", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=timezone.utc)
        if seen_at >= cutoff:
            kept.append(item)
    state["recent_text"] = kept[-200:]


# ---------------------------------------------------------------------------
# HTTP + notification helpers
# ---------------------------------------------------------------------------


def http_get(
    url: str,
    *,
    timeout: int = 20,
    headers: dict[str, str] | None = None,
    retries: int = 1,
) -> tuple[int, bytes, dict[str, str]]:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        request_headers.update(headers)

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers=request_headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                last_error = exc
                time.sleep(2 + attempt)
                continue
            return exc.code, body, dict(exc.headers or {})
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 + attempt)
                continue
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc

    raise RuntimeError(f"{type(last_error).__name__}: {last_error}")


def ntfy_actions(*pairs: tuple[str, str]) -> str:
    return "; ".join(f"view, {label}, {url}, clear=true" for label, url in pairs)


class Notifier:
    def __init__(self, topic: str, server: str = "https://ntfy.sh", dry_run: bool = False):
        self.topic = (topic or "").strip()
        self.server = (server or "https://ntfy.sh").rstrip("/")
        self.dry_run = dry_run

        if self.topic.startswith(("http://", "https://")):
            self.url = self.topic
        elif self.topic:
            self.url = f"{self.server}/{urllib.parse.quote(self.topic, safe='')}"
        else:
            self.url = None

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def send(
        self,
        title: str,
        body: str,
        *,
        priority: str = "default",
        tags: list[str] | None = None,
        click: str | None = None,
        actions: str | None = None,
    ) -> bool:
        if self.dry_run or not self.url:
            print("\n--- notification ---", flush=True)
            print(f"Title: {title}", flush=True)
            print(f"Priority: {priority}", flush=True)
            if tags:
                print(f"Tags: {','.join(tags)}", flush=True)
            if click:
                print(f"Click: {click}", flush=True)
            if actions:
                print(f"Actions: {actions}", flush=True)
            print(body, flush=True)
            print("--- end ---\n", flush=True)
            return True

        headers = {
            "Title": title[:120],
            "Priority": priority,
            "Markdown": "yes",
            "User-Agent": "serverhive-watch/1.0",
        }
        if tags:
            headers["Tags"] = ",".join(tags)
        if click:
            headers["Click"] = click
        if actions:
            headers["Actions"] = actions

        data = (body or "").encode("utf-8")
        for attempt in range(2):
            request = urllib.request.Request(
                self.url, data=data, headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    response.read()
                log("ntfy: sent", title=title, priority=priority)
                return True
            except urllib.error.HTTPError as exc:
                log("ntfy: HTTP error", status=exc.code)
                if exc.code == 429 and attempt == 0:
                    time.sleep(5)
                    continue
                return False
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                log("ntfy: send failed", error=type(exc).__name__)
                if attempt == 0:
                    time.sleep(3)
                    continue
                return False
        return False


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        log("state: ignored non-object state file")
    except FileNotFoundError:
        pass
    except Exception as exc:  # corrupt state should not stop monitoring
        log("state: could not read; starting fresh", error=type(exc).__name__)
    return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def bump_error(
    state: dict[str, Any],
    source: str,
    message: str,
    notifier: Notifier,
    *,
    force: bool = False,
) -> None:
    errors = state.setdefault("errors", {})
    entry = errors.setdefault(source, {"count": 0})
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["last"] = truncate(message, 300)
    entry["at"] = iso(utcnow())
    count = entry["count"]
    if force or count in (1, 3) or count % 12 == 0:
        notifier.send(
            f"ServerHive watcher: {source} failing",
            f"{message}\n\nFailures in a row: {count}.",
            priority="high" if force or count >= 3 else "default",
            tags=["warning"],
        )


def reset_error(state: dict[str, Any], source: str) -> None:
    state.get("errors", {}).pop(source, None)


# ---------------------------------------------------------------------------
# Public channel
# ---------------------------------------------------------------------------


@dataclass
class Post:
    post_id: int
    url: str
    text: str
    links: list[str]
    author: str | None
    owner: str | None
    date: datetime | None


def parse_channel_page(source: str) -> list[Post]:
    posts: list[Post] = []
    for match in WIDGET_RE.finditer(source):
        post_ref, chunk = match.group(1), match.group(2)
        try:
            post_id = int(post_ref.rsplit("/", 1)[1])
        except (ValueError, IndexError):
            continue

        text_match = TEXT_RE.search(chunk)
        text_html = text_match.group(1) if text_match else ""
        text = strip_html(text_html)
        links = [
            html_lib.unescape(link)
            for link in re.findall(r'<a[^>]+href="([^"]+)"', text_html)
            if link.startswith(("http://", "https://"))
        ]

        author_match = AUTHOR_RE.search(chunk)
        author = strip_html(author_match.group(1)) if author_match else None
        owner_match = OWNER_RE.search(chunk)
        owner = strip_html(owner_match.group(1)) if owner_match else None

        date = None
        date_match = DATE_RE.search(chunk)
        if date_match:
            try:
                date = datetime.fromisoformat(date_match.group(1).replace("Z", "+00:00"))
            except ValueError:
                pass

        posts.append(
            Post(
                post_id=post_id,
                url=f"https://t.me/{post_ref}",
                text=text,
                links=links,
                author=author,
                owner=owner,
                date=date,
            )
        )

    by_id = {post.post_id: post for post in posts}
    return sorted(by_id.values(), key=lambda post: post.post_id)


def fetch_channel_posts(last_id: int | None) -> list[Post]:
    collected: list[Post] = []
    before: int | None = None

    for page in range(4):
        url = CHANNEL_URL + (f"?before={before}" if before is not None else "")
        status, body, _ = http_get(url, retries=1)
        if status != 200:
            raise RuntimeError(f"channel HTTP {status}")

        posts = parse_channel_page(body.decode("utf-8", "replace"))
        if not posts:
            raise RuntimeError("channel page had no parsed posts")

        collected.extend(posts)
        oldest = min(post.post_id for post in posts)

        if last_id is None or oldest <= last_id or len(posts) < 15 or page == 3:
            break
        before = oldest

    by_id = {post.post_id: post for post in collected}
    return sorted(by_id.values(), key=lambda post: post.post_id)


def format_channel_post(post: Post) -> str:
    lines = [f"{fmt_date(post.date)} \u00b7 @{CHANNEL}"]
    if post.author:
        lines.append(f"Signed: {post.author}")
    lines.append("")
    lines.append(truncate(post.text, 800) or "[media-only post \u2014 open in Telegram]")
    if post.links:
        lines.append("")
        lines.append("Links:")
        lines.extend(post.links[:4])
    lines.append("")
    lines.append(post.url)
    return "\n".join(lines)


def send_channel_alert(notifier: Notifier, post: Post) -> bool:
    giveaway = is_giveaway(post.text, post.links)
    return notifier.send(
        "Possible ServerHive giveaway" if giveaway else "New @ServerHive post",
        format_channel_post(post),
        priority="high" if giveaway else "default",
        tags=["gift"] if giveaway else ["mega"],
        click=post.url,
        actions=ntfy_actions(
            ("Open Telegram", post.url),
            ("Giveaway page", SITE_URL),
        ),
    )


def check_channel(state: dict[str, Any], notifier: Notifier) -> bool:
    raw_last = state.get("channel_last_id")
    last_id = int(raw_last) if raw_last else None
    posts = fetch_channel_posts(last_id)
    newest = max(post.post_id for post in posts)

    if last_id is None:
        # First run (or lost state): do not replay the channel, but still flag
        # recent giveaways in case the watcher was down when they were posted.
        recent = [
            post
            for post in posts
            if is_recent(post.date, 48) and is_giveaway(post.text, post.links)
        ]
        processed_until = newest
        for post in recent[:5]:
            if text_seen(state, post.text):
                continue
            if not send_channel_alert(notifier, post):
                processed_until = max(post.post_id - 1, 0)
                log("channel: baseline alert failed; will retry", post=post.post_id)
                break
            mark_text(state, post.text)
        if len(recent) > 5:
            processed_until = min(processed_until, recent[4].post_id)
        state["channel_last_id"] = processed_until
        log(
            "channel: baseline",
            last_id=processed_until,
            posts=len(posts),
            recent_giveaways=len(recent),
        )
        return True

    new_posts = [post for post in posts if post.post_id > last_id]
    if not new_posts:
        log("channel: no new posts", last_id=last_id)
        return True

    sent = 0
    processed_until = last_id
    for post in new_posts[:10]:
        if text_seen(state, post.text):
            log("channel: duplicate text skipped", post=post.post_id)
            processed_until = post.post_id
            continue
        if not send_channel_alert(notifier, post):
            log("channel: notification failed; will retry", post=post.post_id)
            break
        mark_text(state, post.text)
        processed_until = post.post_id
        sent += 1

    # Keep any posts after the batch/failure pending for the next run.
    state["channel_last_id"] = processed_until
    log("channel: scanned", new=len(new_posts), alerted=sent, up_to=processed_until)
    return True


# ---------------------------------------------------------------------------
# Giveaway site
# ---------------------------------------------------------------------------


def check_site(state: dict[str, Any], notifier: Notifier) -> bool:
    previous = state.get("site") if isinstance(state.get("site"), dict) else {}
    status, body, _ = http_get(
        SITE_URL,
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        retries=0,
    )
    page = body.decode("utf-8", "replace")
    visible = strip_html(page)
    title_match = TITLE_RE.search(page)
    title = strip_html(title_match.group(1)) if title_match else ""
    challenge = bool(CHALLENGE_RE.search(visible))
    reachable = 200 <= status < 300
    digest = hashlib.sha256(
        f"{status}\n{re.sub(r'\s+', ' ', visible)[:4000]}".encode("utf-8")
    ).hexdigest()

    previous_reachable = bool(previous.get("reachable"))
    previous_digest = previous.get("hash")

    def body_text() -> str:
        excerpt = re.sub(r"\s+", " ", visible)[:600].strip()
        lines = [f"HTTP {status} \u00b7 {SITE_URL}"]
        if title:
            lines.append(f"Title: {truncate(title, 120)}")
        if challenge:
            lines.append("Cloudflare challenge detected; open the page manually.")
        lines.append("")
        lines.append(excerpt or "[no visible text]")
        lines.append("")
        lines.append(SITE_URL)
        return "\n".join(lines)

    alert_ok = True

    if reachable and not previous_reachable:
        alert_ok = notifier.send(
            "ServerHive giveaway page is UP",
            body_text(),
            priority="high",
            tags=["rotating_light"],
            click=SITE_URL,
            actions=ntfy_actions(
                ("Open giveaway page", SITE_URL),
                ("Open @ServerHive", f"https://t.me/{CHANNEL}"),
            ),
        )
    elif reachable and previous_reachable and digest != previous_digest:
        last_change = previous.get("last_change_alert")
        can_alert = True
        if last_change:
            try:
                seen = datetime.fromisoformat(str(last_change).replace("Z", "+00:00"))
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
                can_alert = utcnow() - seen >= timedelta(minutes=30)
            except ValueError:
                pass
        if can_alert:
            alert_ok = notifier.send(
                "ServerHive giveaway page changed",
                body_text(),
                priority="high",
                tags=["gift"],
                click=SITE_URL,
                actions=ntfy_actions(("Open giveaway page", SITE_URL)),
            )
            if alert_ok:
                previous["last_change_alert"] = iso(utcnow())
    elif not reachable and previous_reachable:
        alert_ok = notifier.send(
            "ServerHive giveaway page went down",
            body_text(),
            priority="low",
            tags=["warning"],
            click=SITE_URL,
        )

    new_site = {
        "status": status,
        "reachable": reachable,
        "challenge": challenge,
        "hash": digest,
        "title": truncate(title, 100),
        "checked": iso(utcnow()),
        "last_change_alert": previous.get("last_change_alert"),
    }
    if alert_ok:
        state["site"] = new_site
    else:
        # Keep the previous state so the transition/change is retried instead
        # of being lost when ntfy is temporarily unavailable.
        log("site: notification failed; will retry")
    log("site: checked", status=status, reachable=reachable, challenge=challenge)
    return True


# ---------------------------------------------------------------------------
# Telegram group (Telethon user session)
# ---------------------------------------------------------------------------


@dataclass
class GroupMessage:
    message_id: int
    url: str
    text: str
    links: list[str]
    date: datetime | None
    author: str
    giveaway: bool
    source: str


def extract_message_links(message: Any, text: str) -> list[str]:
    links: list[str] = []
    for match in URL_RE.finditer(text or ""):
        links.append(match.group(0).rstrip(".,);]"))

    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", None)
        if url:
            links.append(url)

    preview = getattr(message, "web_preview", None)
    if preview is not None:
        preview_url = getattr(preview, "url", None)
        if preview_url:
            links.append(preview_url)

    try:
        buttons = getattr(message, "buttons", None) or []
        for row in buttons:
            for button in row:
                url = getattr(button, "url", None)
                if url:
                    links.append(url)
    except Exception:
        pass

    deduped: list[str] = []
    seen: set[str] = set()
    for link in links:
        if link and link not in seen:
            seen.add(link)
            deduped.append(link)
    return deduped


def sender_info(message: Any, target_username: str, target_id: int | None):
    try:
        sender = message.sender or message.get_sender()
    except Exception:
        sender = None

    username = (getattr(sender, "username", None) or "").lower()
    sender_id = getattr(sender, "id", None)

    sender_chat = getattr(message, "sender_chat", None)
    chat_username = (getattr(sender_chat, "username", None) or "").lower()

    display = (
        " ".join(
            part
            for part in (
                getattr(sender, "first_name", None),
                getattr(sender, "last_name", None),
            )
            if part
        )
        or getattr(sender, "title", None)
        or getattr(sender_chat, "title", None)
        or username
        or "unknown"
    )

    is_target = username == target_username.lower() or (
        target_id is not None and sender_id == target_id
    )
    is_channel = CHANNEL.lower() in (username, chat_username)
    return is_target, is_channel, display, username, chat_username


def group_notification(
    entries: list[GroupMessage],
    *,
    has_target: bool,
    has_third_party: bool,
) -> tuple[str, str, str, list[str], str]:
    giveaway = any(entry.giveaway for entry in entries)
    if has_target:
        title = f"New from @{SENDER} in @{GROUP}"
    elif has_third_party:
        title = f"Giveaway link in @{GROUP}"
    else:
        title = f"Giveaway post in @{GROUP}"

    tags = ["gift"] if giveaway else ["speech_balloon"]
    lines: list[str] = []
    if len(entries) == 1:
        lines.append(f"{fmt_date(entries[0].date)} \u00b7 @{GROUP}")
    else:
        lines.append(f"{len(entries)} new messages \u00b7 @{GROUP}")
    lines.append("")

    for entry in entries[:5]:
        label = entry.author
        if entry.source == "channel":
            label = f"{label} (channel post)"
        elif entry.source == "third-party":
            label = f"{label} (member post)"
        lines.append(f"#{entry.message_id} \u00b7 {fmt_date(entry.date)} \u00b7 {label}")
        lines.append(truncate(entry.text, 600) or "[media-only message]")
        if entry.links:
            lines.append("Links: " + " ".join(entry.links[:3]))
        lines.append(entry.url)
        lines.append("")

    if len(entries) > 5:
        lines.append(f"\u2026 and {len(entries) - 5} more. Open {GROUP_URL}")

    click = entries[-1].url
    return title, "\n".join(lines), ("high" if giveaway else "default"), tags, click


def check_group(
    state: dict[str, Any], notifier: Notifier, *, channel_ok: bool = True
) -> bool | None:
    api_id = (os.getenv("TG_API_ID") or "").strip()
    api_hash = (os.getenv("TG_API_HASH") or "").strip()
    session = (os.getenv("TG_SESSION_STRING") or "").strip()
    if not (api_id and api_hash and session):
        log("group: skipped (TG_API_ID, TG_API_HASH, TG_SESSION_STRING not all set)")
        return None

    try:
        from telethon import TelegramClient
        from telethon.errors import FloodWaitError
        from telethon.sessions import StringSession
    except Exception as exc:
        log("group: telethon import failed", error=type(exc).__name__)
        bump_error(state, "group", f"Telethon import failed: {type(exc).__name__}", notifier, force=True)
        return False

    try:
        client = TelegramClient(
            StringSession(session),
            int(api_id),
            api_hash,
            device_model="ServerHive Watch",
            system_version="1.0",
            app_version="1.0",
            lang_code="en",
            system_lang_code="en",
            connection_retries=1,
            request_retries=1,
            timeout=20,
        )
    except Exception as exc:
        log("group: client setup failed", error=type(exc).__name__)
        bump_error(state, "group", f"Telegram client setup failed: {type(exc).__name__}", notifier, force=True)
        return False

    group_name = (os.getenv("TG_GROUP") or GROUP).strip()
    group_handle = group_name.lstrip("@").strip()
    sender_name = (os.getenv("TG_SENDER") or SENDER).strip()

    try:
        client.connect()
        if not client.is_user_authorized():
            message = (
                "Telegram session is not authorized. Run login.py again and update "
                "the TG_SESSION_STRING secret."
            )
            log("group: session not authorized")
            bump_error(state, "group", message, notifier, force=True)
            return False

        try:
            entity = client.get_entity(group_name)
        except Exception as exc:
            message = (
                f"Cannot resolve @{group_name}. Check TG_GROUP and make sure the "
                "account is a member of the group."
            )
            log("group: entity lookup failed", error=type(exc).__name__)
            bump_error(state, "group", message, notifier, force=True)
            return False

        target_id: int | None = None
        try:
            target = client.get_entity(sender_name)
            target_id = int(target.id)
        except Exception:
            log("group: sender lookup failed; filtering by username only", sender=sender_name)

        initialized = bool(state.get("group_initialized"))
        last_id = int(state.get("group_last_id") or 0)
        fetch_limit = 100

        try:
            history = list(
                client.get_messages(entity, limit=fetch_limit, min_id=last_id) or []
            )
        except FloodWaitError as exc:
            message = f"Telegram flood wait: {exc.seconds}s. Will retry next run."
            log("group: flood wait", seconds=exc.seconds)
            bump_error(state, "group", message, notifier, force=False)
            return False
        except Exception as exc:
            message = (
                f"Cannot read @{group_name}: {type(exc).__name__}. Make sure the "
                "account has joined the group."
            )
            log("group: history fetch failed", error=type(exc).__name__)
            bump_error(state, "group", message, notifier, force=True)
            return False

        history.sort(key=lambda message: message.id)
        newest_id = max((message.id for message in history), default=last_id)

        if not initialized:
            fresh: list[GroupMessage] = []
            for message in history:
                if getattr(message, "action", None) is not None:
                    continue
                is_target, _, display, _, _ = sender_info(message, sender_name, target_id)
                if not is_target or message.id <= last_id:
                    continue
                text = message.message or ""
                if not is_recent(getattr(message, "date", None), 6):
                    continue
                links = extract_message_links(message, text)
                fresh.append(
                    GroupMessage(
                        message_id=message.id,
                        url=f"https://t.me/{group_handle}/{message.id}",
                        text=text,
                        links=links,
                        date=getattr(message, "date", None),
                        author=display,
                        giveaway=is_giveaway(text, links),
                        source="target",
                    )
                )

            state["group_initialized"] = True

            notifier.send(
                "ServerHive group watch active",
                f"Watching @{group_name} for messages from @{sender_name}.\n"
                f"Latest message seen: #{newest_id}.\n"
                f"{'Found recent messages; alert follows.' if fresh else 'No recent messages from the target.'}\n\n"
                f"{GROUP_URL}",
                priority="low",
                tags=["white_check_mark"],
                click=GROUP_URL,
            )

            processed_until = newest_id
            failed = False
            for entry in fresh[:5]:
                if text_seen(state, entry.text):
                    continue
                title, body, priority, tags, click = group_notification(
                    [entry], has_target=True, has_third_party=False
                )
                if not notifier.send(
                    title,
                    body,
                    priority=priority,
                    tags=tags,
                    click=click,
                    actions=ntfy_actions(("Open message", click), ("Giveaway page", SITE_URL)),
                ):
                    processed_until = min(processed_until, max(entry.message_id - 1, 0))
                    failed = True
                    log("group: notification failed; will retry", message=entry.message_id)
                    break
                mark_text(state, entry.text)

            if not failed and len(fresh) > 5:
                # Process the remaining recent messages on the next run.
                processed_until = min(processed_until, fresh[4].message_id)

            state["group_last_id"] = processed_until
            log("group: baseline", last_id=processed_until, fresh=len(fresh))
            return True

        entries: list[GroupMessage] = []
        has_target = False
        has_third_party = False
        batch_hashes: set[str] = set()

        for message in history:
            if message.id <= last_id:
                continue
            if getattr(message, "action", None) is not None:
                continue
            text = message.message or ""
            links = extract_message_links(message, text)
            is_target, is_channel, display, username, _ = sender_info(
                message, sender_name, target_id
            )
            giveaway = is_giveaway(text, links)
            site_link = "giveaway.serverhive.in" in (text + " " + " ".join(links)).lower()

            source = None
            if is_target:
                source = "target"
            elif is_channel and not channel_ok and (text or links):
                source = "channel"
            elif site_link:
                source = "third-party"

            if source is None:
                continue

            key = text_key(text)
            if text and (text_seen(state, text) or key in batch_hashes):
                log("group: duplicate text skipped", message=message.id)
                continue

            entries.append(
                GroupMessage(
                    message_id=message.id,
                    url=f"https://t.me/{group_handle}/{message.id}",
                    text=text,
                    links=links,
                    date=getattr(message, "date", None),
                    author=display if source == "target" else (username or display),
                    giveaway=giveaway,
                    source=source,
                )
            )
            if text:
                batch_hashes.add(key)
            has_target = has_target or source == "target"
            has_third_party = has_third_party or source == "third-party"

        state["group_last_id"] = max(newest_id, last_id)

        if not entries:
            log("group: no new relevant messages", last_id=last_id, scanned=len(history))
            return True

        title, body, priority, tags, click = group_notification(
            entries, has_target=has_target, has_third_party=has_third_party
        )
        if not notifier.send(
            title,
            body,
            priority=priority,
            tags=tags,
            click=click,
            actions=ntfy_actions(("Open message", click), ("Giveaway page", SITE_URL)),
        ):
            state["group_last_id"] = last_id
            log("group: notification failed; will retry", last_id=last_id)
            return True

        for entry in entries:
            mark_text(state, entry.text)

        log(
            "group: scanned",
            scanned=len(history),
            relevant=len(entries),
            target=has_target,
        )
        return True
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_once(state: dict[str, Any], notifier: Notifier, state_path: Path, dry_run: bool) -> int:
    first_run = not state.get("initialized")
    results: dict[str, bool | None] = {"channel": None, "site": None, "group": None}

    try:
        results["channel"] = check_channel(state, notifier)
        reset_error(state, "channel")
    except Exception as exc:
        log("channel: error", error=type(exc).__name__)
        bump_error(state, "channel", f"{type(exc).__name__}: {exc}", notifier)
        results["channel"] = False

    try:
        results["site"] = check_site(state, notifier)
        reset_error(state, "site")
    except Exception as exc:
        log("site: error", error=type(exc).__name__)
        bump_error(state, "site", f"{type(exc).__name__}: {exc}", notifier)
        results["site"] = False

    try:
        results["group"] = check_group(
            state, notifier, channel_ok=results["channel"] is True
        )
        if results["group"] is not None:
            reset_error(state, "group")
    except Exception as exc:
        log("group: error", error=type(exc).__name__)
        bump_error(state, "group", f"{type(exc).__name__}: {exc}", notifier)
        results["group"] = False

    if first_run:
        site = state.get("site") if isinstance(state.get("site"), dict) else {}
        if results["group"] is True:
            group_state = "active"
        elif results["group"] is None:
            group_state = "waiting for Telegram secrets"
        else:
            group_state = "error (see alert)"
        notifier.send(
            "ServerHive watcher started",
            f"Watching @{CHANNEL}, @{GROUP} (messages from @{SENDER}) and {SITE_URL}.\n\n"
            f"Giveaway page: HTTP {site.get('status', '?')} "
            f"({'reachable' if site.get('reachable') else 'not reachable'}).\n"
            f"Channel baseline: post #{state.get('channel_last_id', '?')}.\n"
            f"Group: {group_state}.\n\n"
            "Alerts will arrive on this ntfy topic.",
            priority="low",
            tags=["white_check_mark"],
            click=SITE_URL,
        )

    state["initialized"] = True
    state["last_run"] = iso(utcnow())
    state["version"] = 1
    prune_state(state)

    if not dry_run:
        save_state(state_path, state)
    else:
        log("state: dry run, not saved")

    log(
        "run complete",
        channel=results["channel"],
        site=results["site"],
        group=results["group"],
    )

    if results["channel"] is False and results["site"] is False:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        default=os.getenv("STATE_FILE", ".state.json"),
        help="Path to the state JSON file (default: .state.json or $STATE_FILE)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print notifications instead of sending them; do not save state",
    )
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="Send one test notification and exit",
    )
    parser.add_argument(
        "--loop",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Run forever with the given interval (for a VPS/systemd)",
    )
    args = parser.parse_args(argv)

    dry_run = args.dry_run or os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")
    notifier = Notifier(
        os.getenv("NTFY_TOPIC", ""),
        os.getenv("NTFY_SERVER", "https://ntfy.sh"),
        dry_run=dry_run,
    )

    if not notifier.configured and not dry_run:
        log("config: NTFY_TOPIC is not set")
        return 2

    if args.test_notify:
        notifier.send(
            "ServerHive watcher test",
            "If you can read this, ServerHive watch alerts are wired up.",
            priority="low",
            tags=["white_check_mark"],
        )
        return 0

    state_path = Path(args.state).expanduser()
    state = load_state(state_path)

    if args.loop <= 0:
        return run_once(state, notifier, state_path, dry_run)

    log("loop: starting", interval=args.loop)
    try:
        while True:
            try:
                run_once(state, notifier, state_path, dry_run)
            except Exception as exc:
                log("loop: unexpected error", error=type(exc).__name__)
                bump_error(state, "watcher", f"{type(exc).__name__}: {exc}", notifier)
                if not dry_run:
                    save_state(state_path, state)
            time.sleep(max(15, args.loop))
    except KeyboardInterrupt:
        log("loop: stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
