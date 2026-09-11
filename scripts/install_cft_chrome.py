"""Install Chrome for Testing (stable, linux64) into seleniumbase/drivers
and symlink into ~/.local/bin so undetected-chrome finds it."""
import io
import json
import os
import stat
import sys
import urllib.request
import zipfile

META = ("https://googlechromelabs.github.io/chrome-for-testing/"
        "last-known-good-versions-with-downloads.json")

import seleniumbase
DRV_DIR = os.path.join(os.path.dirname(seleniumbase.__file__), "drivers")
CHROME_DIR = os.path.join(DRV_DIR, "chrome-linux64")

with urllib.request.urlopen(META, timeout=60) as r:
    meta = json.load(r)

stable = meta["channels"]["Stable"]
version = stable["version"]
downloads = stable["downloads"]

def url_for(name):
    for d in downloads.get(name, []):
        if d["platform"] == "linux64":
            return d["url"]
    return None

os.makedirs(DRV_DIR, exist_ok=True)

if not os.path.exists(os.path.join(CHROME_DIR, "chrome")):
    u = url_for("chrome")
    print("downloading chrome", version, u)
    with urllib.request.urlopen(u, timeout=300) as r:
        buf = io.BytesIO(r.read())
    with zipfile.ZipFile(buf) as z:
        z.extractall(DRV_DIR)
    for n in ("chrome", "chrome_crashpad_handler", "chrome_sandbox",
              "chrome-wrapper"):
        p = os.path.join(CHROME_DIR, n)
        if os.path.exists(p):
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
else:
    print("chrome already present", version)

# chromedriver into same tree (SB undetected wants a matching driver)
CD_DIR = os.path.join(DRV_DIR, "chromedriver-linux64")
if not os.path.exists(os.path.join(CD_DIR, "chromedriver")):
    u = url_for("chromedriver")
    print("downloading chromedriver", version, u)
    with urllib.request.urlopen(u, timeout=300) as r:
        buf = io.BytesIO(r.read())
    with zipfile.ZipFile(buf) as z:
        z.extractall(DRV_DIR)
    p = os.path.join(CD_DIR, "chromedriver")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
else:
    print("chromedriver already present")

local_bin = os.path.expanduser("~/.local/bin")
os.makedirs(local_bin, exist_ok=True)
drv = os.path.join(CHROME_DIR, "chrome")
for name in ("chrome", "google-chrome", "chromium"):
    link = os.path.join(local_bin, name)
    if not os.path.exists(link):
        os.symlink(drv, link)
print("installed:", version)
