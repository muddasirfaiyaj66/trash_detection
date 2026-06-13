// ═════════════════════════════════════════════════════════════════════════════
//  dustbin_firmware.ino
//  ESP32-S3  –  Dual Dustbin Controller  (Paper + Plastic)
//
//  Features:
//   • WiFiMulti – tries multiple networks in order, always stays connected
//   • DHCP      – no fixed IP needed; mDNS lets the Pi find this board
//   • mDNS      – reachable as http://dustbin-controller.local
//   • REST API  – paper + plastic open/close/level on port 80
//   • Smooth servo sweep (8 ms per degree)
//   • HC-SR04 ultrasonic fill-level (5-sample median)
//   • Auto-close safety timer (closes lid if Pi goes silent)
//   • Wi-Fi watchdog (auto-reconnects on drop)
//   • Serial monitor prints current IP on every (re)connect
//
//  Hardware wiring (single ESP32-S3):
//   GPIO 5  → Paper servo  signal
//   GPIO 6  → Plastic servo signal
//   GPIO 12 → Paper  HC-SR04 TRIG
//   GPIO 13 → Paper  HC-SR04 ECHO  ← use 1kΩ/2kΩ divider (5V→3.3V)
//   GPIO 14 → Plastic HC-SR04 TRIG
//   GPIO 15 → Plastic HC-SR04 ECHO ← use 1kΩ/2kΩ divider (5V→3.3V)
//   GPIO 2  → Status LED (built-in)
//   5V ext  → Both servo power (external 2A supply, shared GND)
//   3V3     → Both HC-SR04 VCC
//   GND     → Common ground
//
//  Required libraries (Arduino Library Manager):
//   • ESP32Servo        (Kevin Harrington)
//   • ESPAsyncWebServer (Me-No-Dev)
//   • AsyncTCP          (Me-No-Dev)
//   • ArduinoJson       (Benoit Blanchon) v7+
//
//  Board: ESP32S3 Dev Module | USB Mode: Hardware CDC and JTAG
// ═════════════════════════════════════════════════════════════════════════════

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiMulti.h>
#include <ESPmDNS.h>
#include <ESPAsyncWebServer.h>
#include <AsyncTCP.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>

// ╔═════════════════════════════════════════════════════════════════════════╗
// ║              ★  CONFIGURATION  –  ONLY EDIT THIS SECTION  ★           ║
// ╚═════════════════════════════════════════════════════════════════════════╝

// ── WiFi Networks ─────────────────────────────────────────────────────────
//  Add as many networks as you like. The board tries them in order and
//  connects to whichever is available. Great for home + lab + mobile hotspot.
//
//  ⚠  Use 2.4 GHz networks only – ESP32 does not support 5 GHz.
//
struct WifiCredential { const char* ssid; const char* pass; };
const WifiCredential WIFI_NETWORKS[] = {
      { "Motorola edge",  "1234@@@###" },
    { "UIU-STUDENT",    "12345678"   },
    { "Shoikot Sazzad", "11111111"   },
    { "Sagar", "96896061"   },   // ← phone hotspot (backup)
    // Add more rows here if needed:
    // { "AnotherSSID", "AnotherPassword" },
};

// ── mDNS hostname ─────────────────────────────────────────────────────────
//  Raspberry Pi will reach this board at:  http://dustbin-controller.local
//  Change only if you deploy multiple ESP32 controllers on the same network.
#define HOSTNAME          "dustbin-controller-team-infyra"

// ── GPIO: Paper Dustbin ───────────────────────────────────────────────────
#define PAPER_SERVO_PIN   5
#define PAPER_TRIG_PIN    12
#define PAPER_ECHO_PIN    13

// ── GPIO: Plastic Dustbin ─────────────────────────────────────────────────
#define PLASTIC_SERVO_PIN 6
#define PLASTIC_TRIG_PIN  14
#define PLASTIC_ECHO_PIN  15

// ── Servo angles (per bin) ──────────────────────────────────────────────────
//  Each bin can have its own open/close angles (calibrate to your mechanics).
#define PAPER_OPEN_DEG     0     // Paper   lid OPEN  position
#define PAPER_CLOSE_DEG    134   // Paper   lid CLOSED position
#define PLASTIC_OPEN_DEG   45    // Plastic lid OPEN  position
#define PLASTIC_CLOSE_DEG  168   // Plastic lid CLOSED position
#define SERVO_STEP_MS      8     // ms per 1° when CLOSING — gentle (min ~4)
#define SERVO_OPEN_STEP_MS 2     // ms per 1° when OPENING — fast so the lid pops open instantly

