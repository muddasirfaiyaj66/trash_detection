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

# USB webcam (/dev/video0, /dev/video1, … — run: v4l2-ctl --list-devices)
USB_CAMERA_INDEX = int(os.environ.get("USB_CAMERA_INDEX", "0"))

# Resolution — 16:9 gives a wider field of view than 4:3 on Pi Camera
CAMERA_WIDTH   = int(os.environ.get("CAMERA_WIDTH", "1280"))
CAMERA_HEIGHT  = int(os.environ.get("CAMERA_HEIGHT", "720"))
CAMERA_WARMUP_FRAMES = 15
# Pi Camera: use full sensor crop (widest view, less “zoomed in”)
CAMERA_WIDE_FOV = os.environ.get("CAMERA_WIDE_FOV", "true").lower() in ("1", "true", "yes")

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
INFERENCE_FPS   = int(os.environ.get("INFERENCE_FPS", "8"))
YOLO_IMGSZ      = int(os.environ.get("YOLO_IMGSZ", "320"))   # smaller = faster on Pi
MJPEG_QUALITY   = int(os.environ.get("MJPEG_QUALITY", "70"))
MJPEG_MAX_FPS   = STREAM_FPS
