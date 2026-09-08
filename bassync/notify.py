# 25Live -> BAS Schedule Sync — failure alerting
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Best-effort notification when a run fails.

Never raises: an alerting problem must not change the run's outcome, and must
not turn a recoverable BAS write failure into a crash.
"""

import logging
import os
import smtplib
from email.message import EmailMessage

import requests

WEBHOOK_TIMEOUT = 15
SMTP_TIMEOUT = 20


def send_alert(alerts_cfg: dict, subject: str, body: str) -> None:
    """Notify via webhook (Slack/Teams/generic) and/or email, if configured."""
    if not alerts_cfg or not alerts_cfg.get("enabled"):
        return

    url = alerts_cfg.get("webhook_url")
    if url:
        try:
            requests.post(url, json={"text": f"{subject}\n\n{body}"},
                          timeout=WEBHOOK_TIMEOUT)
        except requests.RequestException as exc:
            logging.warning("Alert webhook failed: %s", exc)

    email = alerts_cfg.get("email") or {}
    if email.get("enabled"):
        try:
            _send_email(email, subject, body)
        except Exception as exc:                          # noqa: BLE001
            logging.warning("Alert email failed: %s", exc)


def _send_email(email_cfg: dict, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_cfg.get("from_addr", "")
    msg["To"] = ", ".join(email_cfg.get("to_addrs", []))
    msg.set_content(body)
    with smtplib.SMTP(email_cfg.get("smtp_host"),
                      email_cfg.get("smtp_port", 587),
                      timeout=SMTP_TIMEOUT) as smtp:
        if email_cfg.get("use_tls", True):
            smtp.starttls()
        user = email_cfg.get("username")
        password = os.environ.get("BAS_SMTP_PASSWORD")
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)