// ── Bin geometry (cm) – defaults; per-bin values adjustable via dashboard ───
#define PAPER_EMPTY_CM    19.0f  // sensor→bottom when empty
#define PAPER_FULL_CM      2.0f  // sensor→trash when full
#define PLASTIC_EMPTY_CM  22.0f
#define PLASTIC_FULL_CM    2.0f

// ── Ultrasonic samples (median filter) ───────────────────────────────────
#define ULTRA_SAMPLES     5

// ── Auto-close safety (ms) ───────────────────────────────────────────────
//  Backup if Pi stops sending /open. Must exceed dashboard max (60 s).
#define AUTO_CLOSE_MS     65000  // 65 seconds

// ── Status LED ────────────────────────────────────────────────────────────
#define LED_PIN           2

// ── Web server port ───────────────────────────────────────────────────────
#define HTTP_PORT         80

// ╔═════════════════════════════════════════════════════════════════════════╗
// ║                    END OF CONFIGURATION                                ║
// ╚═════════════════════════════════════════════════════════════════════════╝


// ─────────────────────────────────────────────────────────────────────────────
//  Globals
// ─────────────────────────────────────────────────────────────────────────────
WiFiMulti       wifiMulti;
AsyncWebServer  server(HTTP_PORT);

enum LidState { LID_CLOSED, LID_OPENING, LID_OPEN, LID_CLOSING };

struct Dustbin {
    const char* name;
    uint8_t     servoPin;
    uint8_t     trigPin;
    uint8_t     echoPin;
    Servo       servo;
    LidState    lidState;
    int         currentDeg;
    unsigned long openedAtMs;
    int         openDeg;
    int         closeDeg;
    float       emptyCm;
    float       fullCm;
};

Dustbin paperBin   = { "paper",   PAPER_SERVO_PIN,   PAPER_TRIG_PIN,   PAPER_ECHO_PIN,   Servo(), LID_CLOSED, PAPER_CLOSE_DEG,   0, PAPER_OPEN_DEG,   PAPER_CLOSE_DEG,   PAPER_EMPTY_CM,   PAPER_FULL_CM };
Dustbin plasticBin = { "plastic", PLASTIC_SERVO_PIN, PLASTIC_TRIG_PIN, PLASTIC_ECHO_PIN, Servo(), LID_CLOSED, PLASTIC_CLOSE_DEG, 0, PLASTIC_OPEN_DEG, PLASTIC_CLOSE_DEG, PLASTIC_EMPTY_CM, PLASTIC_FULL_CM };
Dustbin* bins[2]   = { &paperBin, &plasticBin };


// ─────────────────────────────────────────────────────────────────────────────
//  Ultrasonic – median-filtered distance (cm)
// ─────────────────────────────────────────────────────────────────────────────
float readDistanceCM(Dustbin* b) {
    float s[ULTRA_SAMPLES];
    for (int i = 0; i < ULTRA_SAMPLES; i++) {
        digitalWrite(b->trigPin, LOW);  delayMicroseconds(2);
        digitalWrite(b->trigPin, HIGH); delayMicroseconds(10);
        digitalWrite(b->trigPin, LOW);
        long dur = pulseIn(b->echoPin, HIGH, 30000UL);
        s[i] = (dur == 0) ? b->emptyCm : (dur * 0.0343f / 2.0f);
        delay(15);
    }
    // Bubble sort for median
    for (int i = 0; i < ULTRA_SAMPLES - 1; i++)
        for (int j = 0; j < ULTRA_SAMPLES - 1 - i; j++)
            if (s[j] > s[j+1]) { float t = s[j]; s[j] = s[j+1]; s[j+1] = t; }
    return s[ULTRA_SAMPLES / 2];
}

int distToLevel(Dustbin* b, float cm) {
    if (cm >= b->emptyCm) return 0;
    if (cm <= b->fullCm)  return 100;
    return (int)(((b->emptyCm - cm) / (b->emptyCm - b->fullCm)) * 100.0f);
}


