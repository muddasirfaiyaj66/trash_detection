# ─────────────────────────────────────────────────────────────────────────────
# dustbin_api.py  –  Handles all dustbin lid & fill-level API calls
# ─────────────────────────────────────────────────────────────────────────────

import time
import threading
import logging
import requests
from requests.adapters import HTTPAdapter
from config import (
    PAPER_LID_OPEN_EP, PAPER_LID_CLOSE_EP, PAPER_STATUS_EP,
    PLASTIC_LID_OPEN_EP, PLASTIC_LID_CLOSE_EP, PLASTIC_STATUS_EP,
    LID_OPEN_DURATION, LEVEL_POLL_INTERVAL, API_TIMEOUT,
    CLASS_NAMES, ESP32_ENABLED,
)

log = logging.getLogger(__name__)

# Persistent session → keep-alive connection reuse means the link to the ESP32
# stays warm (no repeated mDNS lookups / TCP handshakes), so lid commands fire
# almost instantly instead of taking hundreds of ms each.
_session = requests.Session()
_session.headers.update({"Connection": "keep-alive"})
_adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)

# ── Shared state (thread-safe via lock) ──────────────────────────────────────
_lock = threading.Lock()

dustbin_state = {
    "paper":   {"lid": "closed", "level_pct": 0, "last_detected": 0, "open_deg": 0,  "close_deg": 134, "manual": None},
    "plastic": {"lid": "closed", "level_pct": 0, "last_detected": 0, "open_deg": 45, "close_deg": 168, "manual": None},
}

# ── Endpoint maps ─────────────────────────────────────────────────────────────
_OPEN_EP   = {"paper": PAPER_LID_OPEN_EP,   "plastic": PLASTIC_LID_OPEN_EP}
_CLOSE_EP  = {"paper": PAPER_LID_CLOSE_EP,  "plastic": PLASTIC_LID_CLOSE_EP}
_STATUS_EP = {"paper": PAPER_STATUS_EP, "plastic": PLASTIC_STATUS_EP}

# Class index → dustbin name (data.yaml: 1=paper, 2=plastic)
CLASS_TO_BIN = {1: "paper", 2: "plastic"}


# ─────────────────────────────────────────────────────────────────────────────
def _post(url: str, payload: dict = None) -> dict | None:
    """HTTP POST with timeout; returns JSON or None on error."""
    if not ESP32_ENABLED:
        return None
    try:
        r = _session.post(url, json=payload or {}, timeout=API_TIMEOUT)
        r.raise_for_status()
        return r.json() if r.content else {}
    except Exception as e:
        if ESP32_ENABLED:
            log.warning("API POST failed [%s]: %s", url, e)
        return None


