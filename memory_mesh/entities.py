"""Deterministic extraction from synthetic reports; no external data ingestion."""

import ipaddress
import re


def extract_entities(description):
    """Explicit identities take precedence; use demo signals only without them."""
    text = description.lower()
    entities = set()
    for kind, value in re.findall(r"\b(account|device|ip):([a-z0-9_.:@/-]+)", text):
        value = value.rstrip(".,;")
        if kind == "ip":
            try:
                value = str(ipaddress.ip_address(value))
            except ValueError:
                continue
        entities.add((kind, value))
    for value in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
        try:
            entities.add(("ip", str(ipaddress.ip_address(value))))
        except ValueError:
            pass
    if not entities:
        signals = {
            "privileged_account": ("admin", "root", "privileged"),
            "failed_login": ("login failures", "failed login", "credential stuffing", "brute force"),
            "shared_device": ("shared device", "same device"),
            "exposed_token": ("token",),
            "suspicious_ip": ("suspicious ip",),
        }
        for signal, phrases in signals.items():
            if any(phrase in text for phrase in phrases):
                entities.add(("signal", signal))
    return [{"kind": kind, "value": value} for kind, value in sorted(entities)]
