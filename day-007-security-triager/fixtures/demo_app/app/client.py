"""Outbound HTTP helpers."""

import requests


def fetch_partner_report(url):
    # TLS verification disabled for a call that carries the partner API credential.
    return requests.get(url, headers={"Authorization": "Bearer partner"}, verify=False, timeout=10)
