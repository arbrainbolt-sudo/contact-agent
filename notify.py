"""Sends a message when an agent run finishes. Silent no-op if not configured."""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Twilio WhatsApp (optional — see notes at the bottom of the chat)
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()   # e.g. whatsapp:+14155238886
TWILIO_TO = os.getenv("TWILIO_WHATSAPP_TO", "").strip()       # e.g. whatsapp:+19175551234


def status_line():
    channels = []
    if TELEGRAM_TOKEN and TELEGRAM_CHAT:
        channels.append("Telegram")
    if TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM and TWILIO_TO:
        channels.append("WhatsApp")
    return ", ".join(channels) if channels else "off"


def _telegram(text, log):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if r.status_code == 200:
            log("   telegram sent")
        else:
            # Telegram explains the problem clearly in the body — show it.
            log(f"   telegram failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log(f"   telegram error: {e}")


def _whatsapp(text, log):
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_TOKEN),
            data={"From": TWILIO_FROM, "To": TWILIO_TO, "Body": text},
            timeout=20,
        )
        if r.status_code in (200, 201):
            log("   whatsapp sent")
        else:
            log(f"   whatsapp failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log(f"   whatsapp error: {e}")


def send(text, log=print):
    """Send to every configured channel. Never raises."""
    if TELEGRAM_TOKEN and TELEGRAM_CHAT:
        _telegram(text, log)
    if TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM and TWILIO_TO:
        _whatsapp(text.replace("<b>", "*").replace("</b>", "*"), log)


def run_finished(target, country, findings, error=None):
    """Build and send the 'run complete' message."""
    if error:
        return send(f"❌ <b>Contact Finder failed</b>\n{target}\n\n{error}")

    where = f" · {country}" if country else ""
    lines = [f"✅ <b>Contact Finder done</b>",
             f"{target}{where}",
             f"\n<b>{len(findings)}</b> new contact(s) found"]

    for p in findings[:5]:
        detail = p.get("email") or p.get("phone") or p.get("linkedin") or ""
        lines.append(f"• {p.get('name', '?')} — {p.get('role', '')} {detail}".strip())

    if len(findings) > 5:
        lines.append(f"…and {len(findings) - 5} more")

    lines.append("\nhttp://localhost:5000")
    send("\n".join(lines))