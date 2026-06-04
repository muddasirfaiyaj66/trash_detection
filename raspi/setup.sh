#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup.sh  –  One-shot setup script for Raspberry Pi
# Run once after copying the raspi/ folder to the Pi
# ─────────────────────────────────────────────────────────────────────────────
set -e

RASPI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$RASPI_DIR")"
VENV_DIR="$PROJECT_DIR/venv"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   Trash Detection – Raspberry Pi Setup           ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "📁 Project dir : $PROJECT_DIR"
echo "📁 Raspi dir   : $RASPI_DIR"
echo "📁 Venv dir    : $VENV_DIR"
echo ""

# ── System dependencies ───────────────────────────────────────────────────────
echo "▶ Installing system dependencies…"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3-venv python3-pip \
    libopencv-dev python3-opencv \
    libatlas-base-dev libhdf5-dev \
    libjpeg-dev libpng-dev \
    v4l-utils \
    gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good libgstreamer1.0-dev \
    python3-picamera2 python3-libcamera

# ── Virtual environment ───────────────────────────────────────────────────────
echo "▶ Creating Python virtual environment at $VENV_DIR …"
# --system-site-packages: picamera2 from apt is visible inside the venv
python3 -m venv --system-site-packages "$VENV_DIR"
source "$VENV_DIR/bin/activate"

echo "▶ Upgrading pip…"
pip install --upgrade pip --quiet

# ── Python packages ───────────────────────────────────────────────────────────
echo "▶ Installing Python requirements…"
pip install -r "$RASPI_DIR/requirements.txt"

# ── Copy model if not present ─────────────────────────────────────────────────
if [ ! -f "$RASPI_DIR/best.pt" ]; then
    if [ -f "$PROJECT_DIR/best.pt" ]; then
        echo "▶ Copying best.pt → $RASPI_DIR/best.pt"
        cp "$PROJECT_DIR/best.pt" "$RASPI_DIR/best.pt"
    else
        echo "⚠  WARNING: best.pt not found! Copy it manually to $RASPI_DIR/best.pt"
    fi
else
    echo "✓  best.pt already present"
fi

# ── systemd service ───────────────────────────────────────────────────────────
echo "▶ Installing systemd service…"

# Patch the service file with the correct paths
SERVICE_CONTENT="[Unit]
Description=Trash Detection YOLO Node
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$RASPI_DIR
ExecStart=$VENV_DIR/bin/python detect.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target"

echo "$SERVICE_CONTENT" | sudo tee /etc/systemd/system/trash_detection.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable trash_detection

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   ✅ Setup complete!                             ║"
echo "╠══════════════════════════════════════════════════╣"
echo "║  1. Edit config.py with your ESP32 Host/IP       ║"
echo "║  2. Test:  python test_api.py                    ║"
echo "║  3. Run:   python detect.py                      ║"
echo "║  4. Start service: sudo systemctl start          ║"
echo "║              trash_detection                     ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
