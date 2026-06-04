# 🗑️ Trash Detection – Raspberry Pi Node

## Folder Structure

```
raspi/
├── detect.py              ← Main YOLO detection loop & streaming hub
├── config.py              ← All settings (ESP32 host/IP, ports, classes)
├── dustbin_api.py         ← Threaded REST API interface & status poller
├── streamer.py            ← Flask MJPEG server & custom ground-station dashboard
├── test_api.py            ← Simple script to test ESP32 connectivity
├── requirements.txt       ← Headless-optimized Python dependencies
├── setup.sh               ← Dynamic one-click system dependencies & daemon installer
├── trash_detection.service← systemd service blueprint (automatically installed by setup.sh)
└── README.md              ← This file
```

---

## 1. Copied Layout Verification
Before running any scripts, ensure you copy the `raspi` directory and your YOLO model (`best.pt`) to the Pi:
1. Copy the `raspi/` folder into your Pi's home directory under `~/trash_detection/raspi/`.
2. Place your `best.pt` file at `~/trash_detection/best.pt` or inside the `raspi` folder directly (`~/trash_detection/raspi/best.pt`).
3. Running `./setup.sh` will dynamically find your paths and generate the configuration.

---

## 2. Dynamic Installation on Raspberry Pi

Run the automated setup script. This installs system libraries, creates a virtual environment, installs headless-optimized Python packages, and registers the autostart daemon:

```bash
cd ~/trash_detection/raspi
chmod +x setup.sh
./setup.sh
```

> [!NOTE]
> The `setup.sh` script dynamically identifies your Pi's active path and username (`$(whoami)`). It configures the systemd service to match your configuration automatically, regardless of whether your user is `pi`, `admin`, or a custom name.

---

## 3. Configure

Edit **`config.py`** to match your network:

| Setting | What to change |
|---|---|
| `ESP32_HOST` | Hostname or IP of the ESP32 controller (default `dustbin-controller-team-infyra.local`) |
| `ESP32_ENABLED` | Set `False` for camera stream + YOLO only (no ESP32 on network) |
| **`CAMERA_TYPE`** | **`"usb"`** or **`"pi"`** or **`"auto"`** — see table below |
| `USB_CAMERA_INDEX` | USB device index (`0`, `1`, …) when `CAMERA_TYPE` is `"usb"` or `"auto"` |
| `CAMERA_WIDTH` / `CAMERA_HEIGHT` | Stream + detection resolution (default 640×480) |

### Camera presets (same stream URLs for both)

| Hardware | `config.py` | One-line run |
|---|---|---|
| **USB webcam** | `CAMERA_TYPE = "usb"` and `USB_CAMERA_INDEX = 0` | `CAMERA_TYPE=usb python detect.py` |
| **Pi Camera Module** | `CAMERA_TYPE = "pi"` | `CAMERA_TYPE=pi python detect.py` |
| **Not sure / either** | `CAMERA_TYPE = "auto"` | tries USB first, then Pi cam |

Stream endpoints (unchanged for USB and Pi cam):

- Dashboard: `http://<raspi-ip>:5000/`
- MJPEG: `http://<raspi-ip>:5000/video_feed`
- WebSocket (lower latency): `ws://<raspi-ip>:5000/ws/video`
| `LID_OPEN_DURATION` | Seconds lid stays open after last detection |
| `LEVEL_POLL_INTERVAL` | How often to poll fill level (seconds) |
| `STREAM_PORT` | Port for ground-station dashboard (default 5000) |

---

## 4. Dustbin API Contract

The single ESP32-S3 controller exposes these REST endpoints:

### General

| Method | Endpoint | Action |
|---|---|---|
| `GET` | `/ping` | Health check + basic controller configuration status |

### Paper Dustbin (`http://<esp32-host-or-ip>/api/dustbin/paper`)

