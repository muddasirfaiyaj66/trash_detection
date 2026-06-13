# ─────────────────────────────────────────────────────────────────────────────
# streamer.py  –  MJPEG stream server (Flask) for ground-station software
# ─────────────────────────────────────────────────────────────────────────────
#
#  Ground-station can view the feed at:
#       http://<raspi-ip>:5000/video_feed      ← raw MJPEG
#       http://<raspi-ip>:5000/status          ← JSON dustbin status
#       http://<raspi-ip>:5000/                ← embedded HTML dashboard
# ─────────────────────────────────────────────────────────────────────────────

import time
import threading
import logging
from pathlib import Path
from flask import Flask, Response, jsonify, render_template_string

try:
    from flask_sock import Sock
    WEBSOCKET_AVAILABLE = True
except ImportError:
    Sock = None
    WEBSOCKET_AVAILABLE = False

from config import (
    STREAM_HOST, STREAM_PORT, MJPEG_QUALITY, MJPEG_MAX_FPS, ESP32_ENABLED,
    CAMERA_TYPE, USB_CAMERA_INDEX, CAMERA_WIDTH, CAMERA_HEIGHT,
    STREAM_FPS, INFERENCE_FPS, YOLO_IMGSZ, DETECT_CLASSES, CLASS_NAMES, YOLO_MIN_CONF,
    get_confidence, set_confidence, get_lid_open_duration, set_lid_open_duration,
)
from dustbin_api import dustbin_state, _lock, set_lid_manual

log = logging.getLogger(__name__)

app = Flask(__name__)
sock = Sock(app) if WEBSOCKET_AVAILABLE else None

import cv2
import numpy as np

# ── Shared frame buffer + stream health ───────────────────────────────────────
_frame_lock     = threading.Lock()
_latest_frame   = None
_frame_time     = 0.0
_total_frames   = 0
_fps            = 0.0
_fps_window_cnt = 0
_fps_window_ts  = time.time()
_camera_error   = None


def set_camera_status(ok: bool, error: str | None = None):
    """Called from detect.py when camera opens, fails, or stalls."""
    global _camera_error
    _camera_error = None if ok else (error or "Camera unavailable")


def push_frame(jpeg_bytes: bytes):
    """Called by the detection loop each time a new frame is ready."""
    global _latest_frame, _frame_time, _total_frames, _fps, _fps_window_cnt, _fps_window_ts
    if not jpeg_bytes:
        return
    now = time.time()
    with _frame_lock:
        _latest_frame = jpeg_bytes
        _frame_time = now
        _total_frames += 1
        _fps_window_cnt += 1
        elapsed = now - _fps_window_ts
        if elapsed >= 1.0:
            _fps = _fps_window_cnt / elapsed
            _fps_window_cnt = 0
            _fps_window_ts = now


def _stream_status() -> dict:
    with _frame_lock:
        age = None if _frame_time <= 0 else time.time() - _frame_time
        # Frame is stale if older than 2 seconds (instead of 4)
        active = _latest_frame is not None and age is not None and age < 2.0
        return {
            "active": active,
            "last_frame_age_sec": round(age, 2) if age is not None else None,
            "fps": round(_fps, 1),
            "target_fps": STREAM_FPS,
            "inference_fps": INFERENCE_FPS,
            "frame_count": _total_frames,
            "error": _camera_error,
            "mjpeg": True,
            "websocket": WEBSOCKET_AVAILABLE,
        }


