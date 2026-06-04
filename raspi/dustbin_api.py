# ─────────────────────────────────────────────────────────────────────────────
# dustbin_api.py  –  Handles all dustbin lid & fill-level API calls
# ─────────────────────────────────────────────────────────────────────────────

import time
import threading
import logging
import requests
from config import (
    PAPER_LID_OPEN_EP, PAPER_LID_CLOSE_EP, PAPER_STATUS_EP,
    PLASTIC_LID_OPEN_EP, PLASTIC_LID_CLOSE_EP, PLASTIC_STATUS_EP,
    LID_OPEN_DURATION, LEVEL_POLL_INTERVAL, API_TIMEOUT,
    CLASS_NAMES,
)

log = logging.getLogger(__name__)

# ── Shared state (thread-safe via lock) ──────────────────────────────────────
_lock = threading.Lock()

dustbin_state = {
    "paper":   {"lid": "closed", "level_pct": 0, "last_detected": 0, "open_deg": 90, "close_deg": 0},
    "plastic": {"lid": "closed", "level_pct": 0, "last_detected": 0, "open_deg": 90, "close_deg": 0},
}

# ── Endpoint maps ─────────────────────────────────────────────────────────────
_OPEN_EP   = {"paper": PAPER_LID_OPEN_EP,   "plastic": PLASTIC_LID_OPEN_EP}
_CLOSE_EP  = {"paper": PAPER_LID_CLOSE_EP,  "plastic": PLASTIC_LID_CLOSE_EP}
_STATUS_EP = {"paper": PAPER_STATUS_EP, "plastic": PLASTIC_STATUS_EP}

# ── Class index → dustbin name ────────────────────────────────────────────────
CLASS_TO_BIN = {2: "paper", 3: "plastic"}


# ─────────────────────────────────────────────────────────────────────────────
def _post(url: str, payload: dict = None) -> dict | None:
    """HTTP POST with timeout; returns JSON or None on error."""
    try:
        r = requests.post(url, json=payload or {}, timeout=API_TIMEOUT)
        r.raise_for_status()
        return r.json() if r.content else {}
    except Exception as e:
        log.warning("API POST failed [%s]: %s", url, e)
        return None


def _get(url: str) -> dict | None:
    """HTTP GET with timeout; returns JSON or None on error."""
    try:
        r = requests.get(url, timeout=API_TIMEOUT)
        r.raise_for_status()
        return r.json() if r.content else {}
    except Exception as e:
        log.warning("API GET failed [%s]: %s", url, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
def open_lid(bin_name: str):
    """Open the lid for the named dustbin and update shared state."""
    log.info("🔓 Opening lid → %s dustbin", bin_name)
    resp = _post(_OPEN_EP[bin_name])
    with _lock:
        dustbin_state[bin_name]["lid"] = "open"
        dustbin_state[bin_name]["last_detected"] = time.time()
    log.debug("Open lid response: %s", resp)


def close_lid(bin_name: str):
    """Close the lid for the named dustbin and update shared state."""
    log.info("🔒 Closing lid → %s dustbin", bin_name)
    resp = _post(_CLOSE_EP[bin_name])
    with _lock:
        dustbin_state[bin_name]["lid"] = "closed"
    log.debug("Close lid response: %s", resp)


def on_detection(class_id: int):
    """
    Called whenever YOLO detects class_id.
    Opens the matching dustbin lid (idempotent – won't re-send if already open).
    Updates the last_detected timestamp so the auto-close timer resets.
    """
    bin_name = CLASS_TO_BIN.get(class_id)
    if bin_name is None:
        return
    with _lock:
        already_open = dustbin_state[bin_name]["lid"] == "open"
        dustbin_state[bin_name]["last_detected"] = time.time()

    if not already_open:
        # Send in a background thread so detection loop isn't blocked
        threading.Thread(target=open_lid, args=(bin_name,), daemon=True).start()
    else:
        log.debug("Lid already open for %s – refreshing timer only", bin_name)


# ─────────────────────────────────────────────────────────────────────────────
class LidAutoCloseThread(threading.Thread):
    """
    Monitors last_detected timestamps and closes lids after LID_OPEN_DURATION
    seconds of inactivity.
    """
    def __init__(self):
        super().__init__(daemon=True, name="LidAutoClose")

    def run(self):
        log.info("LidAutoCloseThread started (timeout=%.1fs)", LID_OPEN_DURATION)
        while True:
            time.sleep(0.5)
            now = time.time()
            for bin_name in ("paper", "plastic"):
                with _lock:
                    lid_open       = dustbin_state[bin_name]["lid"] == "open"
                    last_detected  = dustbin_state[bin_name]["last_detected"]
                if lid_open and (now - last_detected) >= LID_OPEN_DURATION:
                    threading.Thread(target=close_lid, args=(bin_name,), daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
class FillLevelPoller(threading.Thread):
    """
    Periodically polls the status API for both dustbins and stores
    the result in dustbin_state so the ground-station dashboard can read it.
    """
    def __init__(self):
        super().__init__(daemon=True, name="FillLevelPoller")

    def run(self):
        log.info("FillLevelPoller started (interval=%.1fs)", LEVEL_POLL_INTERVAL)
        while True:
            for bin_name, url in _STATUS_EP.items():
                data = _get(url)
                if data:
                    pct = int(data.get("level", 0))
                    open_deg = int(data.get("open_deg", 90))
                    close_deg = int(data.get("close_deg", 0))
                    with _lock:
                        dustbin_state[bin_name]["level_pct"] = pct
                        dustbin_state[bin_name]["open_deg"] = open_deg
                        dustbin_state[bin_name]["close_deg"] = close_deg
                    log.debug("Fill level/config [%s] = %d%% (open: %d°, close: %d°)", bin_name, pct, open_deg, close_deg)
            time.sleep(LEVEL_POLL_INTERVAL)
