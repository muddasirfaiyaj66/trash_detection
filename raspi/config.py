# ─────────────────────────────────────────────────────────────────────────────
# config.py  –  Raspberry Pi – Trash Detection System
# ─────────────────────────────────────────────────────────────────────────────

import os

# ── Model ─────────────────────────────────────────────────────────────────────
MODEL_PATH     = "best.pt"   # copy best.pt into the raspi/ folder
CONFIDENCE     = 0.6
LINE_WIDTH     = 2

# Class indices from data.yaml:
#   0=can | 1=glass | 2=paper | 3=plastic | 4=trash
DETECT_CLASSES = [2, 3]
CLASS_NAMES    = {2: "paper", 3: "plastic"}

# ── Camera (pick ONE type for your hardware) ──────────────────────────────────
#
#   CAMERA_TYPE = "usb"   → USB webcam via /dev/videoN (V4L2)
#   CAMERA_TYPE = "pi"    → Raspberry Pi Camera Module (Picamera2 / libcamera)
#   CAMERA_TYPE = "auto"  → try USB first, then Pi cam (good if unsure)
#
# Override without editing this file:
#   CAMERA_TYPE=pi python detect.py
#
CAMERA_TYPE = os.environ.get("CAMERA_TYPE", "usb")   # "usb" | "pi" | "auto"

# USB webcam node. Run `v4l2-ctl --list-devices` and pick the node whose
# `--list-formats-ext` shows MJPG (NOT the Pi's internal CSI/ISP nodes).
# On this Pi the A4tech USB cam is /dev/video8.
USB_CAMERA_INDEX = int(os.environ.get("USB_CAMERA_INDEX", "8"))

# Resolution — MUST be a size the camera actually advertises for MJPG, otherwise
# the driver falls back to slow raw mode (~5 FPS). The A4tech cam offers MJPG at
# 1920x1080 / 1280x720 / 800x600 @ 30fps (no 640x480 MJPG), so 720p is the sweet
# spot: smooth 30 FPS, good quality, light to JPEG-decode on the Pi.
CAMERA_WIDTH   = int(os.environ.get("CAMERA_WIDTH", "1280"))
CAMERA_HEIGHT  = int(os.environ.get("CAMERA_HEIGHT", "720"))
CAMERA_WARMUP_FRAMES = 15
# Pi Camera: use full sensor crop (widest view, less “zoomed in”)
CAMERA_WIDE_FOV = os.environ.get("CAMERA_WIDE_FOV", "true").lower() in ("1", "true", "yes")

# ── Image orientation (also live-adjustable from the dashboard) ───────────────
#   CAMERA_FLIP_H = mirror left/right, CAMERA_FLIP_V = flip up/down
#   CAMERA_ROTATE = 0 | 90 | 180 | 270  (degrees, clockwise)
CAMERA_FLIP_H = os.environ.get("CAMERA_FLIP_H", "false").lower() in ("1", "true", "yes")
CAMERA_FLIP_V = os.environ.get("CAMERA_FLIP_V", "false").lower() in ("1", "true", "yes")
CAMERA_ROTATE = int(os.environ.get("CAMERA_ROTATE", "0"))

# Legacy alias (detect.py / camera.py read CAMERA_SOURCE from this)
def _camera_source():
    if CAMERA_TYPE == "pi":
        return "libcamera"
    if CAMERA_TYPE == "usb":
        return USB_CAMERA_INDEX
    return USB_CAMERA_INDEX  # auto: USB index first, Pi cam tried in camera.py

CAMERA_SOURCE = _camera_source()

STREAM_PORT    = 5000
STREAM_HOST    = "0.0.0.0"

# Set False to run detection + dashboard stream without the ESP32 on the network
ESP32_ENABLED  = True

# ── ESP32-S3 Dustbin Controller ───────────────────────────────────────────────
#
#  The ESP32-S3 advertises itself via mDNS as "dustbin-controller.local".
#  Raspberry Pi OS resolves .local hostnames automatically (avahi-daemon).
#
#  ┌─ OPTION A (recommended) ─────────────────────────────────────────────────┐
#  │  Use the mDNS hostname — works as long as Pi & ESP32 are on same network │
#  │  No need to know the IP. ESP32 uses DHCP so its IP may change.           │
#  └──────────────────────────────────────────────────────────────────────────┘
ESP32_HOST = "dustbin-controller-team-infyra.local"   # ← mDNS hostname (no change needed)

#  ┌─ OPTION B ────────────────────────────────────────────────────────────────┐
#  │  Use a fixed IP — paste the IP printed on the ESP32 serial monitor here  │
#  │  Uncomment this line and comment out the line above if mDNS doesn't work │
#  └──────────────────────────────────────────────────────────────────────────┘
# ESP32_HOST = "192.168.1.XXX"   # ← paste your ESP32 IP here (from Serial Monitor)

