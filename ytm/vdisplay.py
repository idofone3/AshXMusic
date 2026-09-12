"""One persistent Xvfb for the whole process.

SeleniumBase's own xvfb handling is racy (~80ms wait) and its base_case
uses use_xauth=True which dies when xauth is missing -> silent headless
fallback -> low-trust pot token -> control-parts-only throttle bursts.
Starting a real virtual display BEFORE seleniumbase is imported keeps
Chrome headed (trusted) on machines with no GUI.
"""
import glob
import os
import threading

_LOCK = threading.Lock()
_DISPLAY = None


def ensure(width: int = 1440, height: int = 1880):
    """Start one persistent Xvfb if DISPLAY is not set. Safe to call
    repeatedly (idempotent)."""
    global _DISPLAY
    with _LOCK:
        if os.environ.get("DISPLAY"):
            return os.environ["DISPLAY"]
        # leftover lockfiles from a crashed run block :N startup
        for lk in glob.glob("/tmp/.X*-lock"):
            try:
                os.remove(lk)
            except OSError:
                pass
        try:
            from pyvirtualdisplay import Display
            _DISPLAY = Display(visible=False, size=(width, height),
                               color_depth=24)
            _DISPLAY.start()
            os.environ["DISPLAY"] = _DISPLAY.new_display_var or ":99"
            return os.environ["DISPLAY"]
        except Exception:
            # last resort: reuse any existing X socket
            for sock in sorted(glob.glob("/tmp/.X11-unix/X*")):
                try:
                    n = int(sock.rsplit("X", 1)[1])
                except ValueError:
                    continue
                os.environ["DISPLAY"] = f":{n}"
                return os.environ["DISPLAY"]
        return None