// ─────────────────────────────────────────────────────────────────────────────
//  Servo – smooth sweep
// ─────────────────────────────────────────────────────────────────────────────
void sweepTo(Dustbin* b, int target, int stepMs) {
    int step = (target > b->currentDeg) ? 1 : -1;
    while (b->currentDeg != target) {
        b->currentDeg += step;
        b->servo.write(b->currentDeg);
        delay(stepMs);
    }
}

void openLid(Dustbin* b) {
    if (b->lidState == LID_OPEN || b->lidState == LID_OPENING) {
        b->openedAtMs = millis();   // reset auto-close timer
        Serial.printf("[%s] Lid already open – timer reset\n", b->name);
        return;
    }
    Serial.printf("[%s] Opening lid…\n", b->name);
    b->lidState = LID_OPENING;
    digitalWrite(LED_PIN, HIGH);
    sweepTo(b, b->openDeg, SERVO_OPEN_STEP_MS);
    b->lidState   = LID_OPEN;
    b->openedAtMs = millis();
    Serial.printf("[%s] Lid OPEN ✓\n", b->name);
}

void closeLid(Dustbin* b) {
    if (b->lidState == LID_CLOSED || b->lidState == LID_CLOSING) return;
    Serial.printf("[%s] Closing lid…\n", b->name);
    b->lidState = LID_CLOSING;
    sweepTo(b, b->closeDeg, SERVO_STEP_MS);
    b->lidState = LID_CLOSED;
    if (paperBin.lidState == LID_CLOSED && plasticBin.lidState == LID_CLOSED)
        digitalWrite(LED_PIN, LOW);
    Serial.printf("[%s] Lid CLOSED ✓\n", b->name);
}

const char* lidStr(LidState s) {
    switch(s) {
        case LID_OPEN:    return "open";
        case LID_OPENING: return "opening";
        case LID_CLOSING: return "closing";
        default:          return "closed";
    }
}


// ─────────────────────────────────────────────────────────────────────────────
//  JSON builders
// ─────────────────────────────────────────────────────────────────────────────
String jsonLevel(Dustbin* b) {
    float cm  = readDistanceCM(b);
    int   lvl = distToLevel(b, cm);
    JsonDocument doc;
    doc["level"]       = lvl;
    doc["distance_cm"] = serialized(String(cm, 1));
    doc["empty_cm"]    = serialized(String(b->emptyCm, 1));
    doc["full_cm"]     = serialized(String(b->fullCm, 1));
    doc["lid"]         = lidStr(b->lidState);
    doc["bin"]         = b->name;
    String out; serializeJson(doc, out); return out;
}

String jsonStatus(Dustbin* b) {
    float cm  = readDistanceCM(b);
    int   lvl = distToLevel(b, cm);
    JsonDocument doc;
    doc["bin"]         = b->name;
    doc["lid"]         = lidStr(b->lidState);
    doc["level"]       = lvl;
    doc["distance_cm"] = serialized(String(cm, 1));
    doc["empty_cm"]    = serialized(String(b->emptyCm, 1));
    doc["full_cm"]     = serialized(String(b->fullCm, 1));
    doc["ip"]          = WiFi.localIP().toString();
    doc["ssid"]        = WiFi.SSID();
    doc["rssi_dbm"]    = WiFi.RSSI();
    doc["uptime_s"]    = millis() / 1000;
    doc["hostname"]    = HOSTNAME ".local";
    doc["open_deg"]    = b->openDeg;
    doc["close_deg"]   = b->closeDeg;
    String out; serializeJson(doc, out); return out;
}

String jsonAction(Dustbin* b, const char* action) {
    JsonDocument doc;
    doc["status"] = "ok";
    doc["action"] = action;
    doc["lid"]    = lidStr(b->lidState);
    doc["bin"]    = b->name;
    String out; serializeJson(doc, out); return out;
}


// ─────────────────────────────────────────────────────────────────────────────
//  HTTP helpers
// ─────────────────────────────────────────────────────────────────────────────
void sendJSON(AsyncWebServerRequest* req, int code, const String& json) {
    AsyncWebServerResponse* r = req->beginResponse(code, "application/json", json);
    r->addHeader("Access-Control-Allow-Origin",  "*");
    r->addHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    r->addHeader("Access-Control-Allow-Headers", "Content-Type");
    req->send(r);
}


