# 🤖 ESP32-S3 Dual Dustbin Controller

## One board. Two dustbins. One IP (or Hostname).

A single ESP32-S3 controls both the **paper** (class 2) and **plastic** (class 3) dustbins simultaneously. The Raspberry Pi sends HTTP requests to one hostname `dustbin-controller-team-infyra.local` (or its DHCP-assigned IP address) — the path determines which bin responds.

---

## Files

```
esp32/dustbin_firmware/
├── dustbin_firmware.ino   ← Complete firmware (open this in Arduino IDE)
└── README.md              ← This file
```

All configuration is at the **top of `dustbin_firmware.ino`** — no separate header needed.

---

## Hardware Wiring

### Components
| Qty | Component | Role |
|---|---|---|
| 1 | ESP32-S3 Dev Board | Wi-Fi + REST API server |
| 2 | SG90 / MG996R Servo | One per lid (paper + plastic) |
| 2 | HC-SR04 Ultrasonic | One per bin, mounted on lid facing down |
| 1 | 5V 2A PSU (external) | Powers both servos safely |

### Pin Map

| ESP32-S3 GPIO | Connected to |
|---|---|
| **GPIO 5** | Paper servo — signal (orange wire) |
| **GPIO 6** | Plastic servo — signal (orange wire) |
| **GPIO 12** | Paper HC-SR04 — TRIG |
| **GPIO 13** | Paper HC-SR04 — ECHO |
| **GPIO 14** | Plastic HC-SR04 — TRIG |
| **GPIO 15** | Plastic HC-SR04 — ECHO |
| **GPIO 2** | Status LED (built-in) |
| **5V / VIN** | Both servo red wires (external 5V 2A rail) |
| **3V3** | Both HC-SR04 VCC pins |
| **GND** | Common ground — ESP32 + servos + sensors |

```
         ESP32-S3
        ┌─────────┐
  GPIO5 ─── Paper Servo (signal)
  GPIO6 ─── Plastic Servo (signal)
        │
 GPIO12 ─── Paper HC-SR04 TRIG
 GPIO13 ─── Paper HC-SR04 ECHO
        │
 GPIO14 ─── Plastic HC-SR04 TRIG
 GPIO15 ─── Plastic HC-SR04 ECHO
        │
    3V3 ─── HC-SR04 VCC (both)
    GND ─── GND (all components)
   5Vext─── Servo VCC (both)  ← use external 5V, NOT ESP32 5V pin
        └─────────┘
```

> ⚠️ **Servo power warning**: MG996R draws up to 1A stall. Always use an external 5V rail for servos and tie its GND to ESP32 GND. SG90 can use 3.3V but is weak — 5V is preferred.

> ⚠️ **HC-SR04 ECHO line**: HC-SR04 outputs 5V on ECHO. Use a 1kΩ/2kΩ voltage divider to bring it to 3.3V before GPIO, or use a JSN-SR04T which is 3.3V native.

---

## Ultrasonic Mounting

Mount each **HC-SR04 inside the lid**, pointed straight down into the bin:

```
  ╔═══════════════════════╗  ← Lid (servo-controlled)
  ║  [HC-SR04 sensor]     ║  ← Mounted here, facing DOWN
  ╚═══════════════════════╝
          ↕  measures distance
  ┌───────────────────────┐
  │    trash contents     │
  │                       │
  └───────────────────────┘  ← Bin bottom
 
  Empty bin  → distance ≈ BIN_EMPTY_CM (default 35 cm)
  Full bin   → distance ≈ BIN_FULL_CM  (default 3 cm)
```

Adjust these in the firmware config block:
```cpp
#define BIN_EMPTY_CM   35.0f   // sensor to bottom when empty
#define BIN_FULL_CM     3.0f   // sensor to trash when full
```

---

## Arduino IDE Setup

### 1. Board Support
- *File → Preferences → Additional Boards Manager URLs:*
  ```
  https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
  ```
- *Tools → Boards Manager* → install **esp32 by Espressif Systems** ≥ 3.0

### 2. Required Libraries (Library Manager)
| Library | Author | Notes |
|---|---|---|
| **ESP32Servo** | Kevin Harrington | |
| **ESPAsyncWebServer** | Me-No-Dev | May need GitHub install |
| **AsyncTCP** | Me-No-Dev | Required by above |
| **ArduinoJson** | Benoit Blanchon | v7+ required |

> If ESPAsyncWebServer isn't in Library Manager, install from:
> https://github.com/me-no-dev/ESPAsyncWebServer

### 3. Board Settings
| Setting | Value |
|---|---|
| Board | **ESP32S3 Dev Module** |
| Upload Speed | 921600 |
| USB Mode | Hardware CDC and JTAG |
| Flash Size | 4MB (match your board) |
| PSRAM | Disabled |

---

## Configuration (inside `dustbin_firmware.ino`)

Find the `★ CONFIGURATION ★` block near the top and edit:

```cpp
// ── WiFi Networks ─────────────────────────────────────────────────────────
// Add multiple networks. The board joins whichever is available.
const WifiCredential WIFI_NETWORKS[] = {
    { "HomeWiFi_SSID",     "HomeWiFiPassword"     },  // ← primary network
    { "LabWiFi_SSID",      "LabWiFiPassword"       },  // ← secondary network
    { "MobileHotspot_SSID","MobileHotspotPassword" },  // ← phone hotspot
};

// mDNS hostname — reached at http://dustbin-controller-team-infyra.local
#define HOSTNAME          "dustbin-controller-team-infyra"

// Adjust to your bin dimensions
#define BIN_EMPTY_CM  35.0f
#define BIN_FULL_CM    3.0f
```

---

## REST API Reference

