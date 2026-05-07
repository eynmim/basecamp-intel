"""Read-only Telegram diagnostic.

Calls getMe, getChat, and getChatMember(bot) against the configured
TELEGRAM_BOT_TOKEN + TELEGRAM_CHANNEL_ID, prints a verdict, and exits
nonzero if anything is wrong. Posts nothing.

Run via the "Diagnose Telegram setup" workflow_dispatch — never on push.
"""

from __future__ import annotations

import os
import sys

import requests

API = "https://api.telegram.org"


def fail(msg: str) -> None:
    print(f"\n::error::{msg}")
    sys.exit(1)


def warn(msg: str) -> None:
    print(f"\n::warning::{msg}")


def call(token: str, method: str, payload: dict | None = None) -> dict:
    r = requests.post(f"{API}/bot{token}/{method}", json=payload or {}, timeout=15)
    try:
        return r.json()
    except ValueError:
        fail(f"{method}: HTTP {r.status_code}, non-JSON body: {r.text[:200]}")
        return {}


def main() -> None:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHANNEL_ID") or "").strip()

    print("=" * 60)
    print("Telegram setup diagnostic")
    print("=" * 60)

    if not token:
        fail("TELEGRAM_BOT_TOKEN secret is empty.")
    if not chat_id:
        fail("TELEGRAM_CHANNEL_ID secret is empty.")

    # The token format is `<bot_id>:<random>`. Showing only the prefix is safe.
    token_prefix = token.split(":", 1)[0]
    print(f"Token bot id:   {token_prefix}")
    print(f"chat_id under test: {chat_id!r}  (len={len(chat_id)})")

    # ---- 1. getMe: is the token valid? -----------------------------------
    print("\n[1/3] getMe — does the token work?")
    r = call(token, "getMe")
    if not r.get("ok"):
        fail(f"getMe failed: {r.get('description')}\n"
             f"  → Token is invalid or revoked. Generate a new one via @BotFather and "
             f"update TELEGRAM_BOT_TOKEN.")
    me = r["result"]
    print(f"  ✅ Token works. Bot is @{me.get('username')} "
          f"(id={me.get('id')}, name={me.get('first_name')!r}).")
    bot_id = me["id"]

    # ---- 2. getChat: can the bot reach the chat? -------------------------
    print("\n[2/3] getChat — can the bot see this chat?")
    r = call(token, "getChat", {"chat_id": chat_id})
    if not r.get("ok"):
        desc = r.get("description", "<no description>")
        print(f"  ❌ getChat failed: {desc}")
        if "chat not found" in desc.lower():
            fail(
                "Telegram says it can't find this chat. Three possible causes:\n"
                "  1. TELEGRAM_CHANNEL_ID is wrong. Expected exactly: -1003957539319\n"
                "     If yours is different, re-paste it (Settings → Secrets → Update).\n"
                "  2. The bot is not in the supergroup. Add it as a member.\n"
                "  3. The supergroup ID changed (rare; happens if you re-created it)."
            )
        fail(f"getChat unexpected error: {desc}")
    chat = r["result"]
    print(f"  ✅ Chat is reachable.")
    print(f"     id:        {chat.get('id')}")
    print(f"     title:     {chat.get('title')!r}")
    print(f"     type:      {chat.get('type')}  "
          f"(should be 'supergroup' for topic routing)")
    print(f"     is_forum:  {chat.get('is_forum')}  "
          f"(should be True for topics to work)")

    if chat.get("type") != "supergroup":
        warn(f"Chat type is '{chat.get('type')}', not 'supergroup'. "
             f"Topic routing only works in supergroups with Topics enabled.")
    if not chat.get("is_forum"):
        warn("is_forum is False. Topics aren't enabled on this supergroup. "
             "Open group settings → toggle Topics ON.")

    # ---- 3. getChatMember(bot): is the bot admin? ------------------------
    print("\n[3/3] getChatMember — is the bot admin with the right rights?")
    r = call(token, "getChatMember", {"chat_id": chat_id, "user_id": bot_id})
    if not r.get("ok"):
        fail(f"getChatMember failed: {r.get('description')}\n"
             f"  → The bot may not be in the chat at all.")
    member = r["result"]
    status = member.get("status")
    print(f"  Status: {status}")

    if status in ("left", "kicked"):
        fail(
            "Bot is NOT in the supergroup (status='{status}').\n"
            "  → Add @{username} back to the supergroup as a member, "
            "then promote to admin.".format(status=status, username=me.get("username"))
        )
    if status == "member":
        fail(
            "Bot is in the supergroup but as a regular member, NOT admin.\n"
            "  Telegram refuses sendMessage from non-admin bots in topic-enabled\n"
            "  supergroups. Fix:\n"
            "    Group → tap bot → ⋯ → Promote to Admin →\n"
            "    enable: Send Messages, Pin Messages, Manage Topics → Save.\n"
            "  After saving you should see a system message: "
            "\"Ali promoted @{u} to administrator\".".format(u=me.get("username"))
        )
    if status == "restricted":
        fail("Bot is restricted in this supergroup. Group settings → unrestrict the bot.")

    # status == 'administrator' or 'creator'
    print(f"  ✅ Bot has admin status.")
    rights = {
        "can_post_messages":  member.get("can_post_messages",  None),
        "can_send_messages":  member.get("can_send_messages",  None),
        "can_pin_messages":   member.get("can_pin_messages",   None),
        "can_manage_topics":  member.get("can_manage_topics",  None),
        "can_edit_messages":  member.get("can_edit_messages",  None),
        "can_delete_messages": member.get("can_delete_messages", None),
    }
    for name, val in rights.items():
        mark = "✅" if val else ("➖" if val is None else "❌")
        print(f"     {mark} {name}: {val}")

    must_have_any_send = any(member.get(k) for k in ("can_post_messages", "can_send_messages"))
    if not must_have_any_send:
        fail("Bot is admin but neither can_post_messages nor can_send_messages is True.\n"
             "  → Re-edit admin rights and toggle 'Send Messages' ON.")
    if not member.get("can_pin_messages"):
        warn("Bot can't pin messages. The deadline-board pin will fail (best-effort, "
             "won't break delivery). Toggle 'Pin Messages' ON for full UX.")
    if not member.get("can_manage_topics"):
        warn("Bot can't manage topics. Posting INTO topics still works; only "
             "creating/closing topics from the bot would need this.")

    print("\n" + "=" * 60)
    print("✅ All clear. Bot can post into this chat.")
    print("=" * 60)


if __name__ == "__main__":
    main()
