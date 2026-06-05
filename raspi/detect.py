# ─────────────────────────────────────────────────────────────────────────────
# detect.py  –  Main entry: stream @ 30 FPS + YOLO in parallel
# ─────────────────────────────────────────────────────────────────────────────

import time
import logging
import threading

import cv2
from ultralytics import YOLO

from config import (
    MODEL_PATH, CLASS_NAMES, CAMERA_TYPE, USB_CAMERA_INDEX, ESP32_ENABLED,
    STREAM_FPS, INFERENCE_FPS, YOLO_IMGSZ,
)
from dustbin_api import LidAutoCloseThread, FillLevelPoller
from streamer import start_stream_server
from pipeline import start_pipeline, CameraHolder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def main():
    log.info("=" * 60)
    log.info("  Trash Detection – Raspberry Pi Node")
    log.info("  Classes monitored: %s", CLASS_NAMES)
    log.info("  Stream %d FPS | YOLO %d FPS @ imgsz %d", STREAM_FPS, INFERENCE_FPS, YOLO_IMGSZ)
    log.info("=" * 60)

    log.info("Loading YOLO model: %s", MODEL_PATH)
    model = YOLO(MODEL_PATH)
    log.info("Model loaded")

    if ESP32_ENABLED:
        LidAutoCloseThread().start()
        FillLevelPoller().start()
    else:
        log.info("ESP32 disabled – stream and detection only")

    start_stream_server()

    if CAMERA_TYPE == "usb":
        log.info("Camera: USB index %d", USB_CAMERA_INDEX)
    elif CAMERA_TYPE == "pi":
        log.info("Camera: Pi Camera Module")
    else:
        log.info("Camera: auto (USB #%d then Pi cam)", USB_CAMERA_INDEX)

    stop = threading.Event()
    holder, capture, inference = start_pipeline(model, stop)

    log.info("Dashboard → http://0.0.0.0:5000/")

    try:
        while not stop.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down…")
        stop.set()
        time.sleep(0.3)
    finally:
        holder.release()
        cv2.destroyAllWindows()
        log.info("Bye!")


if __name__ == "__main__":
    main()
