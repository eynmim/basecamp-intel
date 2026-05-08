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

    # ---- 2a. Compare the secret against the expected value char-by-char.
    expected = "-1003957539319"
    print(f"\n[2a/3] Char-by-char check vs expected {expected!r} (len={len(expected)})")
    print(f"  is_ascii(secret): {chat_id.isascii()}")
    if len(chat_id) != len(expected):
        print(f"  ❌ Length mismatch: secret has {len(chat_id)}, expected {len(expected)}.")
    else:
        diffs = [(i, c, e) for i, (c, e) in enumerate(zip(chat_id, expected)) if c != e]
        if not diffs:
            print(f"  ✅ Secret matches expected value byte-for-byte.")
        else:
            print(f"  ❌ {len(diffs)} character(s) differ from expected:")
            for i, c, e in diffs:
                print(
                    f"     pos {i}: secret has U+{ord(c):04X} ({c!r}), "
                    f"expected U+{ord(e):04X} ({e!r})"
                )
            print(
                "  → Some character in the secret is not what it appears to be "
                "(e.g. typographic minus '−' U+2212 vs hyphen-minus '-' U+002D)."
            )

    # ---- 2b. getChat with the secret value --------------------------------
    print("\n[2b/3] getChat with chat_id from secret")
    r_secret = call(token, "getChat", {"chat_id": chat_id})
    print(f"  secret -> ok={r_secret.get('ok')}, "
          f"description={r_secret.get('description')!r}")

    # ---- 2c. getChat with the hardcoded expected value -------------------
    print(f"\n[2c/3] getChat with HARDCODED chat_id {expected!r}")
    r_hard = call(token, "getChat", {"chat_id": expected})
    print(f"  hardcoded -> ok={r_hard.get('ok')}, "
          f"description={r_hard.get('description')!r}")

    # ---- 2d. Discovery via getUpdates: dump every chat the bot has seen.
    # If our derived chat_id is wrong, the real one will appear here.
    print("\n[2d/3] getUpdates — what chats has the bot actually seen?")
    print("       (For the freshest data: send any message in the supergroup")
    print("        BEFORE running this diagnostic.)")
    # deleteWebhook first so getUpdates returns data; we don't have a webhook
    # because we don't have a webhook configured anyway.
    call(token, "deleteWebhook", {"drop_pending_updates": False})
    r = call(token, "getUpdates", {
        "timeout": 0,
        "limit": 100,
        "allowed_updates": [
            "message", "edited_message", "channel_post",
            "my_chat_member", "chat_member",
        ],
    })
    seen: dict[int, tuple[str, str]] = {}
    if r.get("ok"):
        for upd in r.get("result", []):
            for key in ("message", "edited_message", "channel_post",
                        "my_chat_member", "chat_member"):
                obj = upd.get(key)
                if obj and "chat" in obj:
                    chat_obj = obj["chat"]
                    cid = chat_obj.get("id")
                    if cid is not None:
                        seen[cid] = (
                            str(chat_obj.get("title", "<private>")),
                            str(chat_obj.get("type", "?")),
                        )
    configured_int = None
    try:
        configured_int = int(chat_id)
    except ValueError:
        pass

    if not seen:
        print(
            "  No chats seen in recent updates.\n"
            "  → Send a message in any topic of the BaseCamp supergroup,\n"
            "    then re-run this diagnostic. The chat_id will appear here."
        )
    else:
        print(f"  Bot has interacted with these {len(seen)} chat(s):")
        for cid, (title, ctype) in seen.items():
            mark = "  ← matches your secret" if cid == configured_int else ""
            print(f"    chat_id={cid:<20} type={ctype:<11} title={title!r}{mark}")

    # ---- Verdict on chat reachability ------------------------------------
    if not r_secret.get("ok"):
        # Build the most useful failure message we can.
        msg_lines: list[str] = []
        msg_lines.append("Telegram refuses the configured chat_id.")
        if r_hard.get("ok"):
            msg_lines.append(
                "  → Hardcoded -1003957539319 worked but your secret didn't.\n"
                "    TELEGRAM_CHANNEL_ID has invisible bad characters.\n"
                "    Delete and re-create by TYPING the value on a desktop keyboard."
            )
        else:
            # Both fail. Use discovery if available.
            group_chats = [
                (cid, title) for cid, (title, ctype) in seen.items()
                if cid != configured_int and "group" in (ctype or "").lower()
            ]
            if group_chats:
                msg_lines.append("  → The real chat_id is one of these (from getUpdates):")
                for cid, title in group_chats:
                    msg_lines.append(f"      {cid}   title={title!r}")
                msg_lines.append(
                    "    Update TELEGRAM_CHANNEL_ID to the one whose title is BaseCamp."
                )
            else:
                msg_lines.append(
                    "  → Bot has not seen any group chat in recent updates.\n"
                    "    Either the bot was kicked, or it's in a different group than\n"
                    "    you think. Open BaseCamp → send a message in any topic →\n"
                    "    re-run this diagnostic. The real chat_id will appear in [2d/3]."
                )
        fail("\n".join(msg_lines))

    # If we get here, getChat with the secret value succeeded.
    chat = r_secret["result"]
    print(f"\n  ✅ Chat is reachable via the secret.")
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