def _get(url: str) -> dict | None:
    """HTTP GET with timeout; returns JSON or None on error."""
    if not ESP32_ENABLED:
        return None
    try:
        r = _session.get(url, timeout=API_TIMEOUT)
        r.raise_for_status()
        return r.json() if r.content else {}
    except Exception as e:
        if ESP32_ENABLED:
            log.warning("API GET failed [%s]: %s", url, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
def open_lid(bin_name: str):
    """Open the lid for the named dustbin and update shared state."""
    log.info("🔓 Opening lid → %s dustbin", bin_name)
    resp = _post(_OPEN_EP[bin_name])
    with _lock:
        if resp is not None:
            lid = resp.get("lid", "open")
            dustbin_state[bin_name]["lid"] = "open" if lid in ("open", "opening") else lid
            dustbin_state[bin_name]["last_detected"] = time.time()
        elif not ESP32_ENABLED:
            dustbin_state[bin_name]["lid"] = "open"
            dustbin_state[bin_name]["last_detected"] = time.time()
        else:
            log.warning("Open lid failed for %s — state unchanged", bin_name)
    log.debug("Open lid response: %s", resp)


def close_lid(bin_name: str):
    """Close the lid for the named dustbin and update shared state."""
    log.info("🔒 Closing lid → %s dustbin", bin_name)
    resp = _post(_CLOSE_EP[bin_name])
    with _lock:
        if resp is not None:
            lid = resp.get("lid", "closed")
            dustbin_state[bin_name]["lid"] = "closed" if lid in ("closed", "closing") else lid
        elif not ESP32_ENABLED:
            dustbin_state[bin_name]["lid"] = "closed"
        else:
            log.warning("Close lid failed for %s — state unchanged", bin_name)
    log.debug("Close lid response: %s", resp)


def _sync_lid_from_esp32(lid: str) -> str:
    """Map ESP32 lid string to dashboard open/closed."""
    if lid in ("open", "opening"):
        return "open"
    return "closed"


def set_lid_manual(bin_name: str, mode: str | None):
    """
    Manual lid override from dashboard.
    mode: 'open' | 'closed' | 'auto' (None) — held until user selects Auto again.
    """
    if bin_name not in ("paper", "plastic"):
        raise ValueError("invalid bin")
    if mode == "auto":
        mode = None
    if mode not in (None, "open", "closed"):
        raise ValueError("mode must be open, closed, or auto")

    with _lock:
        dustbin_state[bin_name]["manual"] = mode

    if mode == "open":
        log.info("Manual HOLD OPEN → %s", bin_name)
        threading.Thread(target=open_lid, args=(bin_name,), daemon=True).start()
    elif mode == "closed":
        log.info("Manual HOLD CLOSED → %s", bin_name)
        threading.Thread(target=close_lid, args=(bin_name,), daemon=True).start()
    else:
        log.info("Manual override OFF → %s (auto detection)", bin_name)


def on_detection(class_id: int, confidence: float | None = None):
    """
    Called when YOLO detects class_id at or above the lid-open threshold.
    Opens the matching dustbin lid (idempotent – won't re-send if already open).
    Updates the last_detected timestamp so the auto-close timer resets.
    """
    bin_name = CLASS_TO_BIN.get(class_id)
    if bin_name is None:
        return
    with _lock:
        manual = dustbin_state[bin_name].get("manual")
        if manual == "closed":
            return
        if manual == "open":
            dustbin_state[bin_name]["last_detected"] = time.time()
            return
        dustbin_state[bin_name]["last_detected"] = time.time()

    # Always POST open — ESP32 resets its auto-close timer when lid is already open.
    threading.Thread(target=open_lid, args=(bin_name,), daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
class LidAutoCloseThread(threading.Thread):
    """
    Monitors last_detected timestamps and closes lids after LID_OPEN_DURATION
    seconds of inactivity.
    """
    def __init__(self):
        super().__init__(daemon=True, name="LidAutoClose")

    def run(self):
        if not ESP32_ENABLED:
            return
        log.info("LidAutoCloseThread started (timeout=%.1fs)", LID_OPEN_DURATION)
        while True:
            time.sleep(0.5)
            now = time.time()
            for bin_name in ("paper", "plastic"):
                with _lock:
                    manual = dustbin_state[bin_name].get("manual")
                    if manual:
                        continue
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
        if not ESP32_ENABLED:
            log.info("FillLevelPoller skipped (ESP32_ENABLED=False)")
            return
        log.info("FillLevelPoller started (interval=%.1fs)", LEVEL_POLL_INTERVAL)
        while True:
            for bin_name, url in _STATUS_EP.items():
                data = _get(url)
                if data:
                    with _lock:
                        st = dustbin_state[bin_name]
                        st["level_pct"] = int(data.get("level", st["level_pct"]))
                        st["open_deg"] = int(data.get("open_deg", st["open_deg"]))
                        st["close_deg"] = int(data.get("close_deg", st["close_deg"]))
                        if "lid" in data:
                            st["lid"] = _sync_lid_from_esp32(str(data["lid"]))
                        pct = st["level_pct"]
                        open_deg = st["open_deg"]
                        close_deg = st["close_deg"]
                    log.debug("Fill level/config [%s] = %d%% (open: %d°, close: %d°)", bin_name, pct, open_deg, close_deg)
            time.sleep(LEVEL_POLL_INTERVAL)
