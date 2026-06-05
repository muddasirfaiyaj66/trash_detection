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
    CONFIDENCE, LINE_WIDTH, DETECT_CLASSES, CLASS_NAMES,
    MJPEG_QUALITY, STREAM_FPS, INFERENCE_FPS, YOLO_IMGSZ,
    CAMERA_SOURCE,
)
from camera import initialize_camera
from dustbin_api import on_detection
from streamer import push_frame, set_camera_status

log = logging.getLogger(__name__)

# BGR box colors per class
_BOX_COLORS = {2: (255, 120, 60), 3: (60, 200, 120)}


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
            boxes = self._boxes if (time.time() - self._boxes_ts) < 0.6 else []
        _draw_boxes(out, boxes)
        return out


def _draw_boxes(frame: np.ndarray, boxes: list) -> None:
    for cls_id, conf, x1, y1, x2, y2 in boxes:
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


class CaptureStreamThread(threading.Thread):
    """Reads camera and pushes JPEGs at STREAM_FPS — never blocked by YOLO."""

    def __init__(self, holder: CameraHolder, hub: FrameHub, stop: threading.Event):
        super().__init__(daemon=True, name="CaptureStream")
        self.holder = holder
        self.hub = hub
        self.stop = stop
        self.fail_streak = 0

    def run(self):
        log.info("Capture stream started (target %d FPS)", STREAM_FPS)
        interval = 1.0 / max(STREAM_FPS, 1)
        while not self.stop.is_set():
            t0 = time.perf_counter()
            ret, frame = self.holder.read()
            if not ret or frame is None or frame.size == 0:
                self._handle_read_fail()
                time.sleep(0.01)
                continue

            self.fail_streak = 0
            set_camera_status(True, None)
            self.hub.set_frame(frame)

            composed = self.hub.compose_stream_frame()
            if composed is not None:
                push_frame(_encode_jpeg(composed))

            elapsed = time.perf_counter() - t0
            wait = interval - elapsed
            if wait > 0:
                time.sleep(wait)

    def _handle_read_fail(self):
        self.fail_streak += 1
        if self.fail_streak == 1 or self.fail_streak % 20 == 0:
            log.warning("Frame grab failed (%d) in capture thread", self.fail_streak)
        if self.fail_streak >= 60:
            set_camera_status(False, "Camera stalled — reconnecting…")
            new_cap = initialize_camera(CAMERA_SOURCE)
            if new_cap is not None:
                self.holder.replace(new_cap)
                self.fail_streak = 0
                set_camera_status(True, None)
                log.info("Camera reopened (capture thread)")


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
            frame = self.hub.snapshot()
            if frame is not None:
                results = self.model.predict(
                    source=frame,
                    conf=CONFIDENCE,
                    classes=DETECT_CLASSES,
                    imgsz=YOLO_IMGSZ,
                    verbose=False,
                )
                boxes = _extract_boxes(results)
                self.hub.set_boxes(boxes)
                self._count_inference()
                for cls_id, conf, *_ in boxes:
                    log.info(
                        "Detected: class %d (%s) conf=%.2f",
                        cls_id, CLASS_NAMES.get(cls_id, "?"), conf,
                    )
                    on_detection(cls_id)

            elapsed = time.perf_counter() - t0
            wait = interval - elapsed
            if wait > 0:
                time.sleep(wait)

    def _count_inference(self):
        self._inf_cnt += 1
        now = time.time()
        dt = now - self._inf_ts
        if dt >= 1.0:
            self.inference_fps = self._inf_cnt / dt
            self._inf_cnt = 0
            self._inf_ts = now


def open_camera_with_retry(holder: CameraHolder) -> bool:
    while True:
        cap = initialize_camera(CAMERA_SOURCE)
        if cap is not None:
            holder.set(cap)
            set_camera_status(True, None)
            return True
        set_camera_status(False, "Cannot open camera — retrying…")
        log.error("Camera open failed — retry in 3s")
        time.sleep(3)


def start_pipeline(model: YOLO, stop: threading.Event) -> tuple[CameraHolder, CaptureStreamThread, InferenceThread]:
    holder = CameraHolder()
    open_camera_with_retry(holder)
    hub = FrameHub()
    capture = CaptureStreamThread(holder, hub, stop)
    inference = InferenceThread(model, hub, stop)
    capture.start()
    inference.start()
    return holder, capture, inference
