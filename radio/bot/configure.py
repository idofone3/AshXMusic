"""/configure — set the RTMP server + stream key from the chat.

Flow:
  1. parse  /configure <rtmp_url> <stream_key>   (or url/key in one arg)
  2. persist both as repo Actions secrets via the `gh` CLI
     (GH_TOKEN + GITHUB_REPOSITORY are present inside the runner)
  3. write a RESTART marker to the state branch -> the station exits
     gracefully -> the workflow's chain step re-dispatches -> next run
     picks up the new credentials automatically.
"""
import os
import re
import subprocess

from radio import stateio


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;")


def parse_creds(arg: str):
    """Returns (url, key) or (None, reason)."""
    parts = (arg or "").strip().split()
    if len(parts) >= 2:
        url, key = parts[0], parts[1]
    else:
        # single blob like rtmps://host/s/12345:secret
        m = re.match(r"^(rtmps?://\S+?/)\s*(\S+)$", arg or "")
        if not m:
            return None, "send: <code>/configure rtmps://dc5-1.rtmp.t.me/s/ 3131932193:YOUR_KEY</code>"
        url, key = m.group(1), m.group(2)
    if not re.match(r"^rtmps?://", url):
        return None, "server must start with <code>rtmp://</code> or <code>rtmps://</code>"
    if len(key) < 4:
        return None, "that stream key looks too short"
    return url, key


def gh_available() -> bool:
    return bool(os.environ.get("GH_TOKEN")) and bool(
        os.environ.get("GITHUB_REPOSITORY"))


def save_secrets(url: str, key: str) -> tuple[bool, str]:
    """gh secret set RTMP_URL / RTMP_KEY (works inside Actions runners)."""
    env = dict(os.environ)
    errs = []
    for name, val in (("RTMP_URL", url), ("RTMP_KEY", key)):
        r = subprocess.run(
            ["gh", "secret", "set", name, "--body", val],
            env=env, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "failed").strip()[:120]
            errs.append(f"{name}: {msg}")
    return (not errs), "; ".join(errs)


def mask_url(url: str) -> str:
    return re.sub(r"//(.+?)/", r"//\1/", url or "")


def run_configure(bot, chat_id: str, arg: str):
    url, key = parse_creds(arg)
    if not url:
        bot.api.send_message(chat_id,
                             f"⚠️ {_esc(key) if key else 'invalid format'}")
        return
    bot.api.send_message(
        chat_id,
        f"🔧 <b>Configuration received</b>\n"
        f"server: <code>{_esc(mask_url(url))}</code>\n"
        f"key: <code>{_esc(key[:6])}…</code>\n\n"
        f"💾 Saving to Actions secrets…")
    if not gh_available():
        bot.api.send_message(
            chat_id,
            "⚠️ No GH_TOKEN here — creds can't be persisted from this "
            "process. Enter them in the workflow UI instead "
            "(Actions → AshXMusic → Run workflow).")
        return
    ok, err = save_secrets(url, key)
    if not ok:
        bot.api.send_message(chat_id,
                             f"❌ Failed to save secrets:\n<code>{_esc(err)}"
                             f"</code>")
        return
    stateio.set_restart()
    try:
        stateio.push_state_branch(bot.repo_dir)
    except Exception:
        pass
    bot.api.send_message(
        chat_id,
        "✅ <b>Saved.</b> Restarting the stream with the new server…\n"
        "The current run exits and the workflow re-launches "
        "automatically (≈2–3 min).")


def run_status_config(bot, chat_id: str):
    url = os.environ.get("RTMP_URL", "")
    key = os.environ.get("RTMP_KEY", "")
    masked = (key[:4] + "…" if key else "—")
    bot.api.send_message(
        chat_id,
        f"📺 <b>Stream config</b>\n"
        f"server: <code>{_esc(mask_url(url) or 'not set')}</code>\n"
        f"key: <code>{_esc(masked)}</code>\n"
        f"chat: <code>{_esc(str(bot.control or '—'))}</code>\n\n"
        f"Change with: <code>/configure rtmps://… key</code>")
