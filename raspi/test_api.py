# ─────────────────────────────────────────────────────────────────────────────
# test_api.py  –  Test connectivity to both dustbin APIs before running detect.py
# ─────────────────────────────────────────────────────────────────────────────
#
#  Run:  python test_api.py
#
#  Will test:
#   - GET /level  on both bins
#   - POST /open  on both bins
#   - POST /close on both bins
# ─────────────────────────────────────────────────────────────────────────────

import requests
import time
from config import (
    PAPER_LID_OPEN_EP, PAPER_LID_CLOSE_EP, PAPER_LEVEL_EP,
    PLASTIC_LID_OPEN_EP, PLASTIC_LID_CLOSE_EP, PLASTIC_LEVEL_EP,
    API_TIMEOUT, ESP32_HOST,
)

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


def ok(msg):  print(f"  {GREEN}✓ {msg}{RESET}")
def fail(msg): print(f"  {RED}✗ {msg}{RESET}")
def info(msg): print(f"  {CYAN}ℹ {msg}{RESET}")


def test_get(label, url):
    print(f"\n  [{label}] GET {url}")
    try:
        r = requests.get(url, timeout=API_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        ok(f"Response: {data}")
        return True
    except Exception as e:
        fail(str(e))
        return False


def test_post(label, url, payload=None):
    print(f"\n  [{label}] POST {url}")
    try:
        r = requests.post(url, json=payload or {}, timeout=API_TIMEOUT)
        r.raise_for_status()
        data = r.json() if r.content else {}
        ok(f"Response: {data}")
        return True
    except Exception as e:
        fail(str(e))
        return False


# ─────────────────────────────────────────────────────────────────────────────
def run_tests():
    print()
    print(f"{BOLD}{'='*55}{RESET}")
    print(f"{BOLD}  Trash Detection – Dustbin API Connectivity Test{RESET}")
    print(f"{BOLD}{'='*55}{RESET}")

    results = {}

    # ── Paper dustbin ─────────────────────────────────────────────────────────
    print(f"\n{BOLD}{CYAN}📄 Paper Dustbin{RESET}")
    results["paper_level"] = test_get("fill level", PAPER_LEVEL_EP)
    results["paper_open"]  = test_post("open lid",  PAPER_LID_OPEN_EP)
    if results["paper_open"]:
        info("Waiting 2 s before closing…")
        time.sleep(2)
    results["paper_close"] = test_post("close lid", PAPER_LID_CLOSE_EP)

    # ── Plastic dustbin ───────────────────────────────────────────────────────
    print(f"\n{BOLD}{CYAN}🧴 Plastic Dustbin{RESET}")
    results["plastic_level"] = test_get("fill level", PLASTIC_LEVEL_EP)
    results["plastic_open"]  = test_post("open lid",  PLASTIC_LID_OPEN_EP)
    if results["plastic_open"]:
        info("Waiting 2 s before closing…")
        time.sleep(2)
    results["plastic_close"] = test_post("close lid", PLASTIC_LID_CLOSE_EP)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print(f"{BOLD}{'='*55}{RESET}")
    print(f"{BOLD}  Results Summary{RESET}")
    print(f"{BOLD}{'='*55}{RESET}")

    passed = sum(results.values())
    total  = len(results)

    for name, success in results.items():
        status = f"{GREEN}PASS{RESET}" if success else f"{RED}FAIL{RESET}"
        print(f"  {name:25s} {status}")

    print()
    colour = GREEN if passed == total else (YELLOW if passed > 0 else RED)
    print(f"  {colour}{BOLD}{passed}/{total} tests passed{RESET}")

    if passed < total:
        print(f"\n  {YELLOW}Tips:{RESET}")
        print(f"    • Check config.py → ESP32_HOST")
        print(f"    • Ensure dustbin controllers are powered & on the same network")
        print(f"    • Ping the host/IP:  ping {ESP32_HOST}")
    print()


if __name__ == "__main__":
    run_tests()