void applyBinConfig(Dustbin* b, AsyncWebServerRequest* req) {
    if (req->hasParam("open")) {
        b->openDeg = req->getParam("open")->value().toInt();
    }
    if (req->hasParam("close")) {
        b->closeDeg = req->getParam("close")->value().toInt();
    }
    if (req->hasParam("empty")) {
        float v = req->getParam("empty")->value().toFloat();
        if (v >= 5.0f && v <= 80.0f) {
            b->emptyCm = v;
            if (b->fullCm >= b->emptyCm)
                b->fullCm = max(1.0f, b->emptyCm - 1.0f);
        }
    }
    if (req->hasParam("full")) {
        float v = req->getParam("full")->value().toFloat();
        if (v >= 1.0f && v <= 30.0f && v < b->emptyCm) b->fullCm = v;
    }
}

String jsonBinConfig(Dustbin* b) {
    JsonDocument doc;
    doc["bin"] = b->name;
    doc["open_deg"]  = b->openDeg;
    doc["close_deg"] = b->closeDeg;
    doc["empty_cm"]  = serialized(String(b->emptyCm, 1));
    doc["full_cm"]   = serialized(String(b->fullCm, 1));
    String j; serializeJson(doc, j); return j;
}


// ─────────────────────────────────────────────────────────────────────────────
//  Route registration – all 11 endpoints
// ─────────────────────────────────────────────────────────────────────────────
void setupRoutes() {
    // CORS pre-flight + 404
    server.onNotFound([](AsyncWebServerRequest* req) {
        if (req->method() == HTTP_OPTIONS) {
            AsyncWebServerResponse* r = req->beginResponse(204);
            r->addHeader("Access-Control-Allow-Origin",  "*");
            r->addHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
            r->addHeader("Access-Control-Allow-Headers", "Content-Type");
            req->send(r);
        } else {
            req->send(404, "application/json", "{\"error\":\"not found\"}");
        }
    });

    // Health check – returns IP + SSID so Pi can log which network ESP32 is on
    server.on("/ping", HTTP_GET, [](AsyncWebServerRequest* req) {
        JsonDocument doc;
        doc["alive"]    = true;
        doc["bins"]     = "paper,plastic";
        doc["ip"]       = WiFi.localIP().toString();
        doc["ssid"]     = WiFi.SSID();
        doc["rssi_dbm"] = WiFi.RSSI();
        doc["hostname"] = HOSTNAME ".local";
        
        JsonObject paper = doc["paper"].to<JsonObject>();
        paper["open_deg"]  = paperBin.openDeg;
        paper["close_deg"] = paperBin.closeDeg;
        paper["empty_cm"]  = serialized(String(paperBin.emptyCm, 1));
        paper["full_cm"]   = serialized(String(paperBin.fullCm, 1));

        JsonObject plastic = doc["plastic"].to<JsonObject>();
        plastic["open_deg"]  = plasticBin.openDeg;
        plastic["close_deg"] = plasticBin.closeDeg;
        plastic["empty_cm"]  = serialized(String(plasticBin.emptyCm, 1));
        plastic["full_cm"]   = serialized(String(plasticBin.fullCm, 1));
        
        String j; serializeJson(doc, j);
        sendJSON(req, 200, j);
    });

    // ── Paper ──────────────────────────────────────────────────────────────────
    server.on("/api/dustbin/paper/level", HTTP_GET, [](AsyncWebServerRequest* req) {
        String j = jsonLevel(&paperBin);
        Serial.printf("[API] GET  /paper/level  → %s\n", j.c_str());
        sendJSON(req, 200, j);
    });
    server.on("/api/dustbin/paper/status", HTTP_GET, [](AsyncWebServerRequest* req) {
        sendJSON(req, 200, jsonStatus(&paperBin));
    });
    server.on("/api/dustbin/paper/open", HTTP_POST, [](AsyncWebServerRequest* req) {
        openLid(&paperBin);
        String j = jsonAction(&paperBin, "open");
        Serial.printf("[API] POST /paper/open   → %s\n", j.c_str());
        sendJSON(req, 200, j);
    });
    server.on("/api/dustbin/paper/close", HTTP_POST, [](AsyncWebServerRequest* req) {
        closeLid(&paperBin);
        String j = jsonAction(&paperBin, "close");
        Serial.printf("[API] POST /paper/close  → %s\n", j.c_str());
        sendJSON(req, 200, j);
    });
    server.on("/api/dustbin/paper/config", HTTP_ANY, [](AsyncWebServerRequest* req) {
        applyBinConfig(&paperBin, req);
        String j = jsonBinConfig(&paperBin);
        Serial.printf("[API] POST /paper/config → %s\n", j.c_str());
        sendJSON(req, 200, j);
    });

    // ── Plastic ────────────────────────────────────────────────────────────────
    server.on("/api/dustbin/plastic/level", HTTP_GET, [](AsyncWebServerRequest* req) {
        String j = jsonLevel(&plasticBin);
        Serial.printf("[API] GET  /plastic/level  → %s\n", j.c_str());
        sendJSON(req, 200, j);
    });
    server.on("/api/dustbin/plastic/status", HTTP_GET, [](AsyncWebServerRequest* req) {
        sendJSON(req, 200, jsonStatus(&plasticBin));
    });
    server.on("/api/dustbin/plastic/open", HTTP_POST, [](AsyncWebServerRequest* req) {
        openLid(&plasticBin);
        String j = jsonAction(&plasticBin, "open");
        Serial.printf("[API] POST /plastic/open   → %s\n", j.c_str());
        sendJSON(req, 200, j);
    });
    server.on("/api/dustbin/plastic/close", HTTP_POST, [](AsyncWebServerRequest* req) {
        closeLid(&plasticBin);
        String j = jsonAction(&plasticBin, "close");
        Serial.printf("[API] POST /plastic/close  → %s\n", j.c_str());
        sendJSON(req, 200, j);
    });
    server.on("/api/dustbin/plastic/config", HTTP_ANY, [](AsyncWebServerRequest* req) {
        applyBinConfig(&plasticBin, req);
        String j = jsonBinConfig(&plasticBin);
        Serial.printf("[API] POST /plastic/config → %s\n", j.c_str());
        sendJSON(req, 200, j);
    });

    Serial.println("[HTTP] 11 routes registered");
}


