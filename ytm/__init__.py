"""ytm — pure-python YouTube Music client (no premade YT libraries)."""
import os as _os
import glob as _glob

__version__ = "1.1.0"


def _ensure_chrome_on_path():
    """Make sure the SeleniumBase Chrome-for-Testing binary is reachable.
    SeleniumBase downloads CfT into its drivers dir; some launches don't
    have ~/.local/bin (where we symlink it) on PATH, so fix it here."""
    home = _os.path.expanduser("~")
    local_bin = _os.path.join(home, ".local", "bin")
    if local_bin not in _os.environ.get("PATH", "").split(_os.pathsep):
        _os.environ["PATH"] = local_bin + _os.pathsep + _os.environ.get("PATH", "")
    if not any(_glob.glob(_os.path.join(local_bin, "*chrome*"))):
        import seleniumbase  # noqa
        drv = _os.path.join(_os.path.dirname(seleniumbase.__file__),
                            "drivers", "chrome-linux64", "chrome")
        if _os.path.exists(drv):
            try:
                _os.makedirs(local_bin, exist_ok=True)
                for name in ("chrome", "google-chrome", "chromium"):
                    link = _os.path.join(local_bin, name)
                    if not _os.path.exists(link):
                        _os.symlink(drv, link)
            except OSError:
                pass


_ensure_chrome_on_path()

try:
    from .vdisplay import ensure as _ensure_display
    _ensure_display()
except Exception:
    pass
