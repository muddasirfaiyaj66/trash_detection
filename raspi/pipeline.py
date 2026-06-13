# ─────────────────────────────────────────────────────────────────────────────
# pipeline.py  –  Decoupled capture/stream + YOLO inference threads
# ─────────────────────────────────────────────────────────────────────────────

import time
import threading
import logging

import cv2
import numpy as np
from ultralytics import YOLO

from config import (
    get_confidence, YOLO_MIN_CONF, LINE_WIDTH, BOX_SCALE, DETECT_CLASSES, CLASS_NAMES,
    MJPEG_QUALITY, STREAM_FPS, INFERENCE_FPS, YOLO_IMGSZ,
    CAMERA_REOPEN_THRESHOLD, MODEL_WARMUP, MODEL_WARMUP_TIMEOUT,
    DETECTION_TTL, THERMAL_GUARD, THERMAL_MAX_TEMP, THERMAL_RESUME_TEMP, THERMAL_POLL,
)
from camera import initialize_camera, consume_switch_request, get_camera_mode, apply_transform
from dustbin_api import on_detection
from streamer import push_frame, set_camera_status

log = logging.getLogger(__name__)

# BGR box colors per class (1=paper, 2=plastic)
_BOX_COLORS = {1: (255, 120, 60), 2: (60, 200, 120)}

# ── Thermal protection ────────────────────────────────────────────────────────
_thermal_lock = threading.Lock()
_cpu_temp = 0.0
_throttled = False


def get_cpu_temp() -> float:
    """CPU temperature in °C (Linux thermal zone), or 0.0 if unavailable."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        return 0.0


def thermal_state() -> dict:
    with _thermal_lock:
        return {"cpu_temp": round(_cpu_temp, 1), "throttled": _throttled}


def _is_throttled() -> bool:
    with _thermal_lock:
        return _throttled


class ThermalGuard(threading.Thread):
    """Watches CPU temp and throttles inference before the Pi overheats/powers off."""

    def __init__(self, stop: threading.Event):
        super().__init__(daemon=True, name="ThermalGuard")
        self.stop = stop

    def run(self):
        global _cpu_temp, _throttled
        if not THERMAL_GUARD:
            return
        if get_cpu_temp() <= 0:
            log.info("Thermal guard: no temp sensor found — disabled")
            return
        log.info("Thermal guard on (throttle ≥ %.0f°C, resume ≤ %.0f°C)", THERMAL_MAX_TEMP, THERMAL_RESUME_TEMP)
        while not self.stop.is_set():
            t = get_cpu_temp()
            with _thermal_lock:
                _cpu_temp = t
                if not _throttled and t >= THERMAL_MAX_TEMP:
                    _throttled = True
                    log.warning("CPU %.1f°C ≥ %.0f°C — throttling inference to cool down", t, THERMAL_MAX_TEMP)
                elif _throttled and t <= THERMAL_RESUME_TEMP:
                    _throttled = False
                    log.info("CPU cooled to %.1f°C — resuming normal inference", t)
            steps = max(1, int(THERMAL_POLL * 10))
            for _ in range(steps):
                if self.stop.is_set():
                    break
                time.sleep(0.1)


class CameraHolder:
    """Thread-safe wrapper so only the capture thread reads from VideoCapture."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cap = None

    def set(self, cap):
        with self._lock:
            self._cap = cap

    def read(self):
        with self._lock:
            cap = self._cap
        if cap is None:
            return False, None
        return cap.read()

    def replace(self, new_cap):
        with self._lock:
            old, self._cap = self._cap, new_cap
        if old is not None:
            try:
                old.release()
            except Exception:
                pass

    def release(self):
        self.replace(None)


