# ─────────────────────────────────────────────────────────────────────────────
# camera.py  –  Reliable camera open + frame capture for Raspberry Pi
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import site
import time
import logging
import threading
import subprocess
from glob import glob
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
    CAMERA_WIDE_FOV,
    CAMERA_FLIP_H,
    CAMERA_FLIP_V,
    CAMERA_ROTATE,
    STREAM_FPS,
    USB_FOURCC,
    USB_V4L2_SET_FORMAT,
    USB_GSTREAMER,
)

log = logging.getLogger(__name__)

# ── Runtime camera mode (switchable live from the dashboard) ──────────────────
_mode_lock = threading.Lock()
_runtime_mode = {"type": (CAMERA_TYPE or "usb").lower(), "usb_index": USB_CAMERA_INDEX}
_switch_event = threading.Event()


def set_camera_mode(cam_type: str, usb_index=None) -> dict:
    """Request a live switch to 'usb', 'pi', or 'auto'. Capture thread applies it."""
    cam_type = (cam_type or "").lower()
    if cam_type not in ("usb", "pi", "auto"):
        raise ValueError("camera type must be 'usb', 'pi', or 'auto'")
    with _mode_lock:
        _runtime_mode["type"] = cam_type
        if usb_index is not None:
            _runtime_mode["usb_index"] = int(usb_index)
        snapshot = dict(_runtime_mode)
    _switch_event.set()
    log.info("Camera switch requested → %s (usb_index=%s)", cam_type, snapshot["usb_index"])
    return snapshot


def get_camera_mode() -> dict:
    with _mode_lock:
        return dict(_runtime_mode)


def consume_switch_request() -> bool:
    """True (once) if a switch was requested; clears the flag."""
    if _switch_event.is_set():
        _switch_event.clear()
        return True
    return False


def _resolve_source():
    mode = get_camera_mode()
    if mode["type"] == "pi":
        return "libcamera"
    return mode["usb_index"]


# ── Runtime image orientation (flip / rotate, live-adjustable) ────────────────
_transform_lock = threading.Lock()
_transform = {
    "flip_h": bool(CAMERA_FLIP_H),
    "flip_v": bool(CAMERA_FLIP_V),
    "rotate": int(CAMERA_ROTATE) % 360,
}


def set_camera_transform(flip_h=None, flip_v=None, rotate=None) -> dict:
    """Update flip/rotate. rotate must be 0/90/180/270. Returns the new state."""
    with _transform_lock:
        if flip_h is not None:
            _transform["flip_h"] = bool(flip_h)
        if flip_v is not None:
            _transform["flip_v"] = bool(flip_v)
        if rotate is not None:
            r = int(rotate) % 360
            if r not in (0, 90, 180, 270):
                raise ValueError("rotate must be 0, 90, 180, or 270")
            _transform["rotate"] = r
        return dict(_transform)


def get_camera_transform() -> dict:
    with _transform_lock:
        return dict(_transform)


def apply_transform(frame):
    """Apply the current rotate + flip to a BGR frame (cheap, in capture thread)."""
    with _transform_lock:
        fh, fv, rot = _transform["flip_h"], _transform["flip_v"], _transform["rotate"]
    if rot == 90:
        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    elif rot == 180:
        frame = cv2.rotate(frame, cv2.ROTATE_180)
    elif rot == 270:
        frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if fh and fv:
        frame = cv2.flip(frame, -1)
    elif fh:
        frame = cv2.flip(frame, 1)
    elif fv:
        frame = cv2.flip(frame, 0)
    return frame

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
    """
    Configure a USB/V4L2 device for high FPS + low latency.

    ORDER MATTERS: FOURCC (MJPG) must be set BEFORE width/height/fps, otherwise
    V4L2 keeps the slow raw YUYV format and the camera is capped at ~5 FPS.
    """
    # 1. Pixel format first — MJPG unlocks 30 FPS on most USB webcams
    if USB_FOURCC:
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*USB_FOURCC))
        except Exception:
            pass
    # 2. Resolution
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    # 3. Frame rate
    try:
        cap.set(cv2.CAP_PROP_FPS, STREAM_FPS)
    except Exception:
        pass
    # 4. Tiny buffer so we always read the freshest frame (low latency)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    # Reduce autofocus overhead if supported (ignored by cams without AF)
    try:
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
    except Exception:
        pass
    _log_negotiated(cap)


