"""Notification channels. Each is optional and opt-in via env vars.

Channels:
  * Telegram Bot (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
  * Server 酱 (SCTKEY_TOKEN) — WeChat push
  * DingTalk webhook (DINGTALK_WEBHOOK)
  * SMTP email (SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_TO)
    可选: SMTP_PORT (默认 465), SMTP_STARTTLS (设为 "1" 使用 587 端口模式)

Nothing is sent unless the relevant env var is set. The public send()
function tries every configured channel and returns per-channel results.
"""
from __future__ import annotations
import os
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Any

from ._common import log


# ── Markdown → HTML（轻量转换，无需第三方库）────────────────────────────────
def _md_to_html(md: str) -> str:
    """把日报 Markdown 转成可在邮件客户端直接阅读的 HTML。"""
    lines = md.split("\n")
    html_lines: list[str] = []
    in_table = False
    in_code = False

    for line in lines:
        # 代码块
        if line.strip().startswith("```"):
            if in_code:
                html_lines.append("</pre>")
                in_code = False
            else:
                html_lines.append('<pre style="background:#f4f4f4;padding:8px;border-radius:4px;font-size:13px;">')
                in_code = True
            continue
        if in_code:
            html_lines.append(line.replace("&", "&amp;").replace("<", "&lt;"))
            continue

        # 表格行
        if line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.match(r"^[-: ]+$", c) for c in cells if c):
                # 分隔行：把上一行变成 <thead>
                if html_lines and html_lines[-1].startswith("<tr>"):
                    last = html_lines.pop()
                    header_cells = re.findall(r"<td>(.*?)</td>", last)
                    html_lines.append(
                        "<thead><tr>"
                        + "".join(
                            f'<th style="background:#2c5282;color:#fff;padding:6px 12px;'
                            f'text-align:left;">{c}</th>'
                            for c in header_cells
                        )
                        + "</tr></thead><tbody>"
                    )
                in_table = True
                continue
            if not in_table:
                html_lines.append(
                    '<table style="border-collapse:collapse;width:100%;margin:8px 0;">'
                )
                in_table = True
            style = "padding:6px 12px;border-bottom:1px solid #e2e8f0;"
            html_lines.append(
                "<tr>"
                + "".join(f'<td style="{style}">{c}</td>' for c in cells if c != "")
                + "</tr>"
            )
            continue
        else:
            if in_table:
                html_lines.append("</tbody></table>")
                in_table = False

        # 标题
        if line.startswith("# "):
            html_lines.append(
                f'<h1 style="color:#1a365d;border-bottom:2px solid #2c5282;'
                f'padding-bottom:8px;">{line[2:]}</h1>'
            )
        elif line.startswith("## "):
            html_lines.append(
                f'<h2 style="color:#2c5282;margin-top:20px;">{line[3:]}</h2>'
            )
        elif line.startswith("### "):
            html_lines.append(f'<h3 style="color:#2d3748;">{line[4:]}</h3>')
        elif line.startswith("- "):
            content = line[2:]
            # 行内加粗
            content = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", content)
            html_lines.append(f'<li style="margin:3px 0;">{content}</li>')
        elif line.startswith("*") and line.endswith("*") and len(line) > 2:
            html_lines.append(f'<p style="color:#718096;font-size:12px;">'
                              f'{line.strip("*")}</p>')
        elif line.strip() == "---":
            html_lines.append('<hr style="border:none;border-top:1px solid #e2e8f0;margin:16px 0;">')
        elif line.strip() == "":
            html_lines.append("<br>")
        else:
            content = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", line)
            html_lines.append(f"<p style='margin:4px 0;'>{content}</p>")

    if in_table:
        html_lines.append("</tbody></table>")

    body = "\n".join(html_lines)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         font-size: 14px; color: #2d3748; max-width: 720px;
         margin: 0 auto; padding: 24px; }}
  li {{ list-style: none; }}
</style>
</head>
<body>{body}</body></html>"""


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


def _email_send(text: str, subject: str = "股票提醒",
                html: str | None = None) -> dict[str, Any]:
    host     = os.environ.get("SMTP_HOST")
    user     = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to       = os.environ.get("SMTP_TO")
    if not (host and user and password and to):
        return {"enabled": False}
    port      = int(os.environ.get("SMTP_PORT", "465"))
    starttls  = os.environ.get("SMTP_STARTTLS", "0") == "1"

    try:
        if html:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(text, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html",  "utf-8"))
        else:
            msg = MIMEText(text, "plain", "utf-8")

        msg["Subject"] = subject
        msg["From"]    = user
        msg["To"]      = to

        if starttls:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls()
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP_SSL(host, port, timeout=15) as s:
                s.login(user, password)
                s.send_message(msg)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send(text: str, *, title: str = "股票提醒",
         html: str | None = None) -> dict[str, Any]:
    """Try every configured channel. Returns per-channel results.

    html: optional HTML body for email (auto-generated from text if omitted).
    """
    if html is None and os.environ.get("SMTP_HOST"):
        html = _md_to_html(text)

    results = {
        "telegram":   _telegram_send(text),
        "serverchan": _serverchan_send(text, title),
        "dingtalk":   _dingtalk_send(text),
        "email":      _email_send(text, title, html=html),
    }
    active = [k for k, v in results.items()
              if v.get("enabled", True) and v.get("ok") is True]
    log.info("Notification sent via: %s", active or "none")
    return results


if __name__ == "__main__":
    print(send("test message"))
