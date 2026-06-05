# ─────────────────────────────────────────────────────────────────────────────
# camera.py  –  Reliable camera open + frame capture for Raspberry Pi
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import site
import time
import logging
import subprocess
from shutil import which
import cv2
import numpy as np

from config import (
    CAMERA_SOURCE,
    CAMERA_TYPE,
    USB_CAMERA_INDEX,
    CAMERA_WIDTH,
    CAMERA_HEIGHT,
    CAMERA_WARMUP_FRAMES,
    STREAM_FPS,
)

log = logging.getLogger(__name__)

# Raspberry Pi OS installs picamera2 here; venvs often cannot see it otherwise.
_PI_SITE_PATHS = (
    "/usr/lib/python3/dist-packages",
    "/usr/local/lib/python3/dist-packages",
)


def _enable_system_packages() -> None:
    """Allow importing apt packages (picamera2) from a project venv."""
    for path in _PI_SITE_PATHS:
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    try:
        for path in site.getsitepackages():
            if path not in sys.path:
                sys.path.append(path)
    except Exception:
        pass


def _opencv_has_gstreamer() -> bool:
    try:
        return "GStreamer:                   YES" in cv2.getBuildInformation()
    except Exception:
        return False


def _configure_capture(cap: cv2.VideoCapture) -> None:
    """Reduce latency and set a stable resolution for USB/V4L2 devices."""
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    try:
        cap.set(cv2.CAP_PROP_FPS, STREAM_FPS)
    except Exception:
        pass
    try:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    except Exception:
        pass


def _verify_capture(cap, label: str, warmup: int = CAMERA_WARMUP_FRAMES) -> bool:
    """
    OpenCV often reports isOpened()=True before the device delivers frames.
    Discard warmup frames and require at least one valid buffer.
    """
    _configure_capture(cap)
    for i in range(warmup):
        ret, frame = cap.read()
        if ret and frame is not None and getattr(frame, "size", 0) > 0:
            log.info("Camera verified (%s) after %d frame(s)", label, i + 1)
            return True
        time.sleep(0.05)
    log.warning("Camera opened but no frames from %s", label)
    return False


class Picamera2Capture:
    """Picamera2 backend for Pi Camera Module on Bookworm (libcamera)."""

    def __init__(self):
        _enable_system_packages()
        from picamera2 import Picamera2

        infos = Picamera2.global_camera_info()
        if not infos:
            raise RuntimeError(
                "No Pi camera detected. Check ribbon cable and run: libcamera-hello -t 2000"
            )

        self._picam = Picamera2()
        frame_us = int(1_000_000 / max(STREAM_FPS, 1))
        cfg = self._picam.create_video_configuration(
            main={"size": (CAMERA_WIDTH, CAMERA_HEIGHT), "format": "RGB888"},
            controls={"FrameDurationLimits": (frame_us, frame_us)},
        )
        self._picam.configure(cfg)
        self._picam.start()
        time.sleep(1.0)
        self._opened = True

    def isOpened(self) -> bool:
        return self._opened

    def read(self):
        try:
            rgb = self._picam.capture_array()
            if rgb is None or rgb.size == 0:
                return False, None
            return True, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        except Exception as e:
            log.debug("Picamera2 read failed: %s", e)
            return False, None

    def release(self):
        try:
            self._picam.stop()
        except Exception:
            pass
        self._opened = False