def _log_negotiated(cap: cv2.VideoCapture) -> None:
    """Log what the driver actually negotiated — key for diagnosing low FPS."""
    try:
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        code = int(cap.get(cv2.CAP_PROP_FOURCC))
        fourcc = "".join(chr((code >> (8 * i)) & 0xFF) for i in range(4)).strip() if code else "?"
        log.info("Camera negotiated: %dx%d @ %.0f fps, format=%s", w, h, fps, fourcc or "?")
        if fourcc and fourcc not in ("MJPG", "H264") and fps and fps <= 10:
            log.warning(
                "Camera is in '%s' (raw) mode at %.0f fps. For higher FPS this cam "
                "may need a lower resolution, or it may not support MJPG.",
                fourcc, fps,
            )
    except Exception:
        pass


def _verify_capture(cap, label: str, warmup: int = CAMERA_WARMUP_FRAMES) -> bool:
    """
    OpenCV often reports isOpened()=True before the device delivers frames.
    Discard warmup frames and require at least one valid buffer before trusting it.
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
        if CAMERA_WIDE_FOV:
            self._apply_wide_fov()
        time.sleep(1.0)
        self._opened = True

    def _apply_wide_fov(self):
        """Use the widest sensor crop so the stream is not overly zoomed."""
        try:
            props = self._picam.camera_properties
            crop = props.get("ScalerCropMaximum")
            if crop:
                self._picam.set_controls({"ScalerCrop": crop})
                log.info("Pi camera wide FOV enabled (ScalerCropMaximum)")
        except Exception as e:
            log.debug("Wide FOV not applied: %s", e)

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
        from config import USB_FRAME_TIMEOUT
        deadline = time.time() + USB_FRAME_TIMEOUT
        while time.time() < deadline:
            jpeg = self._read_jpeg()
            if jpeg:
                frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if frame is not None and frame.size > 0:
                    return True, frame
            time.sleep(0.01)
        log.warning("rpicam read timeout (%.1f s) — camera may be stalled", USB_FRAME_TIMEOUT)
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


def _list_v4l2_indices() -> list[int]:
    """Discover available /dev/videoN indices dynamically."""
    found = []
    for dev in glob("/dev/video*"):
        suffix = dev.rsplit("video", 1)[-1]
        if suffix.isdigit():
            found.append(int(suffix))
    return sorted(set(found))


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


def _v4l2_can_capture(device: str) -> bool:
    """
    True if `device` is a real video-capture node (not metadata / Orbbec depth).
    Uses v4l2-ctl when available; otherwise assumes True (probing will confirm).
    """
    if not which("v4l2-ctl"):
        return True
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", device, "--all"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        if "Video Capture" not in out:
            return False
        # Nodes that only expose metadata report no pixel formats we can use.
        return True
    except Exception:
        return True


def _v4l2_supports_mjpg(device: str) -> bool:
    if not which("v4l2-ctl"):
        return True
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", device, "--list-formats"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        return "MJPG" in out or "Motion-JPEG" in out
    except Exception:
        return True


def _v4l2_set_format(device: str) -> None:
    """
    Force resolution + MJPG + FPS at the DRIVER level before OpenCV opens it.
    This is the most reliable fix: OpenCV's FFMPEG backend ignores CAP_PROP_*
    and otherwise leaves the camera in slow 1080p raw mode (~5 FPS).
    """
    if not (USB_V4L2_SET_FORMAT and which("v4l2-ctl")):
        return
    fmt = (USB_FOURCC or "MJPG") if _v4l2_supports_mjpg(device) else "YUYV"
    try:
        subprocess.run(
            ["v4l2-ctl", "-d", device,
             f"--set-fmt-video=width={CAMERA_WIDTH},height={CAMERA_HEIGHT},pixelformat={fmt}"],
            check=False, timeout=3,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["v4l2-ctl", "-d", device, f"--set-parm={STREAM_FPS}"],
            check=False, timeout=3,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log.info("v4l2-ctl forced %s → %dx%d %s @%d fps",
                 device, CAMERA_WIDTH, CAMERA_HEIGHT, fmt, STREAM_FPS)
    except Exception as e:
        log.debug("v4l2-ctl set-fmt failed on %s: %s", device, e)


def _open_usb_gstreamer(device: str) -> cv2.VideoCapture | None:
    """Force MJPG@30 via a GStreamer v4l2src pipeline (most reliable for high FPS)."""
    if not (USB_GSTREAMER and _opencv_has_gstreamer()):
        return None
    pipelines = [
        (
            f"v4l2src device={device} ! image/jpeg,width={CAMERA_WIDTH},"
            f"height={CAMERA_HEIGHT},framerate={STREAM_FPS}/1 ! jpegdec ! "
            "videoconvert ! video/x-raw,format=BGR ! appsink drop=true max-buffers=1 sync=false",
            f"USB GStreamer MJPG {device}",
        ),
        (
            f"v4l2src device={device} ! video/x-raw,width={CAMERA_WIDTH},"
            f"height={CAMERA_HEIGHT} ! videoconvert ! video/x-raw,format=BGR ! "
            "appsink drop=true max-buffers=1 sync=false",
            f"USB GStreamer raw {device}",
        ),
    ]
    for pipeline, label in pipelines:
        log.info("Trying %s…", label)
        try:
            cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
            if cap.isOpened() and _verify_capture(cap, label, warmup=20):
                log.info("Successfully opened USB camera via %s", label)
                return cap
            if cap.isOpened():
                cap.release()
        except Exception as e:
            log.debug("%s failed: %s", label, e)
    return None


def _try_v4l2_path(device: str) -> cv2.VideoCapture | None:
    """Open a /dev/videoN node with the V4L2 backend + MJPG config."""
    if not os.path.exists(device):
        return None
    log.info("Trying V4L2 backend on %s…", device)
    try:
        cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        if cap.isOpened() and _verify_capture(cap, f"V4L2 {device}"):
            log.info("Successfully opened USB camera: %s (V4L2)", device)
            return cap
        if cap.isOpened():
            cap.release()
    except Exception as e:
        log.debug("V4L2 open failed on %s: %s", device, e)
    return None


def _usb_device_candidates(idx: int) -> list[str]:
    """Ordered /dev/videoN paths to try, real capture nodes first."""
    discovered = _list_v4l2_indices()
    order: list[int] = []
    if idx in discovered or os.path.exists(f"/dev/video{idx}"):
        order.append(idx)
    order += [n for n in discovered if n != idx]
    if not order:
        order = [idx, 0, 1, 2, 3, 4]
    # Prefer nodes that v4l2-ctl reports as actual capture devices.
    paths = [f"/dev/video{n}" for n in order]
    capture = [p for p in paths if _v4l2_can_capture(p)]
    rest = [p for p in paths if p not in capture]
    return capture + rest


def _open_usb_camera(idx: int = USB_CAMERA_INDEX) -> cv2.VideoCapture | None:
    """
    USB webcam, robust against Pi quirks (Orbbec nodes, FFMPEG backend, 1080p
    raw lock). For each real capture node we:
      1. force MJPG/res/fps at driver level with v4l2-ctl
      2. try GStreamer MJPG pipeline (best 30 FPS path)
      3. try OpenCV V4L2 backend (honours MJPG)
      4. fall back to default backend (last resort, may be slow)
    """
    candidates = _usb_device_candidates(idx)
    log.info("USB capture candidates: %s", candidates)

    for device in candidates:
        _v4l2_set_format(device)

        cap = _open_usb_gstreamer(device)
        if cap is not None:
            return cap

        cap = _try_v4l2_path(device)
        if cap is not None:
            return cap

    # Last resort: default backend by index (e.g. FFMPEG). Driver format was
    # already forced above so this may now read MJPG instead of slow raw.
    for device in candidates:
        suffix = device.rsplit("video", 1)[-1]
        if not suffix.isdigit():
            continue
        cap = _try_index(int(suffix), None, f"default USB backend {device}")
        if cap is not None:
            return cap

    log.error(
        "USB camera not found/readable. Diagnose with:\n"
        "  v4l2-ctl --list-devices\n"
        "  v4l2-ctl -d /dev/videoN --list-formats-ext\n"
        "then set USB_CAMERA_INDEX=N (the node that lists MJPG/Video Capture)."
    )
    return None


def initialize_camera(source=None):
    """
    Open a camera and confirm frames are readable.
    Honours the live runtime mode (usb | pi | auto) set from the dashboard.
    """
    if source is None:
        source = _resolve_source()

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

    mode = get_camera_mode()["type"]
    usb_index = int(source) if str(source).isdigit() else get_camera_mode()["usb_index"]
    log.info("Camera mode: %s (stream %dx%d, usb_index=%d)", mode, CAMERA_WIDTH, CAMERA_HEIGHT, usb_index)

    if mode == "pi":
        return _open_pi_camera()

    if mode == "usb":
        return _open_usb_camera(usb_index)

    # auto: USB first, then Pi cam
    cap = _open_usb_camera(usb_index)
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
    return initialize_camera()
