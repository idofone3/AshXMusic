"""Queue persistence: radio/state/state.json + optional push to the
`radio-state` branch so the next Actions run resumes the same queue."""
import base64
import json
import os
import threading
import time

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
STOP_PATH = os.path.join(STATE_DIR, "STOP")
RESTART_PATH = os.path.join(STATE_DIR, "RESTART")

_LOCK = threading.Lock()


def _new_state():
    return {"queue": [], "history": [], "current": {}, "savedAt": 0,
            "autoplay": True, "loop": "off", "shuffle": False,
            "volume": 100.0, "songsPlayed": 0}


def load() -> dict:
    with _LOCK:
        try:
            with open(STATE_PATH) as f:
                st = json.load(f)
        except Exception:
            st = _new_state()
    for k, v in _new_state().items():
        st.setdefault(k, v)
    return st


def save(st: dict):
    with _LOCK:
        os.makedirs(STATE_DIR, exist_ok=True)
        st["savedAt"] = time.time()
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)


def set_stop():
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STOP_PATH, "w") as f:
        f.write("stop requested\n")


def clear_stop():
    try:
        os.remove(STOP_PATH)
    except OSError:
        pass


def stop_requested() -> bool:
    return os.path.exists(STOP_PATH)


def set_restart():
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(RESTART_PATH, "w") as f:
        f.write("restart requested\n")


def clear_restart():
    try:
        os.remove(RESTART_PATH)
    except OSError:
        pass


def restart_requested() -> bool:
    return os.path.exists(RESTART_PATH)


def push_state_branch(repo_dir: str):
    """Force-push radio/state to the `radio-state` branch (needs GH_PAT)."""
    pat = os.environ.get("GH_PAT", "")
    if not pat:
        return False
    slug = os.environ.get("GITHUB_REPOSITORY", "")
    if not slug:
        # local: derive from git remote if present
        try:
            import subprocess
            url = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=repo_dir, capture_output=True, text=True).stdout.strip()
            m = __import__("re").search(r"github\.com[:/](.+?)(\.git)?$", url)
            slug = m.group(1) if m else ""
        except Exception:
            slug = ""
    if not slug:
        return False
    url = f"https://x-access-token:{pat}@github.com/{slug}.git"
    import subprocess
    tmp = os.path.join("/tmp", f"radiostate-{int(time.time())}")
    try:
        os.makedirs(tmp, exist_ok=True)
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        def run(*a, ok=False):
            return subprocess.run(a, cwd=tmp, capture_output=True,
                                  text=True, env=env)
        r = run("git", "clone", "--depth", "1", "-b", "radio-state", url, ".")
        if r.returncode != 0:
            r = run("git", "init", "-b", "radio-state")
            if r.returncode != 0:
                return False
            run("git", "remote", "add", "origin", url)
        # copy state files
        import shutil
        for name in os.listdir(STATE_DIR):
            if name.endswith(".tmp"):
                continue
            shutil.copy(os.path.join(STATE_DIR, name), os.path.join(tmp, name))
        run("git", "add", "-A")
        run("git", "-c", "user.email=radio@bot.local",
            "-c", "user.name=radio-bot", "commit", "-m",
            f"state update {time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        run("git", "push", "-f", "origin", "radio-state")
        return True
    except Exception:
        return False
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def pull_state_branch(repo_dir: str) -> bool:
    """Pull latest state.json (and STOP marker) from radio-state branch.
    Only fills in files that don't exist locally yet."""
    pat = os.environ.get("GH_PAT", "")
    slug = os.environ.get("GITHUB_REPOSITORY", "")
    if not (pat and slug):
        return False
    import subprocess
    import shutil
    tmp = os.path.join("/tmp", f"radiopull-{int(time.time())}")
    try:
        url = f"https://x-access-token:{pat}@github.com/{slug}.git"
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        r = subprocess.run(["git", "clone", "--depth", "1", "-b",
                            "radio-state", url, tmp],
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:
            return False
        os.makedirs(STATE_DIR, exist_ok=True)
        for name in ("state.json", "STOP", "RESTART"):
            src = os.path.join(tmp, name)
            dst = os.path.join(STATE_DIR, name)
            if os.path.exists(src) and not os.path.exists(dst):
                shutil.copy(src, dst)
        return True
    except Exception:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
