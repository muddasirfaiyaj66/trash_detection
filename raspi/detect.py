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
    CAMERA_SOURCE, CAMERA_TYPE, USB_CAMERA_INDEX,
    MJPEG_QUALITY, ESP32_ENABLED,
)
from camera import initialize_camera, reopen_camera
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
def main():
    log.info("=" * 60)
    log.info("  Trash Detection – Raspberry Pi Node")
    log.info("  Classes monitored: %s", CLASS_NAMES)
    log.info("=" * 60)

    # 1. Load model
    log.info("Loading YOLO model: %s", MODEL_PATH)
    model = YOLO(MODEL_PATH)
    log.info("Model loaded ✓")

    # 2. Start background services (ESP32 optional for streaming-only runs)
    if ESP32_ENABLED:
        LidAutoCloseThread().start()
        FillLevelPoller().start()
    else:
        log.info("ESP32 disabled – stream and detection only (no lid/level API)")
    start_stream_server()

    # 3. Open camera
    if CAMERA_TYPE == "usb":
        log.info("Camera: USB webcam (index %d)", USB_CAMERA_INDEX)
    elif CAMERA_TYPE == "pi":
        log.info("Camera: Pi Camera Module (Picamera2 / libcamera)")
    else:
        log.info("Camera: auto (USB index %d, then Pi cam)", USB_CAMERA_INDEX)
    cap = initialize_camera(CAMERA_SOURCE)
    if cap is None:
        log.error(
            "CRITICAL: Cannot open camera (CAMERA_TYPE=%s). "
            "See raspi/README.md — USB: v4l2-ctl; Pi: picamera2",
            CAMERA_TYPE,
        )
        return

    log.info("Camera opened ✓  –  Starting detection loop…")
    log.info("Ground-station stream → http://0.0.0.0:5000/")

    fail_streak = 0
    try:
        while True:
            ret, frame = cap.read()
            if not ret or frame is None or frame.size == 0:
                fail_streak += 1
                if fail_streak == 1 or fail_streak % 10 == 0:
                    log.warning(
                        "Frame grab failed (%d consecutive) – retrying…",
                        fail_streak,
                    )
                if fail_streak >= 50:
                    new_cap = reopen_camera(cap)
                    if new_cap is not None:
                        cap = new_cap
                        fail_streak = 0
                        log.info("Camera reopened")
                    else:
                        log.error("Could not reopen camera")
                time.sleep(0.05)
                continue
            fail_streak = 0

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