All endpoints respond on port 80 using hostname **`http://dustbin-controller-team-infyra.local`** (or the DHCP IP address):

### Paper Dustbin (class 2)
| Method | Endpoint | Response |
|---|---|---|
| `GET` | `/api/dustbin/paper/level` | `{"level":45,"distance_cm":"19.2","lid":"closed","bin":"paper"}` |
| `POST` | `/api/dustbin/paper/open` | `{"status":"ok","action":"open","lid":"open","bin":"paper"}` |
| `POST` | `/api/dustbin/paper/close` | `{"status":"ok","action":"close","lid":"closed","bin":"paper"}` |
| `GET` | `/api/dustbin/paper/status` | `{"bin":"paper","lid":"closed","level":40,...,"open_deg":90,"close_deg":0}` |
| `POST` | `/api/dustbin/paper/config` | Configures servo degrees dynamically. Ex: `/config?open=100&close=5` |

### Plastic Dustbin (class 3)
| Method | Endpoint | Response |
|---|---|---|
| `GET` | `/api/dustbin/plastic/level` | `{"level":20,"distance_cm":"28.0","lid":"closed","bin":"plastic"}` |
| `POST` | `/api/dustbin/plastic/open` | `{"status":"ok","action":"open","lid":"open","bin":"plastic"}` |
| `POST` | `/api/dustbin/plastic/close` | `{"status":"ok","action":"close","lid":"closed","bin":"plastic"}` |
| `GET` | `/api/dustbin/plastic/status` | `{"bin":"plastic","lid":"closed","level":10,...,"open_deg":90,"close_deg":0}` |
| `POST` | `/api/dustbin/plastic/config` | Configures servo degrees dynamically. Ex: `/config?open=100&close=5` |

### Health
| Method | Endpoint | Response |
|---|---|---|
| `GET` | `/ping` | Health payload containing state, active networks, and active open/close degrees for both bins. |

---

## Communication Flow

```
┌─────────────────────────────────────────────────────────────┐
│                        Wi-Fi network                        │
│                                                             │
│  ┌─────────────────────┐    HTTP @                           │
│  │   Raspberry Pi      │    dustbin-controller-team-infyra   │
│  │   (detect.py)       │    .local                           │
│  │                     │──POST /api/dustbin/paper/open ──►  │
│  │  YOLO class 2       │◄── {"status":"ok","lid":"open"} ── │
│  │  → paper bin        │                                    │
│  │                     │──POST /api/dustbin/plastic/open ─► │ ┌──────────────────────┐
│  │  YOLO class 3       │◄── {"status":"ok"} ─────────────── │ │   ESP32-S3           │
│  │  → plastic bin      │                                    │ │                      │
│  │                     │──GET /api/dustbin/paper/status ──► │ │  GPIO5 → Paper Servo │
│  │  GET /status poll   │◄── {"level":40,"open_deg":90} ─── │ │  GPIO6 → Plastic Servo│
│  │  every 10s          │                                    │ │  GPIO12/13 → Paper   │
│  └─────────────────────┘──POST /api/dustbin/paper/config ──►│ │           HC-SR04    │
│           │            (via sliders in UI: ?open=95&close=5)│ │  GPIO14/15 → Plastic │
│  ┌────────▼─────────────────────────────────┐              │ │           HC-SR04    │
│  │  Ground Station  http://<raspi-ip>:5000/ │              │ └──────────────────────┘
│  │  Live YOLO feed + dustbin dashboard      │              │
│  └──────────────────────────────────────────┘              │
└─────────────────────────────────────────────────────────────┘
```

---

## Serial Monitor Output (115200 baud)

On power-up you'll see:
```
╔══════════════════════════════════════════╗
║  Dual Dustbin Controller  –  ESP32-S3   ║
║  Paper (class 2)  +  Plastic (class 3)  ║
╚══════════════════════════════════════════╝

[Servo] Paper   GPIO5   → 0° (closed)
[Servo] Plastic GPIO6   → 0° (closed)
[WiFi] Scanning for known networks…
[WiFi] 3 network(s) configured – connecting....
╔══════════════════════════════════════════════════╗
║  Connected to : LabWiFi_SSID                    ║
║  IP address   : 192.168.1.134                   ║
║  mDNS         : dustbin-controller-team-infyra  ║
║                 .local                           ║
║  RSSI         : -52 dBm                         ║
╚══════════════════════════════════════════════════╝
[READY] Pi config → ESP32_HOST = "dustbin-controller-team-infyra.local"
[mDNS]  http://dustbin-controller-team-infyra.local
[HTTP]  Listening on port 80
[HTTP]  11 routes registered
[READY] Waiting for commands…
```

When Pi or UI sends commands:
```
[API] POST /paper/config → {"bin":"paper","open_deg":100,"close_deg":5}
[API] POST /paper/open   → {"status":"ok","action":"open","lid":"open","bin":"paper"}
[paper] Opening lid…
[paper] Lid OPEN ✓
[AUTO-CLOSE] paper – no activity for 8s
[paper] Closing lid…
[paper] Lid CLOSED ✓
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| WiFi connection fails | Check your SSIDs & passwords in `WIFI_NETWORKS`. Must be 2.4 GHz WiFi! |
| Servo stutters / ESP resets | Use **external 5V 2A** power supply for servos, share GND |
| Level always 0% | Check HC-SR04 wiring; check that echo voltage divider is correct |
| Level always 100% | Check that sensor faces straight down, no obstruction within bin |
| mDNS host not resolving | Ensure `avahi-daemon` is running on Raspi, or use the DHCP IP printed on Serial Monitor |
| Auto-close timeout | Change `AUTO_CLOSE_MS` in firmware if default (8s) is too fast/slow |
