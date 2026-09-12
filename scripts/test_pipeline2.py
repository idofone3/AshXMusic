"""Offline pipeline test for AshXMusic station upgrades:
slate -> song A -> PAUSE 3s -> resume A mid-song -> song B -> verify flv.
Also exercises the pause slate + -ss resume remux path.
"""
import os
import subprocess
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["YTM_NO_TG"] = "1"

import radio.station as ST  # noqa: E402

DL = "/home/z/my-project/ytm-api/downloads"
SONGS = [
    ("NJAv_7lHUIU", 12.0),   # Kesariya
    ("EQxEms7gnqs", 9.0),    # Shayad
]
OUT = "/tmp/axm_pipe.flv"


def find_song(vid):
    for name in os.listdir(DL):
        if name.endswith(f"[{vid}].mp4"):
            return os.path.join(DL, name)
    return None


def fake_args():
    return types.SimpleNamespace(hours=1, song="", rtmp="", key="",
                                 station="AshXMusic", clear_stop=False)


def main():
    t0 = time.time()
    st = ST.Station.__new__(ST.Station)   # no engine — pure pipeline test
    st.alive = True
    st.args = fake_args()
    st.skip_ev = threading.Event()
    st.jump_ev = threading.Event()
    st.pause_ev = threading.Event()
    st.pause_offset = 0.0
    st._pause_vid = None
    st.state = {"queue": [], "history": [], "autoplay": True,
                "loop": "off", "shuffle": False, "volume": 110.0,
                "songsPlayed": 0}
    st.current = None
    st.current_t0 = 0.0
    st.tg = None
    st.qlock = threading.RLock()

    # 1) slates (fresh — drop any cached from previous boots)
    for p in (ST.SLATE_TS, ST.PAUSE_TS):
        try:
            os.remove(p)
        except OSError:
            pass
    st._render_slate()
    st._render_pause_slate()
    assert os.path.exists(ST.SLATE_TS), "slate missing"
    assert os.path.exists(ST.PAUSE_TS), "pause slate missing"
    print("slates ok")

    # 2) render both songs with the new visuals + volume 110
    ts_files = {}
    from radio import visuals as V
    for vid, cut in SONGS:
        src = find_song(vid)
        assert src, f"no cached mp4 for {vid}"
        cov = os.path.join(ST.COVERS, f"{vid}.jpg")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
                        "-frames:v", "1", cov], check=True, timeout=60)
        ts = f"/tmp/axm_t_{vid}.ts"
        graph = V.build_song_graph("Test Song " + vid[:4], "Test Artist",
                                   float(cut), ST.WORK, station="AshXMusic",
                                   queued_by="/play test")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", src, "-loop", "1", "-i", cov,
                        "-filter_complex", graph,
                        "-map", "[vout]", "-map", "0:a"] +
                       V.ffmpeg_common_args(ts, float(cut), volume=110.0),
                       check=True, timeout=300, stdin=subprocess.DEVNULL)
        ts_files[vid] = ts
        print(f"rendered {vid}: {os.path.getsize(ts)/1e6:.1f}MB")
    st.COVERS = ST.COVERS  # for notify paths (tg None -> skipped)

    # 3) pump -> flv with pause + resume
    for p in (ST.FIFO, OUT):
        try:
            os.remove(p)
        except OSError:
            pass
    os.mkfifo(ST.FIFO)
    s_err = open("/tmp/axm_streamer_test.log", "wb")
    streamer = subprocess.Popen(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-re",
         "-fflags", "+genpts", "-i", ST.FIFO,
         "-c:v", "copy", "-c:a", "copy", "-bsf:a", "aac_adtstoasc",
         "-f", "flv", "-flvflags", "no_duration_filesize", OUT],
        stdout=subprocess.DEVNULL, stderr=s_err, stdin=subprocess.DEVNULL)
    time.sleep(0.3)

    off = 0.0
    # opening slate: pump the FULL slate file (like production)
    off += st._pump_once(ST.SLATE_TS, off, ST.SLATE_SEC)
    # song A: full 12s
    off += st._pump_once(ts_files[SONGS[0][0]], off, SONGS[0][1])
    # PAUSE 3s: set pause_ev, clear it from a timer (like /resume)
    st.pause_ev.set()
    off_pause_start = off

    def _auto_resume():
        time.sleep(3.0)
        st.pause_ev.clear()
    threading.Thread(target=_auto_resume, daemon=True).start()
    while st.pause_ev.is_set():
        off += st._pump_once(ST.PAUSE_TS, off, ST.PAUSE_SEC)
    pause_len = off - off_pause_start
    print(f"pause segment length: {pause_len:.1f}s (want 3..9)")
    # resume song A from its 4s mark (like the station does)
    resume_at = 4.0
    off += st._pump_once(ts_files[SONGS[0][0]], off,
                         SONGS[0][1], ss=resume_at)
    # song B full
    off += st._pump_once(ts_files[SONGS[1][0]], off, SONGS[1][1])
    wall = time.time() - t0
    print(f"pump done: offset {off:.1f}s in wall {wall:.1f}s")
    time.sleep(0.5)
    streamer.terminate()
    streamer.wait(10)
    s_err.close()

    # 4) verify
    dur = ST.ffprobe_dur(OUT)
    print(f"out.flv duration: {dur:.1f}s (expected ~{off:.1f})")
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_name,width,height",
                        "-of", "csv", OUT], capture_output=True, text=True)
    print(r.stdout.strip())
    for t, name in ((2, "/tmp/axm_f_slate.png"),
                    (25, "/tmp/axm_f_pause.png"),
                    (31, "/tmp/axm_f_resume.png"),
                    (48, "/tmp/axm_f_songB.png")):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t),
                        "-i", OUT, "-frames:v", "1", name],
                       check=False, timeout=60)
    print("frames extracted")
    print(f"TOTAL TEST TIME {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