// ─────────────────────────────────────────────────────────────────────────────
//  WiFiMulti connect – tries all networks until one works
// ─────────────────────────────────────────────────────────────────────────────
void connectWiFi() {
    Serial.println("[WiFi] Scanning for known networks…");
    // Add all configured networks to WiFiMulti
    int n = sizeof(WIFI_NETWORKS) / sizeof(WIFI_NETWORKS[0]);
    for (int i = 0; i < n; i++)
        wifiMulti.addAP(WIFI_NETWORKS[i].ssid, WIFI_NETWORKS[i].pass);

    Serial.printf("[WiFi] %d network(s) configured – connecting", n);
    uint8_t attempts = 0;
    while (wifiMulti.run() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
        if (++attempts > 60) {   // 30-second timeout then reboot
            Serial.println("\n[WiFi] No network found – rebooting");
            ESP.restart();
        }
    }
    printNetworkInfo();
}

void printNetworkInfo() {
    Serial.println();
    Serial.println("╔══════════════════════════════════════════════════╗");
    Serial.printf( "║  Connected to : %-32s║\n", WiFi.SSID().c_str());
    Serial.printf( "║  IP address   : %-32s║\n", WiFi.localIP().toString().c_str());
    Serial.printf( "║  mDNS         : %-32s║\n", (String(HOSTNAME) + ".local").c_str());
    Serial.printf( "║  RSSI         : %d dBm%-26s║\n", WiFi.RSSI(), "");
    Serial.println("╚══════════════════════════════════════════════════╝");
    Serial.printf("[READY] Pi config → ESP32_HOST = \"%s.local\"  (or IP: %s)\n",
                  HOSTNAME, WiFi.localIP().toString().c_str());
}


