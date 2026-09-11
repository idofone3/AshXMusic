"""
YouTube signature decipher in pure Python (no JS engine, no premade libs).

YouTube web players ship streaming URLs inside `signatureCipher` where the
`s=` parameter is scrambled by a generated JS function. Every version of that
function is a fixed sequence of only three primitive operations applied to
the signature string:

    reverse : a.reverse()
    splice  : a.splice(0, N)
    swap    : swap a[0] and a[N % len(a)]

We statically locate the decipher function + its helper op table in the
player JS, rebuild the operation list, and replay it in Python.
"""
import re
from typing import Callable, List, Optional, Tuple

_CACHE: dict = {}


def _brace_block(src: str, open_idx: int) -> str:
    """Return source from the '{' at open_idx through its matching '}'."""
    depth = 0
    for j in range(open_idx, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx:j + 1]
    raise ValueError("unbalanced braces in player js")


_MAIN_RE = re.compile(
    r'([a-zA-Z0-9$_]{1,8})\s*=\s*function\s*\(([a-zA-Z0-9$_]{1,8})\)\s*\{'
    r'\s*\2\s*=\s*\2\s*\.split\(\s*""\s*\)\s*;'
)

_FN_DEF_RE = re.compile(
    r'function\s+([a-zA-Z0-9$_]{1,8})\s*\(([a-zA-Z0-9$_]{1,8})\)\s*\{'
    r'\s*\2\s*=\s*\2\s*\.split\(\s*""\s*\)\s*;'
)

_HELPER_CALL_RE = re.compile(r'([a-zA-Z0-9$_]{1,8})\.([a-zA-Z0-9$_]{1,8})\s*\(')


def _find_main_body(js: str) -> str:
    m = _MAIN_RE.search(js) or _FN_DEF_RE.search(js)
    if not m:
        raise ValueError("decipher main function not found in player js")
    open_idx = js.index("{", m.start(), m.end())
    return _brace_block(js, open_idx)


def _find_helper_ops(js: str, body: str) -> dict:
    """Map helper fn names -> op kind by parsing the op-table object."""
    mh = _HELPER_CALL_RE.search(body)
    if not mh:
        return {}
    obj_name = mh.group(1)
    mdef = re.search(r'(?:var\s+|[,;\s])' + re.escape(obj_name) + r'\s*=\s*\{', js)
    if not mdef:
        return {}
    obj_src = _brace_block(js, js.index("{", mdef.start(), mdef.end()))
    ops = {}
    for mm in re.finditer(
            r'([a-zA-Z0-9$_]{1,8})\s*:\s*function\s*\(([^)]*)\)\s*\{', obj_src):
        name = mm.group(1)
        try:
            fbody = _brace_block(obj_src, obj_src.index("{", mm.start(), mm.end()))
        except ValueError:
            continue
        if ".reverse()" in fbody:
            ops[name] = "reverse"
        elif re.search(r'\.splice\(\s*0\s*,', fbody):
            ops[name] = "splice"
        elif re.search(r'\[\s*0\s*\]', fbody) and "%" in fbody:
            ops[name] = "swap"
    return ops


_CALL_RE = re.compile(
    r'([a-zA-Z0-9$_]{1,8})\.([a-zA-Z0-9$_]{1,8})\(\s*[a-zA-Z0-9$_]{1,8}\s*'
    r'(?:,\s*(\d+)\s*)?\)'
)
_INLINE_SPLICE_RE = re.compile(r'\.splice\(\s*0\s*,\s*(\d+)\s*\)')


def _build_plan(js: str) -> List[Tuple[str, Optional[int]]]:
    body = _find_main_body(js)
    helper_ops = _find_helper_ops(js, body)
    plan: List[Tuple[str, Optional[int]]] = []
    tokens = []
    for m in _CALL_RE.finditer(body):
        op = helper_ops.get(m.group(2))
        if op:
            n = int(m.group(3)) if m.group(3) else None
            tokens.append((m.start(), op, n))
    for m in re.finditer(r'\.reverse\(\)', body):
        tokens.append((m.start(), "reverse", None))
    for m in _INLINE_SPLICE_RE.finditer(body):
        tokens.append((m.start(), "splice", int(m.group(1))))
    tokens.sort(key=lambda t: t[0])
    for _, op, n in tokens:
        plan.append((op, n))
    if not plan:
        raise ValueError("no decipher operations extracted")
    return plan


def make_decipher(player_js: str) -> Callable[[str], str]:
    key = hash(player_js)
    if key in _CACHE:
        return _CACHE[key]

    plan = _build_plan(player_js)

    def decipher(sig: str) -> str:
        a = list(sig)
        for op, n in plan:
            if op == "reverse":
                a.reverse()
            elif op == "splice":
                del a[:n]
            elif op == "swap":
                pos = n % len(a)
                a[0], a[pos] = a[pos], a[0]
        return "".join(a)

    _CACHE[key] = decipher
    return decipher


def decipher_url(url: str, s: str, sp: str = "sig", player_js: str = None) -> str:
    dec = make_decipher(player_js)
    return f"{url}&{sp}={dec(s)}"
