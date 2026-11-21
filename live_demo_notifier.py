"""Live End-to-End Demo & Notification Dispatcher (Telegram Bot + Gmail SMTP + Console).

Runs an end-to-end simulation across Epics A, B, C, and D using the Shared Foundation
`TransactionalOutbox` and dispatches every generated notification (Restock Approval
Request, Approved Supplier Purchase Order, Daily Branch Financial Report, and POS
Offer Activation Webhook) to:
1. Console Live Inbox Preview (always active)
2. Telegram Bot Chat (when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set)
3. Email via Gmail SMTP (when `SMTP_USER` and `SMTP_APP_PASSWORD` are set)
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.message import EmailMessage
import json
import os
import smtplib
import urllib.request
from typing import List, Optional

from foundation.failure_test_harness import DuplicateSchedulerAndStaleDataHarness
from foundation.langgraph_runtime import OutboxMessage


def format_outbox_message_text(msg: OutboxMessage) -> str:
    """Formats an OutboxMessage into a clean Telegram/Email notification card."""
    icon = "EMAIL" if msg.channel == "EMAIL" else "POS_WEBHOOK"
    subject_line = msg.subject or f"{msg.channel} Notification"
    pretty_payload = json.dumps(msg.payload, indent=2, ensure_ascii=False, default=str)
    lines = [
        "=" * 64,
        f"[{icon}] Message #{msg.message_id} | Branch {msg.branch_id}",
        f"To: {msg.recipient}",
        f"Subject: {subject_line}",
        f"Idempotency Key: {msg.idempotency_key}",
        "-" * 64,
        pretty_payload,
        "=" * 64,
    ]
    return "\n".join(lines)


def send_via_telegram(text: str, bot_token: str, chat_id: str) -> bool:
    """Sends a formatted message to a Telegram chat or group via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{bot_token.strip()}/sendMessage"
    data = json.dumps({"chat_id": chat_id.strip(), "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status == 200


def send_via_gmail_smtp(
    msg: OutboxMessage,
    body_text: str,
    smtp_user: str,
    smtp_app_password: str,
    override_to: Optional[str] = None,
) -> None:
    """Sends an OutboxMessage through Gmail SMTP SSL (port 465)."""
    email_msg = EmailMessage()
    email_msg["From"] = smtp_user.strip()
    email_msg["To"] = (override_to or smtp_user).strip()
    email_msg["Subject"] = (
        f"[DEPI Retail AI - Branch {msg.branch_id}] "
        f"{msg.subject or msg.channel} (Original To: {msg.recipient})"
    )
    email_msg.set_content(body_text)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
        server.login(smtp_user.strip(), smtp_app_password.strip())
        server.send_message(email_msg)


def run_live_demo(
    *,
    telegram_token: Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
    smtp_user: Optional[str] = None,
    smtp_password: Optional[str] = None,
    smtp_to: Optional[str] = None,
) -> List[OutboxMessage]:
    """Executes all workflows (Restocking, Reporting, Offers) and dispatches live messages."""
    harness = DuplicateSchedulerAndStaleDataHarness()
    eval_time = datetime(2026, 11, 14, 0, 5, 0, tzinfo=timezone.utc)
    fresh_sync = datetime(2026, 11, 14, 0, 0, 0, tzinfo=timezone.utc)
    pos_sync = datetime(2026, 11, 13, 23, 0, 0, tzinfo=timezone.utc)

    # 1. Trigger Restocking Workflow (Branch 1 - Maadi) -> Approved & Supplier Email Dispatched
    harness.trigger_scheduled_restock_run(
        branch_id=1,
        schedule_date="2026-11-14",
        snapshot_synced_at=fresh_sync,
        evaluation_time=eval_time,
        auto_approve_by="stock.maadi@supermarket.eg",
    )

    # 2. Trigger Daily Branch Report Workflow (Branch 1 - Maadi) -> Report Email Dispatched
    harness.trigger_scheduled_daily_report(
        branch_id=1,
        report_date="2026-11-13",
        latest_pos_sync_utc=pos_sync,
        gross_sales="18750.00",
        net_revenue="18200.00",
    )

    # 3. Trigger Promotional Offer Approval & POS Activation (Branch 1 - Maadi)
    harness.trigger_scheduled_offer_activation(
        branch_id=1,
        offer_id=501,
        product_id=1,
        campaign_date="2026-11-14",
        approved_version=1,
        current_version=1,
        approved_offer_price="24.50",
        approved_cost_price="20.23",
        live_cost_price="20.23",
        days_until_expiry=4,
    )

    all_messages = harness.dispatched_emails + harness.dispatched_webhooks
    print(f"\n[LIVE DEMO] Generated {len(all_messages)} outbound messages across Epics A-D:\n")

    for msg in all_messages:
        card = format_outbox_message_text(msg)
        print(card)

        if telegram_token and telegram_chat_id:
            sent_tg = send_via_telegram(card, telegram_token, telegram_chat_id)
            print(f"  -> [Telegram] Delivered to chat_id={telegram_chat_id}: {sent_tg}")

        if smtp_user and smtp_password:
            send_via_gmail_smtp(msg, card, smtp_user, smtp_password, override_to=smtp_to)
            print(f"  -> [Gmail SMTP] Delivered to {smtp_to or smtp_user}")

    return all_messages


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run DEPI Retail AI Agents Live Notification Demo.")
    parser.add_argument("--telegram-token", default=os.environ.get("TELEGRAM_BOT_TOKEN"))
    parser.add_argument("--telegram-chat-id", default=os.environ.get("TELEGRAM_CHAT_ID"))
    parser.add_argument("--smtp-user", default=os.environ.get("SMTP_USER"))
    parser.add_argument("--smtp-password", default=os.environ.get("SMTP_APP_PASSWORD"))
    parser.add_argument("--smtp-to", default=os.environ.get("SMTP_TO"))
    args = parser.parse_args(argv)

    run_live_demo(
        telegram_token=args.telegram_token,
        telegram_chat_id=args.telegram_chat_id,
        smtp_user=args.smtp_user,
        smtp_password=args.smtp_password,
        smtp_to=args.smtp_to,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
