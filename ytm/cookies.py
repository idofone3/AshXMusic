"""
Multi-format cookie parser.

Auto-detects and parses:
  - Netscape HTTP Cookie File (Cookie-Editor / curl export, incl. #HttpOnly_ lines)
  - JSON: array of {name,value,domain,...} objects, nested exports, or {name:value} maps
  - raw header string: "k1=v1; k2=v2; ..."
No premade libs — stdlib only.
"""
import json
from typing import Dict, List

CRITICAL_COOKIES = [
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
    "LOGIN_INFO", "VISITOR_INFO1_LIVE", "YSC", "SIDCC",
]


def _clean(text: str) -> str:
    """Drop real comment lines, but keep '#HttpOnly_' Netscape entries."""
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("#HttpOnly_"):
            lines.append(s[len("#HttpOnly_"):])
        elif s.startswith("#"):
            continue
        else:
            lines.append(ln)
    return "\n".join(lines)


def _walk_json(node, out: List[Dict]):
    """Recursively find any dict that looks like a cookie (has name+value)."""
    if isinstance(node, dict):
        if isinstance(node.get("name"), str) and "value" in node:
            out.append({
                "name": node["name"],
                "value": str(node["value"]),
                "domain": node.get("domain") or node.get("host") or ".youtube.com",
                "path": node.get("path") or "/",
            })
        for v in node.values():
            _walk_json(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_json(v, out)


def parse_cookies(raw: str) -> List[Dict]:
    if not raw or not raw.strip():
        return []
    body = _clean(raw).strip()

    # 1) JSON family
    if body.startswith("{") or body.startswith("["):
        try:
            data = json.loads(body)
            out: List[Dict] = []
            _walk_json(data, out)
            if out:
                return out
        except json.JSONDecodeError:
            pass

    lines = [l for l in body.splitlines() if l.strip()]

    # 2) Netscape tab-separated
    if lines and all(l.count("\t") >= 6 for l in lines):
        out = []
        for l in lines:
            p = l.split("\t")
            if len(p) >= 7:
                out.append({"name": p[5].strip(), "value": p[6].strip(),
                            "domain": p[0].strip(), "path": p[2].strip()})
        if out:
            return out

    # 3) Raw header string
    out = []
    for pair in body.replace("\n", ";").split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        k, _, v = pair.partition("=")
        k, v = k.strip(), v.strip().strip('"')
        if k:
            out.append({"name": k, "value": v, "domain": ".youtube.com", "path": "/"})
    return out


def summarize(cookies: List[Dict]) -> Dict:
    names = [c["name"] for c in cookies]
    return {
        "count": len(cookies),
        "names": names,
        "logged_in": any(n in ("SID", "__Secure-1PSID", "__Secure-3PSID") for n in names),
        "has_sapisid": "SAPISID" in names or "__Secure-1PAPISID" in names,
        "missing_critical": [c for c in CRITICAL_COOKIES if c not in names],
    }