def get_placeholder_frame() -> bytes:
    """Placeholder JPEG when detect.py has not sent a real frame yet."""
    h, w = CAMERA_HEIGHT, CAMERA_WIDTH
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = (15, 18, 24)

    cv2.rectangle(img, (24, 24), (w - 24, h - 24), (40, 48, 64), 2)

    font = cv2.FONT_HERSHEY_SIMPLEX
    title = "WAITING FOR CAMERA"
    text_sz = cv2.getTextSize(title, font, 0.65, 2)[0]
    cv2.putText(
        img, title, ((w - text_sz[0]) // 2, h // 2 - 20),
        font, 0.65, (220, 225, 235), 2, cv2.LINE_AA,
    )

    sub = _camera_error or f"CAMERA_TYPE={CAMERA_TYPE}  {w}x{h}"
    if len(sub) > 52:
        sub = sub[:49] + "..."
    sub_sz = cv2.getTextSize(sub, font, 0.42, 1)[0]
    cv2.putText(
        img, sub, ((w - sub_sz[0]) // 2, h // 2 + 24),
        font, 0.42, (120, 130, 150), 1, cv2.LINE_AA,
    )
    
    ts = time.strftime("%H:%M:%S")
    ts_sz = cv2.getTextSize(ts, font, 0.4, 1)[0]
    cv2.putText(img, ts, ((w - ts_sz[0]) // 2, h - 36), font, 0.4, (90, 100, 120), 1, cv2.LINE_AA)
    
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
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.route("/snapshot.jpg")
def snapshot():
    with _frame_lock:
        data = _latest_frame or get_placeholder_frame()
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


if WEBSOCKET_AVAILABLE:

    @sock.route("/ws/video")
    def video_ws(ws):
        """Low-latency JPEG frames over WebSocket (lower overhead than MJPEG multipart)."""
        min_interval = 1.0 / MJPEG_MAX_FPS
        last_sent = 0.0
        while True:
            now = time.time()
            wait = min_interval - (now - last_sent)
            if wait > 0:
                time.sleep(wait)
            with _frame_lock:
                frame = _latest_frame
            if frame is None:
                frame = get_placeholder_frame()
            try:
                ws.send(frame)
            except Exception:
                break
            last_sent = time.time()


@app.route("/status")
def status():
    import camera
    cam_mode = camera.get_camera_mode()
    try:
        import pipeline
        system = pipeline.thermal_state()
    except Exception:
        system = {"cpu_temp": 0.0, "throttled": False}
    with _lock:
        data = {
            "paper":   dict(dustbin_state["paper"]),
            "plastic": dict(dustbin_state["plastic"]),
            "camera": {
                "type": cam_mode["type"],
                "usb_index": cam_mode["usb_index"],
                "width": CAMERA_WIDTH,
                "height": CAMERA_HEIGHT,
                "transform": camera.get_camera_transform(),
            },
            "stream": _stream_status(),
            "system": system,
            "detection": {
                "confidence": get_confidence(),
                "yolo_min_conf": YOLO_MIN_CONF,
                "lid_open_duration": get_lid_open_duration(),
                "imgsz": YOLO_IMGSZ,
                "classes": DETECT_CLASSES,
                "class_names": CLASS_NAMES,
            },
        }
    return jsonify(data)


@app.route("/camera/switch", methods=["POST"])
def camera_switch():
    """Switch camera mode live from the dashboard: {"type": "usb"|"pi"|"auto", "usb_index": 0}"""
    import camera
    from flask import request
    data = request.get_json(silent=True) or {}
    cam_type = data.get("type")
    usb_index = data.get("usb_index")
    try:
        mode = camera.set_camera_mode(cam_type, usb_index)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    log.info("Camera switch via dashboard → %s", mode)
    return jsonify({"status": "ok", "camera": mode})


@app.route("/camera/transform", methods=["POST"])
def camera_transform():
    """Flip / rotate the feed live: {"flip_h": bool, "flip_v": bool, "rotate": 0|90|180|270}.
    Pass {"rotate_step": true} to cycle rotation 0→90→180→270."""
    import camera
    from flask import request
    data = request.get_json(silent=True) or {}
    rotate = data.get("rotate")
    if data.get("rotate_step"):
        rotate = (camera.get_camera_transform()["rotate"] + 90) % 360
    try:
        t = camera.set_camera_transform(
            flip_h=data.get("flip_h"),
            flip_v=data.get("flip_v"),
            rotate=rotate,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    log.info("Camera transform → %s", t)
    return jsonify({"status": "ok", "transform": t})


@app.route("/settings", methods=["GET", "POST"])
def settings_all():
    """GET/POST all live dashboard settings (confidence, auto-close delay)."""
    from flask import request
    if request.method == "GET":
        return jsonify(_settings_payload())
    data = request.get_json(silent=True) or {}
    out = {}
    if "confidence" in data:
        try:
            out["confidence"] = set_confidence(data["confidence"])
        except (TypeError, ValueError):
            return jsonify({"error": "invalid confidence"}), 400
    if "lid_open_duration" in data:
        try:
            out["lid_open_duration"] = set_lid_open_duration(data["lid_open_duration"])
        except (TypeError, ValueError):
            return jsonify({"error": "invalid lid_open_duration"}), 400
    if not out:
        return jsonify({"error": "no settings provided"}), 400
    log.info("Settings updated → %s", out)
    payload = _settings_payload()
    payload.update(out)
    return jsonify({"status": "ok", **payload})


def _settings_payload() -> dict:
    return {
        "confidence": get_confidence(),
        "lid_open_duration": get_lid_open_duration(),
        "yolo_min_conf": YOLO_MIN_CONF,
        "imgsz": YOLO_IMGSZ,
        "classes": DETECT_CLASSES,
        "class_names": CLASS_NAMES,
    }


@app.route("/settings/detection", methods=["GET", "POST"])
@app.route("/detection/confidence", methods=["GET", "POST"])
def settings_detection():
    """GET/POST lid-open confidence threshold (live, no restart)."""
    from flask import request
    if request.method == "GET":
        return jsonify(_settings_payload())
    data = request.get_json(silent=True) or {}
    if "confidence" not in data:
        return jsonify({"error": "confidence required"}), 400
    try:
        conf = set_confidence(data["confidence"])
    except (TypeError, ValueError):
        return jsonify({"error": "invalid confidence"}), 400
    log.info("Lid-open confidence → %.2f", conf)
    return jsonify({"status": "ok", "confidence": conf, "lid_open_duration": get_lid_open_duration()})


@app.route("/settings/lid", methods=["GET", "POST"])
def settings_lid():
    """GET/POST auto-close delay after last detection (seconds)."""
    from flask import request
    if request.method == "GET":
        return jsonify({"lid_open_duration": get_lid_open_duration()})
    data = request.get_json(silent=True) or {}
    if "lid_open_duration" not in data:
        return jsonify({"error": "lid_open_duration required"}), 400
    try:
        sec = set_lid_open_duration(data["lid_open_duration"])
    except (TypeError, ValueError):
        return jsonify({"error": "invalid lid_open_duration"}), 400
    log.info("Lid auto-close delay → %.1fs", sec)
    return jsonify({"status": "ok", "lid_open_duration": sec, "confidence": get_confidence()})


@app.route("/dustbin/lid", methods=["POST"])
def dustbin_lid_manual():
    """Manual lid: {"bin":"paper"|"plastic", "mode":"open"|"closed"|"auto"}"""
    from flask import request
    data = request.get_json(silent=True) or {}
    bin_name = data.get("bin")
    mode = data.get("mode")
    if not bin_name or mode is None:
        return jsonify({"error": "bin and mode required"}), 400
    try:
        set_lid_manual(bin_name, mode)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    with _lock:
        st = dict(dustbin_state[bin_name])
    return jsonify({"status": "ok", "bin": bin_name, "state": st})


@app.route("/config", methods=["POST"])
def update_config():
    import requests
    from flask import request
    data = request.get_json() or {}
    bin_name = data.get("bin")
    open_deg = data.get("open_deg")
    close_deg = data.get("close_deg")
    empty_cm = data.get("empty_cm")
    full_cm = data.get("full_cm")

    if bin_name not in ("paper", "plastic"):
        return jsonify({"error": "invalid bin"}), 400

    if empty_cm is not None:
        empty_cm = float(empty_cm)
        if not (5.0 <= empty_cm <= 80.0):
            return jsonify({"error": "empty_cm must be between 5 and 80"}), 400
    if full_cm is not None:
        full_cm = float(full_cm)
        if not (1.0 <= full_cm <= 30.0):
            return jsonify({"error": "full_cm must be between 1 and 30"}), 400

    eff_empty = empty_cm
    eff_full = full_cm
    with _lock:
        st = dustbin_state[bin_name]
        if eff_empty is None:
            eff_empty = st.get("empty_cm", 22.0)
        if eff_full is None:
            eff_full = st.get("full_cm", 3.0)
    if eff_full >= eff_empty:
        return jsonify({"error": "full_cm must be less than empty_cm"}), 400

    params = {}
    if open_deg is not None:
        params["open"] = int(open_deg)
    if close_deg is not None:
        params["close"] = int(close_deg)
    if empty_cm is not None:
        params["empty"] = empty_cm
    if full_cm is not None:
        params["full"] = full_cm

    if not params:
        return jsonify({"error": "no config fields provided"}), 400

    if not ESP32_ENABLED:
        with _lock:
            st = dustbin_state[bin_name]
            if open_deg is not None:
                st["open_deg"] = int(open_deg)
            if close_deg is not None:
                st["close_deg"] = int(close_deg)
            if empty_cm is not None:
                st["empty_cm"] = empty_cm
            if full_cm is not None:
                st["full_cm"] = full_cm
        return jsonify({"status": "ok", "config": dustbin_state[bin_name], "esp32": False})

    from config import ESP32_HOST, API_TIMEOUT
    url = f"http://{ESP32_HOST}/api/dustbin/{bin_name}/config"
    try:
        r = requests.post(url, params=params, timeout=API_TIMEOUT)
        r.raise_for_status()
        resp_data = r.json()

        with _lock:
            st = dustbin_state[bin_name]
            if "open_deg" in resp_data:
                st["open_deg"] = resp_data["open_deg"]
            if "close_deg" in resp_data:
                st["close_deg"] = resp_data["close_deg"]
            if "empty_cm" in resp_data:
                st["empty_cm"] = float(resp_data["empty_cm"])
            if "full_cm" in resp_data:
                st["full_cm"] = float(resp_data["full_cm"])

        return jsonify({"status": "ok", "config": dustbin_state[bin_name]})
    except Exception as e:
        log.warning("Failed to configure ESP32 [%s]: %s", url, e)
        return jsonify({"error": f"Failed to configure ESP32: {str(e)}"}), 500


_DASHBOARD_PATH = Path(__file__).resolve().parent / "templates" / "dashboard.html"


@app.route("/")
def dashboard():
    html = _DASHBOARD_PATH.read_text(encoding="utf-8")
    return render_template_string(html)


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
    if WEBSOCKET_AVAILABLE:
        log.info("WebSocket stream → ws://%s:%d/ws/video", STREAM_HOST, STREAM_PORT)
    else:
        log.warning(
            "WebSocket disabled (install: pip install flask-sock simple-websocket). "
            "Using MJPEG /video_feed only."
        )
