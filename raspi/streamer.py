# ─────────────────────────────────────────────────────────────────────────────
# streamer.py  –  MJPEG stream server (Flask) for ground-station software
# ─────────────────────────────────────────────────────────────────────────────
#
#  Ground-station can view the feed at:
#       http://<raspi-ip>:5000/video_feed      ← raw MJPEG
#       http://<raspi-ip>:5000/status          ← JSON dustbin status
#       http://<raspi-ip>:5000/                ← embedded HTML dashboard
# ─────────────────────────────────────────────────────────────────────────────

import io
import time
import threading
import logging
from flask import Flask, Response, jsonify, render_template_string

from config import STREAM_HOST, STREAM_PORT, MJPEG_QUALITY, MJPEG_MAX_FPS
from dustbin_api import dustbin_state, _lock

log = logging.getLogger(__name__)

app = Flask(__name__)

import cv2
import numpy as np

# ── Shared frame buffer ───────────────────────────────────────────────────────
_frame_lock    = threading.Lock()
_latest_frame  = None   # raw JPEG bytes (annotated by YOLO)
_frame_time    = 0.0


def push_frame(jpeg_bytes: bytes):
    """Called by the detection loop each time a new annotated frame is ready."""
    global _latest_frame, _frame_time
    with _frame_lock:
        _latest_frame = jpeg_bytes
        _frame_time   = time.time()