| Method | Endpoint | Action |
|---|---|---|
| `POST` | `/api/dustbin/paper/open` | Open lid to paper angle |
| `POST` | `/api/dustbin/paper/close` | Close lid to paper angle |
| `GET`  | `/api/dustbin/paper/level` | Returns `{"level": 45}` (0–100 %) |
| `GET`  | `/api/dustbin/paper/status` | Extended bin status & active angles |
| `POST` | `/api/dustbin/paper/config` | Configures paper angles (`?open=X&close=Y`) |

### Plastic Dustbin (`http://<esp32-host-or-ip>/api/dustbin/plastic`)

| Method | Endpoint | Action |
|---|---|---|
| `POST` | `/api/dustbin/plastic/open` | Open lid to plastic angle |
| `POST` | `/api/dustbin/plastic/close` | Close lid to plastic angle |
| `GET`  | `/api/dustbin/plastic/level` | Returns `{"level": 20}` (0–100 %) |
| `GET`  | `/api/dustbin/plastic/status` | Extended bin status & active angles |
| `POST` | `/api/dustbin/plastic/config` | Configures plastic angles (`?open=X&close=Y`) |

---

## 5. Run & Test

First, verify that your Raspberry Pi can communicate with the ESP32 controller on your network:
```bash
source ~/trash_detection/venv/bin/activate
cd ~/trash_detection/raspi
python test_api.py
```

If the connection passes, start the main detection loop:
```bash
python detect.py
```

Detection classes monitored:
- **Class 2 – paper** → opens paper dustbin lid (to configured paper open angle)
- **Class 3 – plastic** → opens plastic dustbin lid (to configured plastic open angle)

---

## 6. Ground-Station Dashboard

Once `detect.py` is running, access the user interface from any device on the same local network:

| URL | Description |
|---|---|
| `http://<raspi-ip>:5000/` | 🖥️ Full live control ground-station dashboard |
| `http://<raspi-ip>:5000/video_feed` | 📡 MJPEG feed (fallback) |
| `ws://<raspi-ip>:5000/ws/video` | 📡 WebSocket JPEG feed (lower latency; used by dashboard) |
| `http://<raspi-ip>:5000/status` | 📊 JSON dustbin status payload |

The dashboard displays:
* Live camera stream with YOLO bounding boxes.
* Bin fill levels with dynamic color indicators.
* **Real-time Servo Tuner**: Sliders to adjust the open/close degrees of each lid on-the-fly.
* System events console.

---

## 7. Auto-start on Boot (systemd)

Start the systemd service installed during setup to run the program in the background forever:

```bash
# Start background service
sudo systemctl start trash_detection

# Check daemon logs and status
sudo systemctl status trash_detection
journalctl -u trash_detection -f
```

---

## 8. Camera troubleshooting

If logs show **"Successfully opened camera"** then endless **"Frame grab failed"**:

1. The old code treated `isOpened()` as success without reading a frame. Pull the latest `camera.py` / `detect.py` — every backend now **must pass a real frame** before starting.
2. **Pi Camera Module**: set `CAMERA_TYPE = "pi"` (not USB index `0`). Install Picamera2: `sudo apt install -y python3-picamera2` (included in `setup.sh`).
3. **USB webcam**: set `CAMERA_TYPE = "usb"`, run `v4l2-ctl --list-devices`, set `USB_CAMERA_INDEX` to the right `/dev/videoN`.
4. **Stream without ESP32**: set `ESP32_ENABLED = False` to stop mDNS/API noise; the dashboard at `:5000/` still streams.
5. Check one frame manually:
   ```bash
   libcamera-hello -t 2000          # Pi cam
   # or
   v4l2-ctl --device=/dev/video0 --stream-mmap --stream-count=5
   ```

The dashboard prefers **WebSocket** (`/ws/video`) for lower latency than MJPEG. Full **WebRTC** would need a separate signaling server (`aiortc`); WebSocket JPEG is enough for most LAN dashboards.

---

## 9. Class Reference (data.yaml)

| Index | Name | Action |
|---|---|---|
| 0 | can | not monitored |
| 1 | glass | not monitored |
| **2** | **paper** | **→ paper lid open** |
| **3** | **plastic** | **→ plastic lid open** |
| 4 | trash | not monitored |
