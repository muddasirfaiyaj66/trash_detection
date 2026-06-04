# ─────────────────────────────────────────────────────────────────────────────
# config.py  –  Raspberry Pi – Trash Detection System
# ─────────────────────────────────────────────────────────────────────────────

# ── Model ─────────────────────────────────────────────────────────────────────
MODEL_PATH     = "best.pt"   # copy best.pt into the raspi/ folder
CONFIDENCE     = 0.6
LINE_WIDTH     = 2

# Class indices from data.yaml:
#   0=can | 1=glass | 2=paper | 3=plastic | 4=trash
DETECT_CLASSES = [2, 3]
CLASS_NAMES    = {2: "paper", 3: "plastic"}

# ── Camera ────────────────────────────────────────────────────────────────────
CAMERA_SOURCE  = 0          # 0 = /dev/video0 (USB cam); change to Pi Camera RTSP if needed
STREAM_PORT    = 5000
STREAM_HOST    = "0.0.0.0"

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
MJPEG_QUALITY = 80
MJPEG_MAX_FPS = 25