def get_placeholder_frame() -> bytes:
    """Generates a dark-themed 'Camera Stream Connecting...' placeholder frame."""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    img[:] = (20, 25, 34)  # Matches ground-station background color
    
    # Draw border
    cv2.rectangle(img, (30, 30), (610, 450), (30, 41, 59), 2)
    
    # Pulse status dot
    dot_color = (16, 185, 129) if int(time.time()) % 2 == 0 else (100, 116, 139)
    cv2.circle(img, (320, 180), 8, dot_color, -1)
    
    font = cv2.FONT_HERSHEY_SIMPLEX
    text = "CAMERA STREAM INITIALIZING..."
    text_sz = cv2.getTextSize(text, font, 0.6, 2)[0]
    cv2.putText(img, text, ((640 - text_sz[0]) // 2, 240), font, 0.6, (226, 232, 240), 2, cv2.LINE_AA)
    
    sub = "Trying GStreamer (Pi Cam v3) & V4L2 (USB Cams)"
    sub_sz = cv2.getTextSize(sub, font, 0.45, 1)[0]
    cv2.putText(img, sub, ((640 - sub_sz[0]) // 2, 280), font, 0.45, (100, 116, 139), 1, cv2.LINE_AA)
    
    ts = time.strftime("%Y-%m-%d  %H:%M:%S")
    ts_sz = cv2.getTextSize(ts, font, 0.4, 1)[0]
    cv2.putText(img, ts, ((640 - ts_sz[0]) // 2, 420), font, 0.4, (100, 116, 139), 1, cv2.LINE_AA)
    
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, MJPEG_QUALITY])
    return bytes(buf) if ok else b""


# ── MJPEG generator ───────────────────────────────────────────────────────────
def _gen_frames():
    min_interval = 1.0 / MJPEG_MAX_FPS
    last_sent    = 0.0
    while True:
        now = time.time()
        wait = min_interval - (now - last_sent)
        if wait > 0:
            time.sleep(wait)

        with _frame_lock:
            frame = _latest_frame

        if frame is None:
            frame = get_placeholder_frame()

        last_sent = time.time()
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        )


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/video_feed")
def video_feed():
    return Response(
        _gen_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )


@app.route("/status")
def status():
    with _lock:
        data = {
            "paper":   dict(dustbin_state["paper"]),
            "plastic": dict(dustbin_state["plastic"]),
        }
    return jsonify(data)


@app.route("/config", methods=["POST"])
def update_config():
    import requests
    from flask import request
    data = request.get_json() or {}
    bin_name = data.get("bin")
    open_deg = data.get("open_deg")
    close_deg = data.get("close_deg")

    if bin_name not in ("paper", "plastic"):
        return jsonify({"error": "invalid bin"}), 400

    params = {}
    if open_deg is not None:
        params["open"] = int(open_deg)
    if close_deg is not None:
        params["close"] = int(close_deg)

    # Forward the configuration to the ESP32
    from config import ESP32_HOST, API_TIMEOUT
    url = f"http://{ESP32_HOST}/api/dustbin/{bin_name}/config"
    try:
        r = requests.post(url, params=params, timeout=API_TIMEOUT)
        r.raise_for_status()
        resp_data = r.json()
        
        # Update our local state
        with _lock:
            if "open_deg" in resp_data:
                dustbin_state[bin_name]["open_deg"] = resp_data["open_deg"]
            if "close_deg" in resp_data:
                dustbin_state[bin_name]["close_deg"] = resp_data["close_deg"]
                
        return jsonify({"status": "ok", "config": dustbin_state[bin_name]})
    except Exception as e:
        log.warning("Failed to configure ESP32 [%s]: %s", url, e)
        return jsonify({"error": f"Failed to configure ESP32: {str(e)}"}), 500


# ── Embedded HTML dashboard ───────────────────────────────────────────────────
_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🗑️ Trash Detection – Ground Station</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700;900&display=swap');
  :root {
    --bg:       #0d0f14;
    --surface:  #161b26;
    --card:     #1e2535;
    --accent1:  #3b82f6;
    --accent2:  #10b981;
    --warn:     #f59e0b;
    --danger:   #ef4444;
    --text:     #e2e8f0;
    --muted:    #64748b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 24px 16px;
    gap: 24px;
  }
  header {
    width: 100%;
    max-width: 1200px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding-bottom: 16px;
    border-bottom: 1px solid rgba(255,255,255,.06);
  }
  header h1 { font-size: 1.4rem; font-weight: 700; }
  .badge {
    padding: 3px 10px;
    border-radius: 999px;
    font-size: .72rem;
    font-weight: 600;
    background: rgba(59,130,246,.2);
    color: var(--accent1);
    border: 1px solid rgba(59,130,246,.35);
  }
  .live-dot {
    width: 9px; height: 9px;
    border-radius: 50%;
    background: var(--accent2);
    box-shadow: 0 0 8px var(--accent2);
    animation: pulse 1.4s infinite;
    margin-left: auto;
  }
  @keyframes pulse {
    0%,100% { opacity:1; } 50% { opacity:.3; }
  }
  .main-grid {
    width: 100%;
    max-width: 1200px;
    display: grid;
    grid-template-columns: 1fr 340px;
    gap: 20px;
  }
  @media (max-width: 860px) {
    .main-grid { grid-template-columns: 1fr; }
  }
  .video-card {
    background: var(--surface);
    border-radius: 16px;
    border: 1px solid rgba(255,255,255,.07);
    overflow: hidden;
    position: relative;
  }
  .video-card img {
    width: 100%;
    display: block;
    border-radius: 16px;
  }
  .video-label {
    position: absolute;
    top: 12px; left: 12px;
    background: rgba(0,0,0,.6);
    backdrop-filter: blur(6px);
    padding: 4px 12px;
    border-radius: 8px;
    font-size: .75rem;
    font-weight: 600;
    color: var(--accent2);
    border: 1px solid rgba(16,185,129,.3);
  }
  .sidebar { display: flex; flex-direction: column; gap: 16px; }
  .card {
    background: var(--card);
    border-radius: 14px;
    border: 1px solid rgba(255,255,255,.07);
    padding: 18px 20px;
  }
  .card-title {
    font-size: .7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: var(--muted);
    margin-bottom: 14px;
  }
  .bin-row {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    margin-bottom: 24px;
  }
  .bin-icon { font-size: 1.6rem; margin-top: 2px; }
  .bin-info { flex: 1; }
  .bin-name { font-weight: 600; font-size: .95rem; }
  .bin-lid  { font-size: .75rem; color: var(--muted); margin-top: 2px; }
  .bin-lid.open  { color: var(--accent2); }
  .bin-lid.closed { color: var(--muted); }
  .level-bar-bg {
    height: 8px;
    border-radius: 999px;
    background: rgba(255,255,255,.08);
    overflow: hidden;
    margin-top: 8px;
  }
  .level-bar-fill {
    height: 100%;
    border-radius: 999px;
    transition: width .5s ease, background .3s;
  }
  .level-text {
    font-size: .75rem;
    font-weight: 600;
    margin-top: 4px;
    text-align: right;
  }
  
  /* Angle adjusters styling */
  .angle-controls {
    margin-top: 14px;
    padding: 10px 12px;
    background: rgba(0, 0, 0, 0.2);
    border-radius: 8px;
    border: 1px solid rgba(255, 255, 255, 0.05);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .angle-slider-group {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .angle-slider-group label {
    font-size: 0.7rem;
    font-weight: 600;
    color: var(--muted);
    display: flex;
    justify-content: space-between;
  }
  .angle-slider-group label span {
    color: var(--text);
  }
  .angle-slider-group input[type="range"] {
    width: 100%;
    accent-color: var(--accent1);
    height: 4px;
    border-radius: 2px;
    background: rgba(255,255,255,0.1);
    outline: none;
    -webkit-appearance: none;
  }
  .angle-slider-group input[type="range"]::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--text);
    cursor: pointer;
    transition: background 0.15s;
  }
  .angle-slider-group input[type="range"]::-webkit-slider-thumb:hover {
    background: var(--accent1);
  }

  .log-box {
    background: rgba(0,0,0,.4);
    border-radius: 10px;
    border: 1px solid rgba(255,255,255,.06);
    padding: 10px 12px;
    font-size: .72rem;
    font-family: 'Courier New', monospace;
    color: #94a3b8;
    height: 160px;
    overflow-y: auto;
    display: flex;
    flex-direction: column-reverse;
  }
  .log-entry { padding: 2px 0; border-bottom: 1px solid rgba(255,255,255,.04); }
  .log-entry.open  { color: var(--accent2); }
  .log-entry.close { color: var(--warn); }
  .log-entry.level { color: var(--accent1); }
</style>
</head>
<body>
<header>
  <span style="font-size:1.5rem">🗑️</span>
  <h1>Trash Detection Ground Station</h1>
  <span class="badge">LIVE</span>
  <span class="live-dot"></span>
</header>

<div class="main-grid">
  <!-- Video feed -->
  <div class="video-card">
    <div class="video-label">📡 Raspberry Pi Camera</div>
    <img id="feed" src="/video_feed" alt="Live YOLO Detection Feed">
  </div>

  <!-- Sidebar -->
  <div class="sidebar">
    <!-- Dustbin status cards -->
    <div class="card">
      <div class="card-title">Dustbin Status & Angle Settings</div>

      <!-- Paper dustbin -->
      <div class="bin-row">
        <div class="bin-icon">📄</div>
        <div class="bin-info">
          <div class="bin-name">Paper Dustbin</div>
          <div class="bin-lid" id="paper-lid">● Lid: closed</div>
          <div class="level-bar-bg">
            <div class="level-bar-fill" id="paper-bar"
                 style="width:0%; background: #3b82f6;"></div>
          </div>
          <div class="level-text" id="paper-level">0%</div>
          
          <!-- Angle Adjusters -->
          <div class="angle-controls">
            <div class="angle-slider-group">
              <label>Open Angle: <span id="paper-open-val">90</span>°</label>
              <input type="range" id="paper-open-input" min="0" max="180" value="90" onchange="sendAngle('paper')">
            </div>
            <div class="angle-slider-group">
              <label>Close Angle: <span id="paper-close-val">0</span>°</label>
              <input type="range" id="paper-close-input" min="0" max="180" value="0" onchange="sendAngle('paper')">
            </div>
          </div>
        </div>
      </div>

      <!-- Plastic dustbin -->
      <div class="bin-row" style="margin-bottom:0">
        <div class="bin-icon">🧴</div>
        <div class="bin-info">
          <div class="bin-name">Plastic Dustbin</div>
          <div class="bin-lid" id="plastic-lid">● Lid: closed</div>
          <div class="level-bar-bg">
            <div class="level-bar-fill" id="plastic-bar"
                 style="width:0%; background: #10b981;"></div>
          </div>
          <div class="level-text" id="plastic-level">0%</div>
          
          <!-- Angle Adjusters -->
          <div class="angle-controls">
            <div class="angle-slider-group">
              <label>Open Angle: <span id="plastic-open-val">90</span>°</label>
              <input type="range" id="plastic-open-input" min="0" max="180" value="90" onchange="sendAngle('plastic')">
            </div>
            <div class="angle-slider-group">
              <label>Close Angle: <span id="plastic-close-val">0</span>°</label>
              <input type="range" id="plastic-close-input" min="0" max="180" value="0" onchange="sendAngle('plastic')">
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Event log -->
    <div class="card">
      <div class="card-title">Event Log</div>
      <div class="log-box" id="log-box">
        <div class="log-entry">System ready – polling status…</div>
      </div>
    </div>

    <!-- Detection classes info -->
    <div class="card">
      <div class="card-title">Detection Classes</div>
      <div style="display:flex; flex-direction:column; gap:8px; font-size:.82rem;">
        <div style="display:flex; align-items:center; gap:8px;">
          <span style="width:10px;height:10px;border-radius:3px;background:#3b82f6;display:inline-block;"></span>
          <span><b>Class 2 – Paper</b> → Opens Paper Lid</span>
        </div>
        <div style="display:flex; align-items:center; gap:8px;">
          <span style="width:10px;height:10px;border-radius:3px;background:#10b981;display:inline-block;"></span>
          <span><b>Class 3 – Plastic</b> → Opens Plastic Lid</span>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const logBox    = document.getElementById('log-box');
let   prevState = { paper: {}, plastic: {} };

function addLog(msg, type='') {
  const el = document.createElement('div');
  el.className = 'log-entry ' + type;
  el.textContent = new Date().toLocaleTimeString() + '  ' + msg;
  logBox.prepend(el);
  while (logBox.children.length > 60) logBox.removeChild(logBox.lastChild);
}

function levelColor(pct) {
  if (pct >= 90) return '#ef4444';
  if (pct >= 60) return '#f59e0b';
  return pct > 30 ? '#3b82f6' : '#10b981';
}

function updateUI(data) {
  ['paper','plastic'].forEach(bin => {
    const s = data[bin];
    const lid   = document.getElementById(bin+'-lid');
    const bar   = document.getElementById(bin+'-bar');
    const level = document.getElementById(bin+'-level');

    // Lid state
    const isOpen = s.lid === 'open';
    lid.textContent = '● Lid: ' + s.lid;
    lid.className   = 'bin-lid ' + (isOpen ? 'open' : 'closed');

    // Detect lid changes
    if (prevState[bin].lid !== undefined && prevState[bin].lid !== s.lid) {
      const act = isOpen ? '🔓 OPENED' : '🔒 CLOSED';
      const type = isOpen ? 'open' : 'close';
      addLog(bin.charAt(0).toUpperCase()+bin.slice(1)+' lid ' + act, type);
    }

    // Detect level changes
    if (prevState[bin].level_pct !== s.level_pct) {
      addLog('📊 '+bin+' fill level: '+s.level_pct+'%', 'level');
    }

    // Bar
    const pct = s.level_pct;
    bar.style.width    = pct + '%';
    bar.style.background = levelColor(pct);
    level.textContent  = pct + '%';
    level.style.color  = levelColor(pct);

    // Sync sliders (only if not currently focused by the user)
    const openInput = document.getElementById(bin+'-open-input');
    const closeInput = document.getElementById(bin+'-close-input');
    if (document.activeElement !== openInput && s.open_deg !== undefined) {
      openInput.value = s.open_deg;
      document.getElementById(bin+'-open-val').textContent = s.open_deg;
    }
    if (document.activeElement !== closeInput && s.close_deg !== undefined) {
      closeInput.value = s.close_deg;
      document.getElementById(bin+'-close-val').textContent = s.close_deg;
    }

    prevState[bin] = { ...s };
  });
}

async function poll() {
  try {
    const resp = await fetch('/status');
    if (resp.ok) updateUI(await resp.json());
  } catch(e) { console.warn('Status poll failed', e); }
}

async function sendAngle(bin) {
  const openVal  = parseInt(document.getElementById(bin + '-open-input').value);
  const closeVal = parseInt(document.getElementById(bin + '-close-input').value);
  
  document.getElementById(bin + '-open-val').textContent = openVal;
  document.getElementById(bin + '-close-val').textContent = closeVal;

  try {
    const resp = await fetch('/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bin: bin, open_deg: openVal, close_deg: closeVal })
    });
    if (resp.ok) {
      addLog('🔧 Configured ' + bin + ' angles: Open=' + openVal + '°, Close=' + closeVal + '°', 'level');
    } else {
      addLog('❌ Failed to update angles for ' + bin, 'close');
    }
  } catch(e) {
    console.error(e);
    addLog('❌ Error connecting to controller', 'close');
  }
}

// Add real-time drag display updates
['paper', 'plastic'].forEach(bin => {
  ['open', 'close'].forEach(type => {
    const input = document.getElementById(bin + '-' + type + '-input');
    const val = document.getElementById(bin + '-' + type + '-val');
    input.addEventListener('input', (e) => {
      val.textContent = e.target.value;
    });
  });
});

poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""


@app.route("/")
def dashboard():
    return render_template_string(_DASHBOARD_HTML)


# ─────────────────────────────────────────────────────────────────────────────
def start_stream_server():
    """Start Flask in a daemon thread so it doesn't block the main loop."""
    t = threading.Thread(
        target=lambda: app.run(
            host=STREAM_HOST,
            port=STREAM_PORT,
            debug=False,
            use_reloader=False,
            threaded=True,
        ),
        daemon=True,
        name="FlaskStream",
    )
    t.start()
    log.info("Stream server started → http://%s:%d/", STREAM_HOST, STREAM_PORT)
