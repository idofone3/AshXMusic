"""Inline keyboards — the radio's remote-control buttons."""
import json


def _kb(rows: list) -> dict:
    return {"inline_keyboard": rows}


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def np_kb(paused: bool = False, loop: str = "off", autoplay: bool = True,
          shuffle: bool = False) -> dict:
    loop_lbl = {"off": "🔁 Loop: off", "one": "🔂 Loop: one",
                "all": "🔁 Loop: all"}.get(loop, "🔁 Loop: off")
    r1 = [
        _btn("⏸ Pause" if not paused else "▶ Resume",
             "act:pause" if not paused else "act:resume"),
        _btn("⏭ Skip", "act:skip"),
    ]
    r2 = [
        _btn(loop_lbl, "act:loop"),
        _btn("🔀 Shuffle: on" if shuffle else "🔀 Shuffle: off",
             "act:shuffle"),
    ]
    r3 = [
        _btn("🤖 Autoplay: on" if autoplay else "🤖 Autoplay: off",
             "act:autoplay"),
        _btn("📋 Queue", "act:queue"),
    ]
    return _kb([r1, r2, r3])


def queue_kb(n: int) -> dict | None:
    if n <= 0:
        return None
    row = [_btn(f"❌ {i}", f"rm:{i - 1}") for i in range(1, min(n, 8) + 1)]
    return _kb([row, [_btn("🔀 Shuffle all", "act:shufflequeue"),
                      _btn("🗑 Clear", "act:clearqueue")]])


def search_kb(count: int) -> dict | None:
    if count <= 0:
        return None
    row = [_btn(str(i), f"pick:{i - 1}") for i in range(1, count + 1)]
    return _kb([row, [_btn("Cancel", "pick:cancel")]])


def help_kb(repo_url: str = "") -> dict:
    rows = [[_btn("⏸ Pause", "act:pause"), _btn("⏭ Skip", "act:skip"),
             _btn("▶ Resume", "act:resume")]]
    if repo_url:
        rows.append([{"text": "⭐ Source", "url": repo_url}])
    return _kb(rows)
