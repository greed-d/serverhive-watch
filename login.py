#!/usr/bin/env python3
"""Create a Telethon StringSession for the ServerHive watcher.

Run this locally, once. It prints a session string that you store as the
TG_SESSION_STRING GitHub secret. The string grants full access to the account,
so never commit it, paste it into a public issue, or share it.
"""

from __future__ import annotations

import os
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = (os.getenv("TG_API_ID") or "").strip()
API_HASH = (os.getenv("TG_API_HASH") or "").strip()


def ask(prompt: str, current: str) -> str:
    if current:
        return current
    try:
        return input(prompt).strip()
    except EOFError:
        print("\nNo input available. Set TG_API_ID and TG_API_HASH or run interactively.")
        raise SystemExit(2)


def main() -> int:
    print("Telegram API credentials come from https://my.telegram.org/apps\n")
    api_id = ask("api_id: ", API_ID)
    api_hash = ask("api_hash: ", API_HASH)

    if not api_id.isdigit():
        print("api_id must be numeric.")
        return 2
    if not api_hash:
        print("api_hash is required.")
        return 2

    print(
        "\nTelethon will now ask for your phone number (international format), "
        "the login code, and your 2FA password if enabled.\n"
    )

    try:
        with TelegramClient(
            StringSession(),
            int(api_id),
            api_hash,
            device_model="ServerHive Watch",
            system_version="1.0",
            app_version="1.0",
            lang_code="en",
            system_lang_code="en",
        ) as client:
            me = client.get_me()
            username = f"@{me.username}" if getattr(me, "username", None) else "(no username)"
            print(f"\nLogged in as {me.first_name} {username} (id {me.id}).\n")
            print("Store this as the TG_SESSION_STRING GitHub secret:\n")
            print(client.session.save())
            print(
                "\nNever commit this string. To revoke it later, use Telegram "
                "Settings -> Devices -> Terminate all sessions, then run this again."
            )
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception as exc:
        print(f"\nLogin failed: {type(exc).__name__}: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
