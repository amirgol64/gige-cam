#!/usr/bin/env python3
"""
camera_mgr.py — single combined GStreamer pipeline with a tee:
  branch 1 → H.264/RTP/UDP  (main stream to receiver)
  branch 2 → MJPEG/TCP      (5 fps preview served on port 8765)

ZMQ REP on tcp://127.0.0.1:5555 for control commands.
Watchdog thread auto-restarts the pipeline if GStreamer exits unexpectedly.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time

import zmq

sys.path.insert(0, os.path.dirname(__file__))
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [cam_mgr] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

ZMQ_ADDR = "tcp://127.0.0.1:5555"
PREVIEW_PORT = 8765


# ---------------------------------------------------------------------------
# Pipeline builder
# ---------------------------------------------------------------------------

_AWB_MODE = {
    "incandescent": 1, "tungsten": 2, "fluorescent": 3,
    "indoor": 4, "daylight": 5, "cloudy": 6,
}


def _libcamerasrc_props(c: dict) -> str:
    """Return libcamerasrc property string for exposure, gain, and AWB."""
    parts = []

    if c.get("night_mode"):
        # Maximum shutter for IMX219 = 66 666 µs (~15 fps limit)
        parts += ["ae-enable=false", "exposure-time=66666", "analogue-gain=10.0"]
    elif c.get("exposure") == "manual":
        shutter = min(int(c.get("exposure_value", 10000)), 66666)  # IMX219 hw max
        gain    = max(1.0, min(float(c.get("gain", 1.0)), 10.8))  # IMX219 hw range
        parts += ["ae-enable=false", f"exposure-time={shutter}", f"analogue-gain={gain}"]
    # else: full auto — no properties needed

    awb = c.get("white_balance", "auto")
    if awb in _AWB_MODE:
        parts.append(f"awb-mode={_AWB_MODE[awb]}")

    return (" ".join(parts) + " ") if parts else ""


def _flip_elements(c: dict) -> str:
    parts = []
    rot = c.get("rotation", 0)
    if rot == 90:
        parts.append("videoflip method=clockwise !")
    elif rot == 180:
        parts.append("videoflip method=rotate-180 !")
    elif rot == 270:
        parts.append("videoflip method=counterclockwise !")
    if c.get("hflip"):
        parts.append("videoflip method=horizontal-flip !")
    if c.get("vflip"):
        parts.append("videoflip method=vertical-flip !")
    return (" ".join(parts) + " ") if parts else ""


def build_pipeline(cfg: dict) -> str:
    c = cfg["camera"]
    s = cfg["stream"]
    n = cfg["network"]

    w, h    = c["width"], c["height"]
    bitrate = s["bitrate_kbps"] * 1000
    gop     = s["gop"]
    flip    = _flip_elements(c)
    props   = _libcamerasrc_props(c)

    # Night mode caps sensor at ~15 fps (66 ms shutter can't go faster)
    fps = min(c["framerate"], 15) if c.get("night_mode") else c["framerate"]

    extra = (
        f"controls,video_bitrate={bitrate},"
        f"h264_profile=1,h264_level=13,"
        f"h264_i_frame_period={gop},"
        f"repeat_sequence_header=1"
    )

    return (
        # Source
        f"gst-launch-1.0 "
        f"libcamerasrc {props}! "
        f"'video/x-raw,width={w},height={h},framerate={fps}/1' ! "
        f"{flip}"

        # Split
        f"tee name=t "

        # ── branch 1: H.264 / RTP / UDP ──────────────────────────────
        f"t. ! queue leaky=downstream max-size-buffers=2 ! "
        f"videoconvert ! "
        f"v4l2h264enc extra-controls=\"{extra}\" ! "
        f"'video/x-h264,profile=baseline,level=(string)4' ! "
        f"h264parse config-interval=1 ! "
        f"rtph264pay pt=96 config-interval=1 ! "
        f"udpsink host={n['receiver_ip']} port={n['receiver_port']} sync=false "

        # ── branch 2: JPEG preview / TCP (5 fps) ─────────────────────
        f"t. ! queue leaky=downstream max-size-buffers=2 ! "
        f"videorate drop-only=true ! "
        f"'video/x-raw,framerate=5/1' ! "
        f"videoconvert ! "
        f"videoscale method=1 add-borders=true ! "
        f"'video/x-raw,width=640,height=480' ! "
        f"jpegenc quality=70 ! "
        f"multipartmux boundary=frame ! "
        f"tcpserversink host=127.0.0.1 port={PREVIEW_PORT} "
        f"recover-policy=keyframe sync=false"
    )


# ---------------------------------------------------------------------------
# Pipeline manager with watchdog
# ---------------------------------------------------------------------------

class PipelineManager:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._started_at: float | None = None
        self._restarts = 0
        self._should_run = False   # user intent — False = intentionally stopped

        t = threading.Thread(target=self._watchdog, daemon=True)
        t.start()

    # ------------------------------------------------------------------
    def start(self, cfg: dict) -> str:
        with self._lock:
            self._should_run = True
            return self._start_unlocked(cfg)

    def stop(self) -> str:
        with self._lock:
            self._should_run = False
            self._stop_unlocked()
            return "ok"

    def restart(self, cfg: dict) -> str:
        return self.start(cfg)

    # ------------------------------------------------------------------
    def _start_unlocked(self, cfg: dict) -> str:
        self._stop_unlocked()
        cmd = build_pipeline(cfg)
        log.info("Starting pipeline: %s", cmd)
        try:
            self._proc = subprocess.Popen(
                cmd, shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,   # own process group so killpg reaches gst-launch
            )
            print(f"[PIPELINE STARTED] {time.strftime('%H:%M:%S')} uptime={open('/proc/uptime').read().split()[0]}s", flush=True)
            self._started_at = time.time()
            self._restarts += 1
            return "ok"
        except Exception as e:
            log.error("Failed to start pipeline: %s", e)
            return f"error: {e}"

    def _stop_unlocked(self):
        if self._proc and self._proc.poll() is None:
            try:
                # Kill the whole process group: covers both /bin/sh and gst-launch-1.0
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                self._proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass  # already gone
        self._proc = None
        self._started_at = None

    # ------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            uptime = int(time.time() - self._started_at) if running and self._started_at else 0
            return {"running": running, "uptime_s": uptime, "restarts": self._restarts}

    # ------------------------------------------------------------------
    def _watchdog(self):
        """Restart the pipeline if GStreamer exits while _should_run is True."""
        while True:
            time.sleep(5)
            needs_restart = False
            with self._lock:
                if (
                    self._should_run
                    and self._proc is not None
                    and self._proc.poll() is not None
                ):
                    log.warning(
                        "Pipeline exited unexpectedly (rc=%d) — restarting",
                        self._proc.returncode,
                    )
                    self._stop_unlocked()
                    needs_restart = True

            if needs_restart:
                cfg = config.load()
                with self._lock:
                    if self._should_run:   # check again after re-acquiring lock
                        self._start_unlocked(cfg)


# ---------------------------------------------------------------------------
# System info
# ---------------------------------------------------------------------------

def _cpu_percent() -> float | None:
    """Read /proc/stat twice (200 ms apart) for an accurate CPU %. """
    def _read():
        with open("/proc/stat") as f:
            vals = list(map(int, f.readline().split()[1:]))
        idle = vals[3]
        return idle, sum(vals)

    try:
        i1, t1 = _read()
        time.sleep(0.2)
        i2, t2 = _read()
        dt = t2 - t1
        return round((1 - (i2 - i1) / dt) * 100, 1) if dt else None
    except Exception:
        return None


def get_system_info() -> dict:
    info: dict = {}

    try:
        info["temp_c"] = round(
            int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000, 1
        )
    except Exception:
        info["temp_c"] = None

    try:
        info["uptime_s"] = int(float(open("/proc/uptime").read().split()[0]))
    except Exception:
        info["uptime_s"] = 0

    try:
        lines = {l.split(":")[0]: int(l.split()[1])
                 for l in open("/proc/meminfo") if ":" in l}
        total = lines.get("MemTotal", 0)
        info["mem_total_mb"] = total // 1024
        info["mem_used_mb"]  = (total - lines.get("MemAvailable", 0)) // 1024
    except Exception:
        info["mem_total_mb"] = info["mem_used_mb"] = 0

    info["cpu_pct"] = _cpu_percent()

    try:
        result = subprocess.run(
            ["ip", "-br", "addr"], capture_output=True, text=True, timeout=2
        )
        info["interfaces"] = [
            {"name": p[0], "state": p[1], "addr": p[2] if len(p) > 2 else ""}
            for p in (l.split() for l in result.stdout.splitlines())
            if len(p) >= 2
        ]
    except Exception:
        info["interfaces"] = []

    return info


# ---------------------------------------------------------------------------
# ZMQ control server
# ---------------------------------------------------------------------------

def zmq_server(mgr: PipelineManager):
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REP)
    sock.bind(ZMQ_ADDR)
    log.info("ZMQ listening on %s", ZMQ_ADDR)

    handlers = {
        "start":   lambda: mgr.start(config.load()),
        "stop":    lambda: mgr.stop(),
        "restart": lambda: mgr.restart(config.load()),
        "apply":   lambda: mgr.restart(config.load()),
    }

    while True:
        try:
            msg = sock.recv_json()
            cmd = msg.get("cmd", "")

            if cmd in handlers:
                sock.send_json({"status": handlers[cmd]()})
            elif cmd == "status":
                sock.send_json({"status": "ok", "data": mgr.status()})
            elif cmd == "sysinfo":
                sock.send_json({"status": "ok", "data": get_system_info()})
            else:
                sock.send_json({"status": f"unknown: {cmd}"})

        except Exception as e:
            log.error("ZMQ error: %s", e)
            try:
                sock.send_json({"status": f"error: {e}"})
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    mgr = PipelineManager()

    def _shutdown(sig, frame):
        log.info("Shutting down")
        mgr.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("Auto-starting combined pipeline")
    mgr.start(config.load())
    zmq_server(mgr)


if __name__ == "__main__":
    main()
