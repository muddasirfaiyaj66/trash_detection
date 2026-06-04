# ─────────────────────────────────────────────────────────────────────────────
# camera.py  –  Reliable camera open + frame capture for Raspberry Pi
# ─────────────────────────────────────────────────────────────────────────────

import time
import logging
import cv2
import numpy as np

from config import (
    CAMERA_SOURCE,
    CAMERA_TYPE,
    USB_CAMERA_INDEX,
    CAMERA_WIDTH,
    CAMERA_HEIGHT,
    CAMERA_WARMUP_FRAMES,
)

log = logging.getLogger(__name__)


def _configure_capture(cap: cv2.VideoCapture) -> None:
    """Reduce latency and set a stable resolution for USB/V4L2 devices."""
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
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
        from picamera2 import Picamera2  # optional dependency

        self._picam = Picamera2()
        cfg = self._picam.create_preview_configuration(
            main={"size": (CAMERA_WIDTH, CAMERA_HEIGHT), "format": "RGB888"}
        )
        self._picam.configure(cfg)
        self._picam.start()
        time.sleep(0.8)
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


def _try_picamera2() -> Picamera2Capture | None:
    try:
        from picamera2 import Picamera2  # noqa: F401
    except ImportError:
        log.debug("picamera2 not installed – skip Pi Camera Module path")
        return None

    log.info("Trying Picamera2 (Pi Camera Module / libcamera)…")
    try:
        cap = Picamera2Capture()
        ret, frame = cap.read()
        if ret and frame is not None and frame.size > 0:
            log.info("Successfully opened Pi Camera via Picamera2")
            return cap
        cap.release()
    except Exception as e:
        log.info("Picamera2 unavailable: %s", e)
    return None


def _try_gstreamer() -> cv2.VideoCapture | None:
    gst_pipeline = (
        f"libcamerasrc ! video/x-raw,width={CAMERA_WIDTH},height={CAMERA_HEIGHT},"
        f"framerate=30/1 ! videoconvert ! video/x-raw,format=BGR ! "
        "appsink drop=true max-buffers=1 sync=false"
    )
    log.info("Trying GStreamer pipeline for Pi Camera Module…")
    try:
        cap = cv2.VideoCapture(gst_pipeline, cv2.CAP_GSTREAMER)
        if cap.isOpened() and _verify_capture(cap, "GStreamer/libcamerasrc", warmup=15):
            log.info("Successfully opened Pi Camera via GStreamer")
            return cap
        if cap.isOpened():
            cap.release()
    except Exception as e:
        log.debug("GStreamer Pi Camera failed: %s", e)
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


def _open_pi_camera() -> Picamera2Capture | cv2.VideoCapture | None:
    """Pi Camera Module: Picamera2, then GStreamer/libcamerasrc."""
    cap = _try_picamera2()
    if cap is not None:
        return cap
    return _try_gstreamer()


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

    # auto: USB first, then Pi cam
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