class FrameHub:
    """Latest camera frame + most recent detections for overlay."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self._boxes = []
        self._boxes_ts = 0.0

    def set_frame(self, frame: np.ndarray):
        with self._lock:
            self._frame = frame

    def snapshot(self) -> np.ndarray | None:
        with self._lock:
            if self._frame is None:
                return None
            return self._frame.copy()

    def set_boxes(self, boxes: list):
        with self._lock:
            self._boxes = boxes
            self._boxes_ts = time.time()

    def compose_stream_frame(self) -> np.ndarray | None:
        with self._lock:
            if self._frame is None:
                return None
            out = self._frame.copy()
            boxes = self._boxes if (time.time() - self._boxes_ts) < DETECTION_TTL else []
        _draw_boxes(out, boxes)
        return out


def _scale_box(x1, y1, x2, y2, factor: float, w: int, h: int):
    if factor >= 1.0:
        return x1, y1, x2, y2
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    hw, hh = (x2 - x1) * factor / 2, (y2 - y1) * factor / 2
    return (
        max(0, cx - hw), max(0, cy - hh),
        min(w - 1, cx + hw), min(h - 1, cy + hh),
    )


def _draw_boxes(frame: np.ndarray, boxes: list) -> None:
    h, w = frame.shape[:2]
    for cls_id, conf, x1, y1, x2, y2 in boxes:
        x1, y1, x2, y2 = _scale_box(x1, y1, x2, y2, BOX_SCALE, w, h)
        color = _BOX_COLORS.get(cls_id, (180, 180, 180))
        p1, p2 = (int(x1), int(y1)), (int(x2), int(y2))
        cv2.rectangle(frame, p1, p2, color, LINE_WIDTH)
        label = f"{CLASS_NAMES.get(cls_id, '?')} {conf:.2f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(frame, (p1[0], p1[1] - th - 8), (p1[0] + tw + 4, p1[1]), color, -1)
        cv2.putText(
            frame, label, (p1[0] + 2, p1[1] - 4),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA,
        )


def _encode_jpeg(frame: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, MJPEG_QUALITY])
    return bytes(buf) if ok else b""


def _extract_boxes(results) -> list:
    boxes = []
    if not results or results[0].boxes is None:
        return boxes
    for box in results[0].boxes:
        cls_id = int(box.cls[0].item())
        conf = float(box.conf[0].item())
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        boxes.append((cls_id, conf, x1, y1, x2, y2))
    return boxes


def _warmup_model(model: YOLO, hub: FrameHub) -> None:
    if not MODEL_WARMUP:
        return
    log.info("Warming up YOLO on first frame...")
    deadline = time.time() + MODEL_WARMUP_TIMEOUT
    while time.time() < deadline:
        frame = hub.snapshot()
        if frame is None:
            time.sleep(0.02)
            continue
        try:
            results = model.predict(
                source=frame,
                conf=YOLO_MIN_CONF,
                classes=DETECT_CLASSES,
                imgsz=YOLO_IMGSZ,
                verbose=False,
            )
            boxes = _extract_boxes(results)
            if boxes:
                hub.set_boxes(boxes)
            log.info("YOLO warmup complete")
        except Exception as e:
            log.warning("YOLO warmup failed: %s", e)
        return
    log.warning("YOLO warmup skipped (no frame within %.1fs)", MODEL_WARMUP_TIMEOUT)


class CaptureStreamThread(threading.Thread):
    """Reads camera and pushes JPEGs at STREAM_FPS — never blocked by YOLO."""

    def __init__(self, holder: CameraHolder, hub: FrameHub, stop: threading.Event):
        super().__init__(daemon=True, name="CaptureStream")
        self.holder = holder
        self.hub = hub
        self.stop = stop
        self.fail_streak = 0

    def run(self):
        log.info("Capture stream started (target %d FPS, recovery at %d failures)", STREAM_FPS, CAMERA_REOPEN_THRESHOLD)
        # Open the camera from inside the thread so the dashboard stays live and
        # mode-switching works even if no camera is connected yet.
        open_camera_with_retry(self.holder, self.stop)
        interval = 1.0 / max(STREAM_FPS, 1)
        last_frame_time = time.time()
        
        while not self.stop.is_set():
            t0 = time.perf_counter()

            # Apply a dashboard-requested camera switch (usb ↔ pi)
            if consume_switch_request():
                self._apply_switch()
                continue

            ret, frame = self.holder.read()
            if not ret or frame is None or frame.size == 0:
                self._handle_read_fail()
                time.sleep(0.01)
                continue

            frame = apply_transform(frame)
            last_frame_time = time.time()
            self.fail_streak = 0
            set_camera_status(True, None)
            self.hub.set_frame(frame)

            composed = self.hub.compose_stream_frame()
            if composed is not None:
                push_frame(_encode_jpeg(composed))

            elapsed = time.perf_counter() - t0
            # Halve the stream rate while throttled to cut encode load (less heat).
            eff_interval = interval * 2 if _is_throttled() else interval
            wait = eff_interval - elapsed
            if wait > 0:
                time.sleep(wait)

    def _apply_switch(self):
        mode = get_camera_mode()
        log.info("Switching camera → %s (usb_index=%s)", mode["type"], mode["usb_index"])
        set_camera_status(False, f"Switching to {mode['type'].upper()} camera…")
        new_cap = initialize_camera()
        if new_cap is not None:
            self.holder.replace(new_cap)
            self.fail_streak = 0
            set_camera_status(True, None)
            log.info("Camera switched to %s", mode["type"])
        else:
            self.holder.release()
            set_camera_status(False, f"{mode['type'].upper()} camera not found — check connection")
            log.error("Camera switch to %s failed", mode["type"])

    def _handle_read_fail(self):
        self.fail_streak += 1
        if self.fail_streak == 1:
            log.warning("Frame grab failed in capture thread")
        elif self.fail_streak % 10 == 0:
            log.warning("Frame grab failed (%d consecutive) — camera may be stalled", self.fail_streak)
        
        if self.fail_streak >= CAMERA_REOPEN_THRESHOLD:
            set_camera_status(False, f"Camera stalled ({self.fail_streak} frames) — reconnecting…")
            log.error("Reopening camera after %d consecutive failures", self.fail_streak)
            new_cap = initialize_camera()
            if new_cap is not None:
                self.holder.replace(new_cap)
                self.fail_streak = 0
                set_camera_status(True, None)
                log.info("Camera reopened successfully (capture thread)")
            else:
                log.error("Failed to reopen camera — will retry next iteration")


class InferenceThread(threading.Thread):
    """Runs YOLO at INFERENCE_FPS on snapshots — does not block the stream."""

    def __init__(self, model: YOLO, hub: FrameHub, stop: threading.Event):
        super().__init__(daemon=True, name="YOLOInference")
        self.model = model
        self.hub = hub
        self.stop = stop
        self.inference_fps = 0.0
        self._inf_cnt = 0
        self._inf_ts = time.time()

    def run(self):
        log.info("YOLO inference started (target %d FPS, imgsz=%d)", INFERENCE_FPS, YOLO_IMGSZ)
        interval = 1.0 / max(INFERENCE_FPS, 1)
        while not self.stop.is_set():
            t0 = time.perf_counter()
            # When the Pi is hot, run inference at ~1 FPS so the CPU can cool.
            if _is_throttled():
                frame = self.hub.snapshot()
                if frame is not None:
                    self._run_once(frame)
                time.sleep(1.0)
                continue
            frame = self.hub.snapshot()
            if frame is not None:
                self._run_once(frame)

            elapsed = time.perf_counter() - t0
            wait = interval - elapsed
            if wait > 0:
                time.sleep(wait)

    def _run_once(self, frame):
        results = self.model.predict(
            source=frame,
            conf=YOLO_MIN_CONF,
            classes=DETECT_CLASSES,
            imgsz=YOLO_IMGSZ,
            verbose=False,
        )
        boxes = _extract_boxes(results)
        self.hub.set_boxes(boxes)
        self._count_inference()
        lid_threshold = get_confidence()
        for cls_id, conf, *_ in boxes:
            log.info(
                "Detected: class %d (%s) conf=%.2f",
                cls_id, CLASS_NAMES.get(cls_id, "?"), conf,
            )
            if conf >= lid_threshold:
                on_detection(cls_id, conf)
            else:
                log.debug(
                    "Below lid threshold (%.0f%%): %s conf=%.2f",
                    lid_threshold * 100, CLASS_NAMES.get(cls_id, "?"), conf,
                )

    def _count_inference(self):
        self._inf_cnt += 1
        now = time.time()
        dt = now - self._inf_ts
        if dt >= 1.0:
            self.inference_fps = self._inf_cnt / dt
            self._inf_cnt = 0
            self._inf_ts = now


def open_camera_with_retry(holder: CameraHolder, stop: threading.Event) -> bool:
    """Open the camera, retrying — but keep checking for switch requests so the
    dashboard can change the mode even while the current camera is missing."""
    while not stop.is_set():
        consume_switch_request()  # clear any pending flag; we read live mode below
        cap = initialize_camera()
        if cap is not None:
            holder.set(cap)
            set_camera_status(True, None)
            return True
        mode = get_camera_mode()["type"]
        set_camera_status(False, f"{mode.upper()} camera not found — retrying (you can switch mode)")
        log.error("Camera open failed (mode=%s) — retry in 3s", mode)
        # Wait up to 3s but break early if a switch is requested
        for _ in range(30):
            if stop.is_set() or _switch_pending():
                break
            time.sleep(0.1)
    return False


def _switch_pending() -> bool:
    from camera import _switch_event
    return _switch_event.is_set()


def start_pipeline(model: YOLO, stop: threading.Event) -> tuple[CameraHolder, CaptureStreamThread, InferenceThread]:
    holder = CameraHolder()
    hub = FrameHub()
    capture = CaptureStreamThread(holder, hub, stop)
    capture.start()
    ThermalGuard(stop).start()
    _warmup_model(model, hub)
    inference = InferenceThread(model, hub, stop)
    inference.start()
    return holder, capture, inference
