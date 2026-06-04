# ─────────────────────────────────────────────────────────────────────────────
# detect.py  –  Main YOLO detection loop for Raspberry Pi
# ─────────────────────────────────────────────────────────────────────────────
#
#  Run:  python detect.py
#
#  What it does:
#   1. Loads best.pt with Ultralytics YOLO
#   2. Streams frames from camera, running inference on each
#   3. For class 2 (paper)   → triggers paper dustbin lid open API
#      For class 3 (plastic) → triggers plastic dustbin lid open API
#   4. Starts fill-level poller (polls both dustbins every N seconds)
#   5. Starts lid auto-close timer thread
#   6. Starts Flask MJPEG stream server so ground-station can view feed
#      at  http://<raspi-ip>:5000/
# ─────────────────────────────────────────────────────────────────────────────

import cv2
import time
import logging
import numpy as np
from ultralytics import YOLO

from config import (
    MODEL_PATH, CONFIDENCE, LINE_WIDTH,
    DETECT_CLASSES, CLASS_NAMES,
    CAMERA_SOURCE, MJPEG_QUALITY,
)
from dustbin_api import (
    on_detection,
    LidAutoCloseThread,
    FillLevelPoller,
)
from streamer import start_stream_server, push_frame

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
def annotate_frame(frame: np.ndarray, results) -> np.ndarray:
    """
    Draw bounding boxes + labels on the frame using OpenCV.
    Uses YOLO's built-in .plot() for clean rendering.
    """
    return results[0].plot(line_width=LINE_WIDTH)


def encode_jpeg(frame: np.ndarray) -> bytes:
    """Encode an OpenCV BGR frame as JPEG bytes."""
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, MJPEG_QUALITY])
    return bytes(buf) if ok else b""


# ─────────────────────────────────────────────────────────────────────────────
def initialize_camera(source) -> cv2.VideoCapture | None:
    """
    Attempts to open a camera connection. Supports GStreamer libcamerasrc for Pi Camera v3,
    V4L2 for USB cameras, and scans backup indices if the primary fails.
    """
    # If source is a string (e.g. RTSP url or file path), try it directly
    if isinstance(source, str) and not source.isdigit():
        log.info("Trying to open video stream/file: %s", source)
        cap = cv2.VideoCapture(source)
        if cap.isOpened():
            return cap
        return None

    # Source is an integer index (or string digit)
    idx = int(source)

    # Method 1: Try GStreamer Libcamerasrc (recommended for Raspberry Pi Camera Module)
    # Bookworm OS uses libcamerasrc which doesn't expose a standard v4l2 device
    gst_pipeline = (
        "libcamerasrc ! video/x-raw, width=640, height=480, framerate=30/1 ! "
        "videoconvert ! appsink drop=true max-buffers=1"
    )
    log.info("Trying GStreamer pipeline for Pi Camera Module…")
    try:
        cap = cv2.VideoCapture(gst_pipeline, cv2.CAP_GSTREAMER)
        if cap.isOpened():
            log.info("Successfully opened Pi Camera via GStreamer/libcamerasrc ✓")
            return cap
    except Exception as e:
        log.debug("GStreamer Pi Camera failed: %s", e)

    # Method 2: Try V4L2 backend directly (Standard for USB Cameras on Linux/Pi)
    log.info("Trying USB camera index %d using V4L2 backend…", idx)
    try:
        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if cap.isOpened():
            # Test frame grab
            ret, _ = cap.read()
            if ret:
                log.info("Successfully opened USB/V4L2 camera on index %d ✓", idx)
                return cap
            cap.release()
    except Exception as e:
        log.debug("V4L2 camera open failed: %s", e)

    # Method 3: Try default OpenCV backend
    log.info("Trying default camera backend on index %d…", idx)
    try:
        cap = cv2.VideoCapture(idx)
        if cap.isOpened():
            log.info("Successfully opened camera with default backend on index %d ✓", idx)
            return cap
    except Exception as e:
        log.debug("Default backend failed: %s", e)

    # Method 4: Scan alternative camera indices
    log.info("Scanning alternative camera indices (0, 1, 2, 4)…")
    for alt_idx in [0, 1, 2, 4]:
        if alt_idx == idx:
            continue
        log.info("Trying index %d via V4L2…", alt_idx)
        try:
            cap = cv2.VideoCapture(alt_idx, cv2.CAP_V4L2)
            if cap.isOpened():
                ret, _ = cap.read()
                if ret:
                    log.info("Found working camera on alternative index %d ✓", alt_idx)
                    return cap
                cap.release()
        except Exception:
            pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("  Trash Detection – Raspberry Pi Node")
    log.info("  Classes monitored: %s", CLASS_NAMES)
    log.info("=" * 60)

    # 1. Load model
    log.info("Loading YOLO model: %s", MODEL_PATH)
    model = YOLO(MODEL_PATH)
    log.info("Model loaded ✓")

    # 2. Start background services
    LidAutoCloseThread().start()
    FillLevelPoller().start()
    start_stream_server()

    # 3. Open camera
    log.info("Opening camera source: %s", CAMERA_SOURCE)
    cap = initialize_camera(CAMERA_SOURCE)
    if cap is None:
        log.error("CRITICAL: Cannot open any camera source (checked GStreamer and V4L2)")
        return

    log.info("Camera opened ✓  –  Starting detection loop…")
    log.info("Ground-station stream → http://0.0.0.0:5000/")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                log.warning("Frame grab failed – retrying…")
                time.sleep(0.1)
                continue

            # 4. Run YOLO inference (stream=True is memory-efficient)
            results = model.predict(
                source=frame,
                conf=CONFIDENCE,
                classes=DETECT_CLASSES,
                line_width=LINE_WIDTH,
                verbose=False,
            )

            # 5. Process detections → trigger dustbin API
            if results and results[0].boxes is not None:
                for box in results[0].boxes:
                    cls_id = int(box.cls[0].item())
                    conf   = float(box.conf[0].item())
                    log.info(
                        "🗑️  Detected: class %d (%s)  conf=%.2f",
                        cls_id, CLASS_NAMES.get(cls_id, "?"), conf,
                    )
                    on_detection(cls_id)

            # 6. Annotate frame and push to MJPEG stream
            annotated = annotate_frame(frame, results)
            push_frame(encode_jpeg(annotated))

            # Optional: show local window on raspi (comment out if headless)
            # cv2.imshow("Trash Detection", annotated)
            # if cv2.waitKey(1) & 0xFF == ord('q'):
            #     break

    except KeyboardInterrupt:
        log.info("Interrupted by user – shutting down.")
    finally:
        cap.release()
        cv2.destroyAllWindows()
        log.info("Camera released. Bye!")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()
