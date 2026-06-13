# ─────────────────────────────────────────────────────────────────────────────
# detect.py  –  Main entry: stream @ 30 FPS + YOLO in parallel
# ─────────────────────────────────────────────────────────────────────────────

import os

# Cap native thread pools BEFORE cv2/torch/ncnn initialise them, so YOLO doesn't
# peg all cores and overheat the Pi. Must run before the heavy imports below.
_cores = os.cpu_count() or 4
_threads = os.environ.get("CPU_THREADS") or str(max(1, _cores - 1))
if _threads != "0":
    for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(_v, _threads)

import time
import shutil
import logging
import threading
from pathlib import Path

import cv2
from ultralytics import YOLO

from config import (
    MODEL_PATH, CLASS_NAMES, CAMERA_TYPE, USB_CAMERA_INDEX, ESP32_ENABLED,
    STREAM_FPS, INFERENCE_FPS, YOLO_IMGSZ, USE_NCNN, CPU_THREADS, BOX_SCALE,
    CAMERA_WIDTH, CAMERA_HEIGHT, MJPEG_QUALITY, CONFIDENCE, get_confidence,
)

try:
    cv2.setNumThreads(CPU_THREADS if CPU_THREADS > 0 else _cores)
except Exception:
    pass
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
    marker = ncnn_dir / ".imgsz"

    # Re-export if: no export yet, imgsz changed, or best.pt is newer than the
    # export (i.e. you uploaded a new model). Prevents a stale/mismatched NCNN.
    need_export = True
    if ncnn_dir.exists() and marker.exists():
        try:
            same_size = marker.read_text().strip() == str(YOLO_IMGSZ)
            fresh = ncnn_dir.stat().st_mtime >= Path(MODEL_PATH).stat().st_mtime
            need_export = not (same_size and fresh)
        except Exception:
            need_export = True

    try:
        if need_export:
            log.info("Exporting model to NCNN at imgsz=%d (one-time, ~1-2 min)…", YOLO_IMGSZ)
            if ncnn_dir.exists():
                shutil.rmtree(ncnn_dir, ignore_errors=True)
            model.export(format="ncnn", imgsz=YOLO_IMGSZ)
            try:
                marker.write_text(str(YOLO_IMGSZ))
            except Exception:
                pass
        log.info("Loading NCNN model: %s (imgsz=%d)", ncnn_dir, YOLO_IMGSZ)
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
    log.info("  Camera %dx%d | MJPEG quality %d | box scale %.0f%%", CAMERA_WIDTH, CAMERA_HEIGHT, MJPEG_QUALITY, BOX_SCALE * 100)
    log.info("  Lid opens at ≥ %.0f%% confidence", get_confidence() * 100)
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
