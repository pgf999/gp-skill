"""Notification channels. Each is optional and opt-in via env vars.

Channels:
  * Telegram Bot (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
  * Server 酱 (SCTKEY_TOKEN) — WeChat push
  * DingTalk webhook (DINGTALK_WEBHOOK)
  * SMTP email (SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_TO)

Nothing is sent unless the relevant env var is set. The public send()
function tries every configured channel and returns per-channel results.
"""
from __future__ import annotations
import os
import smtplib
from email.mime.text import MIMEText
from typing import Any

from ._common import log


def _telegram_send(text: str) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return {"enabled": False}
    try:
        import requests
    except ImportError:
        return {"ok": False, "error": "requests not installed"}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        return {"ok": r.ok, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _serverchan_send(text: str, title: str = "股票复盘") -> dict[str, Any]:
    key = os.environ.get("SCTKEY_TOKEN")
    if not key:
        return {"enabled": False}
    try:
        import requests
    except ImportError:
        return {"ok": False, "error": "requests not installed"}
    try:
        r = requests.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": title, "desp": text},
            timeout=10,
        )
        return {"ok": r.ok, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _dingtalk_send(text: str) -> dict[str, Any]:
    webhook = os.environ.get("DINGTALK_WEBHOOK")
    if not webhook:
        return {"enabled": False}
    try:
        import requests
    except ImportError:
        return {"ok": False, "error": "requests not installed"}
    try:
        r = requests.post(
            webhook,
            json={"msgtype": "markdown",
                  "markdown": {"title": "股票提醒", "text": text}},
            timeout=10,
        )
        return {"ok": r.ok, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _email_send(text: str, subject: str = "股票提醒") -> dict[str, Any]:
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to = os.environ.get("SMTP_TO")
    if not (host and user and password and to):
        return {"enabled": False}
    port = int(os.environ.get("SMTP_PORT", "465"))
    try:
        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to
        with smtplib.SMTP_SSL(host, port) as s:
            s.login(user, password)
            s.send_message(msg)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send(text: str, *, title: str = "股票提醒") -> dict[str, Any]:
    """Try every configured channel. Returns per-channel results."""
    results = {
        "telegram": _telegram_send(text),
        "serverchan": _serverchan_send(text, title),
        "dingtalk": _dingtalk_send(text),
        "email": _email_send(text, title),
    }
    active = [k for k, v in results.items()
              if v.get("enabled", True) and v.get("ok") is True]
    log.info("Notification sent via: %s", active or "none")
    return results


if __name__ == "__main__":
    print(send("test message"))