class RpicamVidCapture:
    """
    Fallback when picamera2/GStreamer are unavailable but rpicam-apps is installed.
    Reads MJPEG frames from: rpicam-vid -t 0 --codec mjpeg -o -
    """

    SOI = b"\xff\xd8"
    EOI = b"\xff\xd9"

    def __init__(self, binary: str = "rpicam-vid"):
        cmd = [
            binary,
            "-t", "0",
            "--width", str(CAMERA_WIDTH),
            "--height", str(CAMERA_HEIGHT),
            "--codec", "mjpeg",
            "--flush",
            "--nopreview",
            "-o", "-",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._buf = b""
        self._opened = self._proc.stdout is not None
        if not self._opened:
            raise RuntimeError("rpicam-vid failed to start")

    def isOpened(self) -> bool:
        return self._opened and self._proc.poll() is None

    def _read_jpeg(self) -> bytes | None:
        out = self._proc.stdout
        if out is None:
            return None
        chunk = out.read(4096)
        if not chunk:
            return None
        self._buf += chunk
        start = self._buf.find(self.SOI)
        end = self._buf.find(self.EOI, start + 2)
        if start < 0 or end < 0:
            if len(self._buf) > 512_000:
                self._buf = self._buf[-64_000:]
            return None
        end += 2
        jpeg = self._buf[start:end]
        self._buf = self._buf[end:]
        return jpeg

    def read(self):
        deadline = time.time() + 5.0
        while time.time() < deadline:
            jpeg = self._read_jpeg()
            if jpeg:
                frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if frame is not None and frame.size > 0:
                    return True, frame
            time.sleep(0.01)
        return False, None

    def release(self):
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._opened = False


def _try_picamera2() -> Picamera2Capture | None:
    _enable_system_packages()
    try:
        from picamera2 import Picamera2  # noqa: F401
    except ImportError:
        log.warning(
            "Picamera2 not importable in this Python. Fix with either:\n"
            "  sudo apt install -y python3-picamera2\n"
            "  python3 -m venv --system-site-packages ../venv   (recreate venv)\n"
            "  pip install picamera2   (on Pi only)"
        )
        return None

    log.info("Trying Picamera2 (Pi Camera Module / libcamera)…")
    try:
        cap = Picamera2Capture()
        ret, frame = cap.read()
        if ret and frame is not None and frame.size > 0:
            log.info("Successfully opened Pi Camera via Picamera2")
            return cap
        cap.release()
        log.warning("Picamera2 opened but returned no frames")
    except Exception as e:
        log.warning("Picamera2 failed: %s", e)
    return None


def _try_gstreamer_pipeline(pipeline: str, label: str) -> cv2.VideoCapture | None:
    if not _opencv_has_gstreamer():
        return None
    log.info("Trying GStreamer: %s…", label)
    try:
        cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        if cap.isOpened() and _verify_capture(cap, label, warmup=20):
            log.info("Successfully opened Pi Camera via GStreamer (%s)", label)
            return cap
        if cap.isOpened():
            cap.release()
    except Exception as e:
        log.debug("GStreamer [%s] failed: %s", label, e)
    return None


def _try_gstreamer_pi() -> cv2.VideoCapture | None:
    if not _opencv_has_gstreamer():
        log.warning(
            "OpenCV was built without GStreamer (common with pip opencv). "
            "Use Picamera2 or: sudo apt install python3-opencv"
        )
        return None

    pipelines = [
        (
            f"libcamerasrc ! video/x-raw,width={CAMERA_WIDTH},height={CAMERA_HEIGHT},"
            f"framerate=30/1 ! videoconvert ! video/x-raw,format=BGR ! "
            "appsink drop=true max-buffers=1 sync=false",
            "libcamerasrc",
        ),
    ]
    for dev in ("/dev/video0", "/dev/video1", "/dev/video2"):
        if os.path.exists(dev):
            pipelines.append(
                (
                    f"v4l2src device={dev} ! video/x-raw,width={CAMERA_WIDTH},"
                    f"height={CAMERA_HEIGHT} ! videoconvert ! "
                    "video/x-raw,format=BGR ! appsink drop=true max-buffers=1 sync=false",
                    f"v4l2src {dev}",
                ),
            )

    for pipeline, label in pipelines:
        cap = _try_gstreamer_pipeline(pipeline, label)
        if cap is not None:
            return cap
    return None


def _try_rpicam_vid() -> RpicamVidCapture | None:
    for binary in ("rpicam-vid", "libcamera-vid"):
        if not which(binary):
            continue
        log.info("Trying %s (libcamera CLI)…", binary)
        try:
            cap = RpicamVidCapture(binary=binary)
            ret, frame = cap.read()
            if ret and frame is not None and frame.size > 0:
                log.info("Successfully opened Pi Camera via %s", binary)
                return cap
            cap.release()
        except Exception as e:
            log.warning("%s failed: %s", binary, e)
    return None


def _try_index(idx: int, backend: int | None, label: str) -> cv2.VideoCapture | None:
    log.info("Trying %s on index %d…", label, idx)
    try:
        if backend is None:
            cap = cv2.VideoCapture(idx)
        else:
            cap = cv2.VideoCapture(idx, backend)
        if cap.isOpened() and _verify_capture(cap, label):
            log.info("Successfully opened camera: %s index %d", label, idx)
            return cap
        if cap.isOpened():
            cap.release()
    except Exception as e:
        log.debug("%s index %d failed: %s", label, idx, e)
    return None


def _open_pi_camera():
    """
    Pi Camera Module — try in order:
      1. Picamera2 (apt / system-site-packages)
      2. GStreamer libcamerasrc / v4l2src
      3. rpicam-vid subprocess
      4. V4L2 /dev/videoN via OpenCV
    """
    cap = _try_picamera2()
    if cap is not None:
        return cap

    cap = _try_gstreamer_pi()
    if cap is not None:
        return cap

    cap = _try_rpicam_vid()
    if cap is not None:
        return cap

    log.info("Trying Pi cam as V4L2 device (libcamera compat layer)…")
    for idx in (0, 1, 2):
        cap = _try_index(idx, cv2.CAP_V4L2, f"Pi/V4L2 index {idx}")
        if cap is not None:
            return cap

    log.error(
        "Pi camera failed. On the Pi run:\n"
        "  libcamera-hello -t 2000\n"
        "  sudo apt install -y python3-picamera2 rpicam-apps\n"
        "  v4l2-ctl --list-devices\n"
        "If using a venv: python3 -m venv --system-site-packages ~/trash_detection/venv"
    )
    return None


def _open_usb_camera(idx: int = USB_CAMERA_INDEX) -> cv2.VideoCapture | None:
    """USB webcam via V4L2, then default backend, then other /dev/video indices."""
    cap = _try_index(idx, cv2.CAP_V4L2, "V4L2 USB")
    if cap is not None:
        return cap
    cap = _try_index(idx, None, "default USB backend")
    if cap is not None:
        return cap
    log.info("Scanning alternative USB indices…")
    for alt_idx in [0, 1, 2, 4]:
        if alt_idx == idx:
            continue
        cap = _try_index(alt_idx, cv2.CAP_V4L2, f"V4L2 USB index {alt_idx}")
        if cap is not None:
            return cap
    return None


def initialize_camera(source=CAMERA_SOURCE):
    """
    Open a camera and confirm frames are readable.
    Controlled by config.CAMERA_TYPE: usb | pi | auto.
    """
    if isinstance(source, str) and not str(source).isdigit():
        if str(source).lower() in ("libcamera", "picamera2", "pi"):
            return _open_pi_camera()
        log.info("Opening stream/file: %s", source)
        cap = cv2.VideoCapture(source)
        if cap.isOpened() and _verify_capture(cap, f"stream {source}"):
            return cap
        if cap.isOpened():
            cap.release()
        return None

    mode = (CAMERA_TYPE or "auto").lower()
    log.info("Camera mode: %s (stream %dx%d)", mode, CAMERA_WIDTH, CAMERA_HEIGHT)

    if mode == "pi":
        return _open_pi_camera()

    if mode == "usb":
        idx = int(source) if str(source).isdigit() else USB_CAMERA_INDEX
        return _open_usb_camera(idx)

    cap = _open_usb_camera(USB_CAMERA_INDEX)
    if cap is not None:
        return cap
    log.info("No USB camera – trying Pi Camera Module…")
    return _open_pi_camera()


def reopen_camera(cap):
    """Release and re-open the camera after sustained read failures."""
    log.error("Camera stalled – reopening…")
    try:
        cap.release()
    except Exception:
        pass
    time.sleep(0.5)
    return initialize_camera(CAMERA_SOURCE)
