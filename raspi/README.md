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
| `CAMERA_SOURCE` | `0` for USB cam, `1` for Pi Camera v3, or RTSP URL |
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
| `http://<raspi-ip>:5000/video_feed` | 📡 Raw MJPEG camera feed annotated by YOLO |
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

## 8. Class Reference (data.yaml)

| Index | Name | Action |
|---|---|---|
| 0 | can | not monitored |
| 1 | glass | not monitored |
| **2** | **paper** | **→ paper lid open** |
| **3** | **plastic** | **→ plastic lid open** |
| 4 | trash | not monitored |
