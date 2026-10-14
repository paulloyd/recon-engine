"""Propose a chart-of-accounts category for card charges that have no ledger entry yet.

Rules first (cheap, deterministic, auditable). An optional LLM pass is available for rows the
rules are unsure about; it is validated against the allowed category list and can never
auto-post - every proposal still goes through the human approval queue.
"""
import json
import os
import urllib.request

from .matcher import normalize_vendor

CATEGORIES = ["Inventory Purchases", "Shipping & Postage", "Advertising", "Office Supplies",
              "Packaging Supplies", "Travel & Meals", "Software & Subscriptions", "Uncategorized"]

# (required tokens, category, confidence)
RULES = [
    ({"UPS"}, "Shipping & Postage", 0.95), ({"FEDEX"}, "Shipping & Postage", 0.95),
    ({"USPS"}, "Shipping & Postage", 0.95), ({"META"}, "Advertising", 0.95),
    ({"GOOGLE", "ADS"}, "Advertising", 0.95), ({"GOOGLE", "WORKSPACE"}, "Software & Subscriptions", 0.95),
    ({"QUICKBOOKS"}, "Software & Subscriptions", 0.95), ({"CANVA"}, "Software & Subscriptions", 0.9),
    ({"ZOOM"}, "Software & Subscriptions", 0.9), ({"STAPLES"}, "Office Supplies", 0.9),
    ({"ULINE"}, "Packaging Supplies", 0.9), ({"UBER"}, "Travel & Meals", 0.9),
    ({"AMAZON"}, "Inventory Purchases", 0.8), ({"ALIBABA"}, "Inventory Purchases", 0.9),
    ({"SUPPLY"}, "Packaging Supplies", 0.55),
]


def rule_categorize(description: str):
    toks = set(normalize_vendor(description).split())
    for need, cat, conf in RULES:
        if need <= toks:
            return cat, conf, "rule"
    return "Uncategorized", 0.3, "rule"


def llm_categorize(description: str, amount: float):
    """Optional. Needs ANTHROPIC_API_KEY. Returns (category, confidence, 'llm') or None on any problem."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = (f"Card charge: '{description}', amount {amount:.2f} USD, for an online merchandise reseller.\n"
              f"Pick exactly one category from {CATEGORIES}. Reply ONLY as JSON: "
              '{"category": "...", "confidence": 0.0-1.0}')
    body = json.dumps({"model": os.environ.get("RECON_LLM_MODEL", "claude-sonnet-5-5"), "max_tokens": 100,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", body, {
        "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            text = json.loads(r.read())["content"][0]["text"]
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
        if data["category"] in CATEGORIES:
            return data["category"], min(float(data["confidence"]), 0.85), "llm"  # cap: never outranks clear rules
    except Exception:
        pass
    return None


def categorize(description: str, amount: float, use_llm: bool = False):
    cat, conf, src = rule_categorize(description)
    if use_llm and conf < 0.7:
        res = llm_categorize(description, amount)
        if res:
            return res
    return cat, conf, src