// ─────────────────────────────────────────────────────────────────────────────
//  Setup
// ─────────────────────────────────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);
    delay(600);
    Serial.println(F("\n╔══════════════════════════════════════════╗"));
    Serial.println(F(  "║  Dual Dustbin Controller  –  ESP32-S3   ║"));
    Serial.println(F(  "║  Paper (class 1)  +  Plastic (class 2)  ║"));
    Serial.println(F(  "╚══════════════════════════════════════════╝\n"));

    // GPIO
    pinMode(LED_PIN,          OUTPUT);
    pinMode(PAPER_TRIG_PIN,   OUTPUT); pinMode(PAPER_ECHO_PIN,   INPUT);
    pinMode(PLASTIC_TRIG_PIN, OUTPUT); pinMode(PLASTIC_ECHO_PIN, INPUT);
    digitalWrite(LED_PIN, LOW);
    digitalWrite(PAPER_TRIG_PIN,   LOW);
    digitalWrite(PLASTIC_TRIG_PIN, LOW);

    // Servos
    ESP32PWM::allocateTimer(0);
    ESP32PWM::allocateTimer(1);

    paperBin.servo.setPeriodHertz(50);
    paperBin.servo.attach(PAPER_SERVO_PIN, 500, 2400);
    paperBin.servo.write(paperBin.closeDeg);
    Serial.printf("[Servo] Paper   GPIO%-2d  → %d° (closed)\n", PAPER_SERVO_PIN, paperBin.closeDeg);

    plasticBin.servo.setPeriodHertz(50);
    plasticBin.servo.attach(PLASTIC_SERVO_PIN, 500, 2400);
    plasticBin.servo.write(plasticBin.closeDeg);
    Serial.printf("[Servo] Plastic GPIO%-2d  → %d° (closed)\n", PLASTIC_SERVO_PIN, plasticBin.closeDeg);

    // WiFi (multi-network)
    WiFi.mode(WIFI_STA);
    connectWiFi();

    // mDNS
    if (MDNS.begin(HOSTNAME)) {
        MDNS.addService("http", "tcp", HTTP_PORT);
        Serial.printf("[mDNS]  http://%s.local\n", HOSTNAME);
    } else {
        Serial.println("[mDNS]  Failed – use IP directly");
    }

    // HTTP server
    setupRoutes();
    server.begin();
    Serial.printf("[HTTP]  Listening on port %d\n", HTTP_PORT);

    // Ready blink
    for (int i = 0; i < 4; i++) {
        digitalWrite(LED_PIN, HIGH); delay(100);
        digitalWrite(LED_PIN, LOW);  delay(100);
    }
    Serial.println("[READY] Waiting for commands…\n");
}


// ─────────────────────────────────────────────────────────────────────────────
//  Loop  –  auto-close + WiFi watchdog + heartbeat LED
// ─────────────────────────────────────────────────────────────────────────────
void loop() {
    unsigned long now = millis();

    // ── Auto-close: independently close each lid after timeout ────────────────
    for (int i = 0; i < 2; i++) {
        Dustbin* b = bins[i];
        if (b->lidState == LID_OPEN && (now - b->openedAtMs) >= AUTO_CLOSE_MS) {
            Serial.printf("[AUTO-CLOSE] %s – no activity for %lus\n",
                          b->name, (unsigned long)(AUTO_CLOSE_MS / 1000));
            closeLid(b);
        }
    }

    // ── WiFi watchdog – reconnect using WiFiMulti on drop ─────────────────────
    static unsigned long lastWiFiCheck = 0;
    if (now - lastWiFiCheck > 10000UL) {
        lastWiFiCheck = now;
        if (WiFi.status() != WL_CONNECTED) {
            Serial.println("[WiFi] Connection lost – reconnecting…");
            if (wifiMulti.run() == WL_CONNECTED) {
                Serial.println("[WiFi] Reconnected ✓");
                printNetworkInfo();
                // Re-register mDNS after reconnect
                MDNS.end();
                if (MDNS.begin(HOSTNAME)) MDNS.addService("http", "tcp", HTTP_PORT);
            }
        }
    }

    // ── Heartbeat LED: slow blink when idle (both lids closed) ───────────────
    static unsigned long lastBlink = 0;
    static bool          ledOn     = false;
    bool bothClosed = (paperBin.lidState == LID_CLOSED && plasticBin.lidState == LID_CLOSED);
    if (bothClosed && (now - lastBlink > 2500UL)) {
        lastBlink = now;
        ledOn = !ledOn;
        digitalWrite(LED_PIN, ledOn);
    }

    delay(50);
}