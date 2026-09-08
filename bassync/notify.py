# 25Live -> BAS Schedule Sync — failure alerting
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Notification when a run fails (or, optionally, succeeds).

Two channels, either or both: an incoming **webhook** (Slack, Teams, or
anything that accepts a JSON `{"text": ...}` POST) and **SMTP email**.

`send_alert` never raises. An alerting problem must not change the run's
outcome — a BAS write that succeeded should not be reported as a failure
because the mail relay was down. Failures are logged and returned, so
`--test-alert` can show them without a real outage to trigger them.
"""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Optional

import requests

WEBHOOK_TIMEOUT = 15
SMTP_TIMEOUT = 20

# Environment variable holding the SMTP password. Read at send time so a
# rotated secret takes effect on the next run without a config change.
SMTP_PASSWORD_ENV = "BAS_SMTP_PASSWORD"

# Ports that conventionally mean implicit TLS (SMTPS). Used only to make the
# default sensible; `security:` always wins when set explicitly.
IMPLICIT_TLS_PORTS = {465}


class AlertResult:
    """Outcome of one notification channel."""

    __slots__ = ("channel", "ok", "detail")

    def __init__(self, channel: str, ok: bool, detail: str = ""):
        self.channel = channel
        self.ok = ok
        self.detail = detail

    def __str__(self) -> str:
        return f"[{'OK  ' if self.ok else 'FAIL'}] {self.channel} — {self.detail}"


def send_alert(alerts_cfg: dict, subject: str, body: str,
               force: bool = False) -> list:
    """
    Notify via whichever channels are configured. Returns a list of
    AlertResult — empty when alerting is switched off.

    `force` sends even when `alerts.enabled` is false, which is what
    `--test-alert` uses to prove the plumbing before turning it on.
    """
    results: list = []
    if not alerts_cfg:
        return results
    if not (alerts_cfg.get("enabled") or force):
        return results

    url = alerts_cfg.get("webhook_url")
    if url:
        results.append(_send_webhook(url, subject, body))

    email = alerts_cfg.get("email") or {}
    if email.get("enabled") or (force and email.get("smtp_host")):
        results.append(_send_email(email, subject, body))

    for result in results:
        if not result.ok:
            logging.warning("Alert channel %s failed: %s",
                            result.channel, result.detail)
    return results


def _send_webhook(url: str, subject: str, body: str) -> AlertResult:
    try:
        r = requests.post(url, json={"text": f"{subject}\n\n{body}"},
                          timeout=WEBHOOK_TIMEOUT)
    except requests.RequestException as exc:
        return AlertResult("webhook", False, str(exc))
    if r.status_code >= 400:
        return AlertResult("webhook", False,
                           f"HTTP {r.status_code} {r.text[:150]}")
    return AlertResult("webhook", True, f"HTTP {r.status_code}")


def _resolve_security(email_cfg: dict, port: int) -> str:
    """
    Which transport security to use: "starttls", "ssl", or "none".

    `security:` is the current key. `use_tls:` is the pre-2.0 boolean and is
    still honored. With neither, port 465 implies implicit TLS and everything
    else implies STARTTLS — the safe default, since a plaintext fallback would
    silently put a relay password on the wire.
    """
    explicit = (email_cfg.get("security") or "").strip().lower()
    if explicit in ("starttls", "ssl", "tls", "smtps", "none", "plain"):
        return {"tls": "ssl", "smtps": "ssl", "plain": "none"}.get(explicit, explicit)
    use_tls = email_cfg.get("use_tls")
    if use_tls is False:
        return "none"
    return "ssl" if port in IMPLICIT_TLS_PORTS else "starttls"


def _validate_email_cfg(email_cfg: dict) -> Optional[str]:
    """First missing required setting, as a message. None when it's usable."""
    if not (email_cfg.get("smtp_host") or "").strip():
        return "alerts.email.smtp_host is not set"
    if not (email_cfg.get("from_addr") or "").strip():
        return "alerts.email.from_addr is not set"
    recipients = email_cfg.get("to_addrs") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    if not [r for r in recipients if str(r).strip()]:
        return "alerts.email.to_addrs is empty"
    if (email_cfg.get("username") or "").strip() \
            and not os.environ.get(SMTP_PASSWORD_ENV):
        # Sending unauthenticated when a username was configured gets rejected
        # by most relays with an opaque 5xx. Say what is actually wrong.
        return (f"alerts.email.username is set but ${SMTP_PASSWORD_ENV} is not — "
                "set it, or clear the username for an open relay")
    return None


def build_message(email_cfg: dict, subject: str, body: str) -> EmailMessage:
    """The message itself, separated out so tests can inspect it offline."""
    recipients = email_cfg.get("to_addrs") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_cfg["from_addr"]
    msg["To"] = ", ".join(str(r).strip() for r in recipients if str(r).strip())
    # Explicit Date and Message-ID: some relays and spam filters treat mail
    # without them as suspect, and an alert that lands in quarantine is an
    # alert nobody sees.
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=email_cfg["from_addr"].split("@")[-1]
                                   or None)
    msg.set_content(body)
    return msg


def _send_email(email_cfg: dict, subject: str, body: str) -> AlertResult:
    problem = _validate_email_cfg(email_cfg)
    if problem:
        return AlertResult("email", False, problem)

    host = email_cfg["smtp_host"].strip()
    port = int(email_cfg.get("smtp_port", 587))
    security = _resolve_security(email_cfg, port)
    username = (email_cfg.get("username") or "").strip()
    password = os.environ.get(SMTP_PASSWORD_ENV)
    msg = build_message(email_cfg, subject, body)

    # Each stage is reported separately: "connection refused" and "auth
    # rejected" and "relay denied this recipient" need three different fixes,
    # and a single "email failed" tells the operator none of them.
    try:
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT,
                                    context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
    except (OSError, smtplib.SMTPException) as exc:
        return AlertResult("email", False,
                           f"could not connect to {host}:{port} ({security}): {exc}")

    try:
        with smtp:
            smtp.ehlo()
            if security == "starttls":
                try:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                except smtplib.SMTPException as exc:
                    return AlertResult(
                        "email", False,
                        f"STARTTLS failed on {host}:{port} ({exc}). If this "
                        "relay uses implicit TLS, set alerts.email.security: "
                        "ssl (usually port 465); if it is plaintext-only on a "
                        "trusted network, set security: none.")
            if username and password:
                try:
                    smtp.login(username, password)
                except smtplib.SMTPAuthenticationError as exc:
                    return AlertResult(
                        "email", False,
                        f"authentication rejected for {username}: {exc}")
            refused = smtp.send_message(msg)
    except smtplib.SMTPException as exc:
        return AlertResult("email", False, f"send failed: {exc}")
    except OSError as exc:
        return AlertResult("email", False, f"connection lost: {exc}")

    if refused:
        return AlertResult("email", False,
                           f"relay refused {len(refused)} recipient(s): "
                           f"{', '.join(sorted(refused))}")
    return AlertResult("email", True,
                       f"sent via {host}:{port} ({security}) to {msg['To']}")
