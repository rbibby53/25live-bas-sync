# 25Live -> BAS Schedule Sync — HTTP helpers
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""Shared HTTP behaviour for the REST-based clients and drivers."""

from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def mount_retries(session: requests.Session, retry: Optional[dict],
                  allowed_methods) -> None:
    """
    Mount a urllib3 Retry adapter so transient errors (connection failures,
    timeouts, 429/5xx) are retried with exponential backoff.

    `allowed_methods` limits which verbs auto-retry. Callers pass only safe or
    idempotent ones — a replayed POST would create a duplicate schedule entry,
    which on a BAS means a room that never stops conditioning.
    """
    if not retry:
        return
    attempts = int(retry.get("attempts", 3))
    policy = Retry(
        total=attempts,
        connect=attempts,
        read=attempts,
        status=attempts,
        backoff_factor=float(retry.get("backoff_seconds", 2.0)),
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(allowed_methods),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=policy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