# ── API endpoints (built automatically — do not edit below) ───────────────────
_BASE_PAPER   = f"http://{ESP32_HOST}/api/dustbin/paper"
_BASE_PLASTIC = f"http://{ESP32_HOST}/api/dustbin/plastic"

PAPER_LID_OPEN_EP    = f"{_BASE_PAPER}/open"
PAPER_LID_CLOSE_EP   = f"{_BASE_PAPER}/close"
PAPER_LEVEL_EP       = f"{_BASE_PAPER}/level"
PAPER_STATUS_EP      = f"{_BASE_PAPER}/status"
PAPER_CONFIG_EP      = f"{_BASE_PAPER}/config"

PLASTIC_LID_OPEN_EP  = f"{_BASE_PLASTIC}/open"
PLASTIC_LID_CLOSE_EP = f"{_BASE_PLASTIC}/close"
PLASTIC_LEVEL_EP     = f"{_BASE_PLASTIC}/level"
PLASTIC_STATUS_EP    = f"{_BASE_PLASTIC}/status"
PLASTIC_CONFIG_EP    = f"{_BASE_PLASTIC}/config"

ESP32_PING_EP        = f"http://{ESP32_HOST}/ping"

# ── Timing ────────────────────────────────────────────────────────────────────
LID_OPEN_DURATION   = 5.0    # seconds lid stays open after last detection
LEVEL_POLL_INTERVAL = 10.0   # seconds between fill-level polls
API_TIMEOUT         = 5      # seconds before HTTP request times out

# ── Ground-station stream ─────────────────────────────────────────────────────
# Stream runs at full camera rate; YOLO runs separately so video stays smooth.
STREAM_FPS      = int(os.environ.get("STREAM_FPS", "30"))
INFERENCE_FPS   = int(os.environ.get("INFERENCE_FPS", "15"))  # thread runs as fast as the Pi allows up to this
YOLO_IMGSZ      = int(os.environ.get("YOLO_IMGSZ", "320"))   # smaller = faster on Pi (256 = even faster)
MJPEG_QUALITY   = int(os.environ.get("MJPEG_QUALITY", "70"))
MJPEG_MAX_FPS   = STREAM_FPS

# How long (seconds) the last detection boxes stay drawn on the stream between
# inferences. Higher = smoother boxes when YOLO runs slower than the video.
DETECTION_TTL   = float(os.environ.get("DETECTION_TTL", "1.2"))

# ── Inference acceleration (NCNN) ─────────────────────────────────────────────
# NCNN is an ARM-optimized runtime that is typically 2-3x faster than PyTorch on
# a Raspberry Pi CPU. On first run the model is auto-exported to best_ncnn_model/.
# Needs: pip install ncnn   (export also pulls pnnx automatically). Falls back to
# the .pt model if NCNN is not installed.
USE_NCNN        = os.environ.get("USE_NCNN", "true").lower() in ("1", "true", "yes")

# ── Model Warmup ──────────────────────────────────────────────────────────────
# Run one inference as soon as the first frame arrives so detections start immediately.
MODEL_WARMUP = os.environ.get("MODEL_WARMUP", "true").lower() in ("1", "true", "yes")
MODEL_WARMUP_TIMEOUT = float(os.environ.get("MODEL_WARMUP_TIMEOUT", "3.0"))

# ── USB Camera Optimization ────────────────────────────────────────────────────
# Prevents camera shutdown and low FPS issues on Raspberry Pi USB
USB_FRAME_TIMEOUT = float(os.environ.get("USB_FRAME_TIMEOUT", "5.0"))  # max seconds to wait for a frame
CAMERA_REOPEN_THRESHOLD = int(os.environ.get("CAMERA_REOPEN_THRESHOLD", "30"))  # re-open after N consecutive failures

# MJPG lets most USB webcams hit 30 FPS; raw YUYV/BGR3 is often capped at ~5 FPS.
# Set to "" to skip, or "YUYV" if your specific camera does not support MJPG.
USB_FOURCC = os.environ.get("USB_FOURCC", "MJPG")

# Force the camera format at the V4L2 driver level with `v4l2-ctl` BEFORE OpenCV
# opens it. This is the most reliable fix when OpenCV uses the FFMPEG backend
# (which ignores resolution/FPS requests and leaves the cam in slow 1080p raw).
USB_V4L2_SET_FORMAT = os.environ.get("USB_V4L2_SET_FORMAT", "true").lower() in ("1", "true", "yes")

# Prefer a GStreamer v4l2src MJPG pipeline for USB (forces 30 FPS) when OpenCV
# was built with GStreamer. Falls back automatically if unavailable.
USB_GSTREAMER = os.environ.get("USB_GSTREAMER", "true").lower() in ("1", "true", "yes")
