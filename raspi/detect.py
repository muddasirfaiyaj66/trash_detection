# ─────────────────────────────────────────────────────────────────────────────
# detect.py  –  Main entry: stream @ 30 FPS + YOLO in parallel
# ─────────────────────────────────────────────────────────────────────────────

import time
import logging
import threading
from pathlib import Path

import cv2
from ultralytics import YOLO

from config import (
    MODEL_PATH, CLASS_NAMES, CAMERA_TYPE, USB_CAMERA_INDEX, ESP32_ENABLED,
    STREAM_FPS, INFERENCE_FPS, YOLO_IMGSZ, USE_NCNN,
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


def load_model() -> YOLO:
    """
    Load the detector. On a Pi, prefer an NCNN export (2-3x faster on ARM CPU).
    The .pt is auto-converted to best_ncnn_model/ on first run. Falls back to the
    PyTorch model if NCNN isn't installed.
    """
    log.info("Loading YOLO model: %s", MODEL_PATH)
    model = YOLO(MODEL_PATH)
    if not USE_NCNN:
        log.info("Model loaded (PyTorch)")
        return model

    ncnn_dir = Path(f"{Path(MODEL_PATH).with_suffix('')}_ncnn_model")
    try:
        if not ncnn_dir.exists():
            log.info("Exporting model to NCNN for faster Pi inference (one-time, ~1-2 min)…")
            model.export(format="ncnn", imgsz=YOLO_IMGSZ)
        log.info("Loading NCNN model: %s", ncnn_dir)
        return YOLO(str(ncnn_dir), task="detect")
    except Exception as e:
        log.warning(
            "NCNN unavailable (%s). Using PyTorch model (slower). "
            "For faster detection run: pip install ncnn", e,
        )
        return model


def main():
    log.info("=" * 60)
    log.info("  Trash Detection – Raspberry Pi Node")
    log.info("  Classes monitored: %s", CLASS_NAMES)
    log.info("  Stream %d FPS | YOLO %d FPS @ imgsz %d", STREAM_FPS, INFERENCE_FPS, YOLO_IMGSZ)
    log.info("=" * 60)

    model = load_model()
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
