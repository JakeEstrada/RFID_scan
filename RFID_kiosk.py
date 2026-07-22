#!/usr/bin/env python3

import argparse
import base64
import os
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template_string, request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, ".env")


def load_env_file(path=ENV_PATH):
    """Load KEY=VALUE pairs from .env (works with sudo python3, no pip package needed)."""
    if not os.path.isfile(path):
        return

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


try:
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH)
except ImportError:
    load_env_file()

# -------------------------
# Logo
# -------------------------
LOGO_PATH = os.path.join(SCRIPT_DIR, "logo.png")

try:
    with open(LOGO_PATH, "rb") as f:
        LOGO_B64 = base64.b64encode(f.read()).decode("ascii")
except FileNotFoundError:
    print(f"WARNING: logo.png not found at {LOGO_PATH} — kiosk will run without a logo.", flush=True)
    LOGO_B64 = ""

# -------------------------
# Paarth API Config
# -------------------------
PAARTH_API_URL = os.environ.get("PAARTH_API_URL", "https://paarth-api.onrender.com").rstrip("/")
TENANT_ID = (os.environ.get("TENANT_ID") or os.environ.get("PAARTH_TENANT_ID") or "").strip()
API_KEY = (os.environ.get("API_KEY") or os.environ.get("RFID_DEVICE_API_KEY") or "").strip()
DEVICE_LABEL = (
    os.environ.get("DEVICE_LABEL") or os.environ.get("RFID_DEVICE_LABEL") or "shop-kiosk"
).strip()

# -------------------------
# RFID hardware (Raspberry Pi only)
# -------------------------
HAS_RFID = False
reader = None
GPIO = None


def init_rfid_reader():
    global HAS_RFID, reader, GPIO

    try:
        from mfrc522 import MFRC522
        import RPi.GPIO as GPIO_module

        GPIO = GPIO_module
        reader = MFRC522()
        HAS_RFID = True
        return True
    except ImportError as exc:
        print(
            "RFID reader disabled — missing Pi libraries (mfrc522 / RPi.GPIO).",
            flush=True,
        )
        print(f"  ({exc})", flush=True)
        print("Kiosk UI and PIN entry still work. RFID needs a Raspberry Pi.", flush=True)
        return False


# -------------------------
# Flask Setup
# -------------------------
app = Flask(__name__)


@app.after_request
def disable_cache(response):
    if response.content_type and "text/html" in response.content_type:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


latest_scan = {
    "scan_id": 0,
    "uid": None,
    "pin": None,
    "name": None,
    "time": None,
    "date": None,
    "status": "Waiting for scan...",
    "method": None,
}

last_uid = None
last_scan_time = 0
scan_counter = 0
DEBOUNCE_SECONDS = 2


def paarth_headers():
    return {
        "Content-Type": "application/json",
        "x-rfid-api-key": API_KEY,
        "x-tenant-id": TENANT_ID,
    }


def normalize_pin(raw):
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return digits if len(digits) == 4 else ""


def submit_to_paarth(payload):
    if not TENANT_ID or not API_KEY:
        return "Config Error", "Set TENANT_ID and API_KEY in .env", None

    try:
        response = requests.post(
            f"{PAARTH_API_URL}/rfid/scans",
            json=payload,
            headers=paarth_headers(),
            timeout=15,
        )

        if response.status_code == 201:
            data = response.json()
            scan = data.get("scan", {})
            name = scan.get("displayName", "Unknown")
            week_hours = scan.get("weekTotalHours")
            if week_hours is not None:
                print(f"Logged in Paarth: {name} ({week_hours} hrs this week)", flush=True)
            else:
                print(f"Logged in Paarth: {name}", flush=True)
            return name, None, week_hours

        print("Paarth error:", response.status_code, response.text, flush=True)
        return "Server Error", response.text, None

    except Exception as e:
        print("Network error:", e, flush=True)
        return "Network Error", str(e), None


def record_scan_result(name, uid=None, pin=None, method="rfid", week_hours=None):
    global latest_scan, scan_counter

    local_now = datetime.now()
    scan_counter += 1

    latest_scan = {
        "scan_id": scan_counter,
        "uid": uid,
        "pin": pin,
        "name": name,
        "time": local_now.strftime("%I:%M:%S %p"),
        "date": local_now.strftime("%A, %B %d, %Y"),
        "status": "Scan logged",
        "method": method,
        "weekHours": week_hours,
    }


def scan_loop():
    global latest_scan, last_uid, last_scan_time

    if not HAS_RFID or reader is None:
        return

    while True:
        status, tag_type = reader.MFRC522_Request(reader.PICC_REQIDL)

        if status == reader.MI_OK:
            status, uid = reader.MFRC522_Anticoll()

            if status == reader.MI_OK:
                uid_string = "-".join(str(x) for x in uid)
                now = time.time()

                if uid_string != last_uid or now - last_scan_time >= DEBOUNCE_SECONDS:
                    print(f"RFID UID: {uid_string}", flush=True)

                    payload = {
                        "uid": uid_string,
                        "scannedAt": datetime.now(timezone.utc).isoformat(),
                        "source": "raspberry-pi",
                        "deviceLabel": DEVICE_LABEL,
                    }

                    name, _err, week_hours = submit_to_paarth(payload)
                    record_scan_result(name, uid=uid_string, method="rfid", week_hours=week_hours)

                    last_uid = uid_string
                    last_scan_time = now

        else:
            last_uid = None

        time.sleep(0.1)


HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Paarth RFID Terminal</title>

<style>
  :root {
    --bg-start: #1a2226;
    --bg-end: #0d1214;
    --text-primary: #00a8ec;
    --text-muted: #0a5578;
    --text-bright: #f0fff8;
    --core-bg: #454c53;
    --ring-color: #00a8ec;
    --scanline-color: rgba(0, 168, 236, 0.025);
    --overlay-bg: rgba(2, 5, 4, 0.94);
    --card-bg: #020504;
    --card-border: #00a8ec;
    --card-shadow: rgba(0, 168, 236, 0.20);
    --glow-outer: rgba(0, 168, 236, 0.30);
    --glow-inner: rgba(0, 168, 236, 0.10);
    --glow-outer-strong: rgba(0, 168, 236, 0.65);
    --glow-inner-strong: rgba(0, 168, 236, 0.25);
    --uid-color: #042a3a;
    --toggle-bg: rgba(0, 168, 236, 0.12);
    --toggle-border: rgba(0, 168, 236, 0.35);
    --pin-bg: #0a1216;
    --pin-btn-bg: #101a20;
    --pin-btn-border: rgba(0, 168, 236, 0.35);
  }

  body.light {
    --bg-start: #f4f5f7;
    --bg-end: #e6e8eb;
    --text-primary: #2b6f8f;
    --text-muted: #7a8794;
    --text-bright: #1f2933;
    --core-bg: #d8dce0;
    --ring-color: #5a9fbf;
    --scanline-color: rgba(90, 159, 191, 0.04);
    --overlay-bg: rgba(244, 245, 247, 0.96);
    --card-bg: #ffffff;
    --card-border: #c5ccd3;
    --card-shadow: rgba(31, 41, 51, 0.08);
    --glow-outer: rgba(90, 159, 191, 0.18);
    --glow-inner: rgba(90, 159, 191, 0.08);
    --glow-outer-strong: rgba(90, 159, 191, 0.28);
    --glow-inner-strong: rgba(90, 159, 191, 0.12);
    --uid-color: #9aa5b1;
    --toggle-bg: rgba(90, 159, 191, 0.12);
    --toggle-border: rgba(90, 159, 191, 0.35);
    --pin-bg: #ffffff;
    --pin-btn-bg: #eef1f4;
    --pin-btn-border: #c5ccd3;
  }

  * {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
  }

  body {
    background: radial-gradient(circle at center, var(--bg-start) 0%, var(--bg-end) 70%);
    color: var(--text-primary);
    font-family: "Courier New", monospace;
    min-height: 100vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 24px;
    padding: 24px 20px 32px;
    transition: background 0.3s ease, color 0.3s ease;
  }

  .scanlines {
    pointer-events: none;
    position: fixed;
    inset: 0;
    background: repeating-linear-gradient(
      0deg,
      transparent,
      transparent 2px,
      var(--scanline-color) 2px,
      var(--scanline-color) 4px
    );
    z-index: 100;
  }

  .theme-toggle {
    position: fixed;
    top: 24px;
    right: 28px;
    z-index: 120;
    border: 1px solid var(--toggle-border);
    background: var(--toggle-bg);
    color: var(--text-primary);
    font-family: inherit;
    font-size: 11px;
    letter-spacing: 1px;
    padding: 8px 14px;
    cursor: pointer;
  }

  .clock-block {
    text-align: center;
    z-index: 10;
    width: 100%;
    flex-shrink: 0;
  }

  #clock-val {
    display: block;
    color: var(--text-primary);
    font-size: 64px;
    font-weight: bold;
    letter-spacing: 3px;
    margin-bottom: 8px;
    line-height: 1;
  }

  #clock-date {
    display: block;
    color: var(--text-muted);
    font-size: 32px;
    letter-spacing: 1px;
    line-height: 1.15;
    font-weight: 600;
  }

  .rfid-stage {
    position: relative;
    width: min(620px, 88vw);
    height: min(620px, 55vh);
    flex-shrink: 1;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .pulse-ring {
    position: absolute;
    width: 340px;
    height: 340px;
    border: 3px solid var(--ring-color);
    border-radius: 50%;
    opacity: 0;
    animation: pulse 5s infinite;
  }

  .pulse-ring:nth-child(1) { animation-delay: 0s; }
  .pulse-ring:nth-child(2) { animation-delay: 1.2s; }
  .pulse-ring:nth-child(3) { animation-delay: 2.4s; }
  .pulse-ring:nth-child(4) { animation-delay: 3.6s; }

  @keyframes pulse {
    0% {
      transform: scale(2.5);
      opacity: 0;
    }
    30% {
      opacity: 0.15;
    }
    100% {
      transform: scale(0.8);
      opacity: 0.8;
    }
  }

  .rfid-core {
    position: relative;
    width: 400px;
    height: 400px;
    border-radius: 50%;
    background: var(--core-bg);
    display: flex;
    align-items: center;
    justify-content: center;
    animation: coreGlow 3.2s infinite ease-in-out;
    overflow: hidden;
    cursor: pointer;
    border: none;
    padding: 0;
  }

  .rfid-core img {
    width: 92%;
    height: 92%;
    object-fit: contain;
    pointer-events: none;
  }

  @keyframes coreGlow {
    0%, 100% {
      box-shadow:
        0 0 30px var(--glow-outer),
        inset 0 0 30px var(--glow-inner);
    }
    50% {
      box-shadow:
        0 0 90px var(--glow-outer-strong),
        inset 0 0 50px var(--glow-inner-strong);
    }
  }

  .prompt {
    text-align: center;
    flex-shrink: 0;
  }

  .prompt-sub {
    margin-top: 10px;
    font-size: 18px;
    letter-spacing: 2px;
    color: var(--text-muted);
  }

  .overlay {
    display: none;
    position: fixed;
    inset: 0;
    z-index: 50;
    background: var(--overlay-bg);
    align-items: center;
    justify-content: center;
  }

  .overlay.show {
    display: flex;
  }

  .scan-card {
    min-width: min(92vw, 820px);
    padding: 50px 60px 55px;
    border: 1px solid var(--card-border);
    background: var(--card-bg);
    text-align: center;
    box-shadow: 0 0 45px var(--card-shadow);
    position: relative;
    animation: cardIn 0.22s ease-out;
  }

  @keyframes cardIn {
    from {
      transform: scale(0.94);
      opacity: 0;
    }
    to {
      transform: scale(1);
      opacity: 1;
    }
  }

  .card-tag {
    font-size: 11px;
    letter-spacing: 4px;
    color: var(--text-primary);
    margin-bottom: 26px;
  }

  .card-name {
    font-size: 42px;
    color: var(--text-bright);
    font-weight: bold;
    margin-bottom: 25px;
  }

  .card-time {
    font-size: 68px;
    font-weight: bold;
    color: var(--text-primary);
    margin-bottom: 14px;
    line-height: 1;
  }

  .card-date {
    font-size: 34px;
    color: var(--text-muted);
    margin-bottom: 20px;
    line-height: 1.15;
    font-weight: 600;
  }

  .week-hours-block {
    display: none;
    margin: 0 0 22px;
    padding: 16px 22px;
    border: 1px solid var(--card-border);
    background: rgba(0, 168, 236, 0.08);
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }

  body.light .week-hours-block {
    background: rgba(90, 159, 191, 0.10);
  }

  .week-hours-block.show {
    display: flex;
  }

  .week-hours-label {
    font-size: 28px;
    color: var(--text-muted);
    font-weight: 600;
    letter-spacing: 1px;
  }

  .week-hours-value {
    font-size: 42px;
    color: var(--text-primary);
    font-weight: bold;
    line-height: 1;
  }

  .card-uid {
    font-size: 10px;
    color: var(--uid-color);
    letter-spacing: 2px;
  }

  .progress {
    position: absolute;
    left: 0;
    bottom: 0;
    height: 3px;
    width: 100%;
    background: var(--text-primary);
    transform-origin: left;
    animation: shrink 4s linear forwards;
  }

  @keyframes shrink {
    from { transform: scaleX(1); }
    to { transform: scaleX(0); }
  }

  .pin-panel {
    min-width: 360px;
    padding: 34px 36px 28px;
    border: 1px solid var(--card-border);
    background: var(--pin-bg);
    text-align: center;
    box-shadow: 0 0 45px var(--card-shadow);
    animation: cardIn 0.22s ease-out;
  }

  .pin-title {
    font-size: 11px;
    letter-spacing: 4px;
    color: var(--text-primary);
    margin-bottom: 18px;
  }

  .pin-display {
    font-size: 34px;
    letter-spacing: 12px;
    color: var(--text-bright);
    min-height: 42px;
    margin-bottom: 22px;
  }

  .pin-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
    margin-bottom: 14px;
  }

  .pin-btn {
    border: 1px solid var(--pin-btn-border);
    background: var(--pin-btn-bg);
    color: var(--text-primary);
    font-family: inherit;
    font-size: 22px;
    padding: 16px 0;
    cursor: pointer;
  }

  .pin-btn:active {
    transform: scale(0.98);
  }

  .pin-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }

  .pin-cancel {
    color: var(--text-muted);
  }

  .pin-error {
    min-height: 18px;
    margin-top: 12px;
    font-size: 11px;
    letter-spacing: 1px;
    color: #d9534f;
  }
</style>
</head>

<body>
<div class="scanlines"></div>

<button class="theme-toggle" id="theme-toggle" type="button">LIGHT MODE</button>

<div class="clock-block">
  <span id="clock-val">--:--:--</span>
  <span id="clock-date">—</span>
</div>

<div class="rfid-stage">
  <div class="pulse-ring"></div>
  <div class="pulse-ring"></div>
  <div class="pulse-ring"></div>
  <div class="pulse-ring"></div>

  <button class="rfid-core" id="logo-btn" type="button" aria-label="Enter PIN">
    <img src="data:image/png;base64,{{ logo_b64 }}" alt="San Clemente Woodworking">
  </button>
</div>

<div class="prompt">
  <div class="prompt-sub">WAITING FOR EMPLOYEE SCAN · TAP LOGO FOR PIN</div>
</div>

<div class="overlay" id="overlay">
  <div class="scan-card">
    <div class="card-tag" id="card-tag">SCAN LOGGED</div>
    <div class="card-name" id="card-name">—</div>
    <div class="week-hours-block" id="week-hours-block">
      <span class="week-hours-label">Week total</span>
      <span class="week-hours-value" id="card-week-hours">—</span>
    </div>
    <div class="card-time" id="card-time">—</div>
    <div class="card-date" id="card-date">—</div>
    <div class="card-uid" id="card-uid">UID: —</div>
    <div class="progress" id="progress"></div>
  </div>
</div>

<div class="overlay" id="pin-overlay">
  <div class="pin-panel">
    <div class="pin-title">ENTER 4-DIGIT PIN</div>
    <div class="pin-display" id="pin-display"></div>
    <div class="pin-grid">
      <button class="pin-btn" data-digit="1" type="button">1</button>
      <button class="pin-btn" data-digit="2" type="button">2</button>
      <button class="pin-btn" data-digit="3" type="button">3</button>
      <button class="pin-btn" data-digit="4" type="button">4</button>
      <button class="pin-btn" data-digit="5" type="button">5</button>
      <button class="pin-btn" data-digit="6" type="button">6</button>
      <button class="pin-btn" data-digit="7" type="button">7</button>
      <button class="pin-btn" data-digit="8" type="button">8</button>
      <button class="pin-btn" data-digit="9" type="button">9</button>
      <button class="pin-btn pin-cancel" id="pin-clear" type="button">CLR</button>
      <button class="pin-btn" data-digit="0" type="button">0</button>
      <button class="pin-btn" id="pin-back" type="button">⌫</button>
    </div>
    <div class="pin-actions">
      <button class="pin-btn pin-cancel" id="pin-close" type="button">CANCEL</button>
      <button class="pin-btn" id="pin-submit" type="button">SUBMIT</button>
    </div>
    <div class="pin-error" id="pin-error"></div>
  </div>
</div>

<script>
  function updateClock() {
    const now = new Date();
    document.getElementById("clock-val").textContent =
      now.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit"
      });
    document.getElementById("clock-date").textContent =
      now.toLocaleDateString([], {
        weekday: "long",
        month: "long",
        day: "numeric",
        year: "numeric"
      });
  }

  setInterval(updateClock, 1000);
  updateClock();

  const themeToggle = document.getElementById("theme-toggle");
  const savedTheme = localStorage.getItem("kiosk-theme") || "dark";

  function applyTheme(theme) {
    const isLight = theme === "light";
    document.body.classList.toggle("light", isLight);
    themeToggle.textContent = isLight ? "DARK MODE" : "LIGHT MODE";
    localStorage.setItem("kiosk-theme", theme);
  }

  applyTheme(savedTheme);
  themeToggle.addEventListener("click", () => {
    applyTheme(document.body.classList.contains("light") ? "dark" : "light");
  });

  let lastScanId = null;
  let popupTimer = null;
  let pinValue = "";
  let pinSubmitting = false;

  const pinOverlay = document.getElementById("pin-overlay");
  const pinDisplay = document.getElementById("pin-display");
  const pinError = document.getElementById("pin-error");

  function renderPin() {
    pinDisplay.textContent = "•".repeat(pinValue.length);
  }

  function openPinPad() {
    pinValue = "";
    pinError.textContent = "";
    renderPin();
    pinOverlay.classList.add("show");
  }

  function closePinPad() {
    pinOverlay.classList.remove("show");
    pinValue = "";
    pinError.textContent = "";
    renderPin();
  }

  document.getElementById("logo-btn").addEventListener("click", openPinPad);
  document.getElementById("pin-close").addEventListener("click", closePinPad);
  document.getElementById("pin-clear").addEventListener("click", () => {
    pinValue = "";
    pinError.textContent = "";
    renderPin();
  });
  document.getElementById("pin-back").addEventListener("click", () => {
    pinValue = pinValue.slice(0, -1);
    pinError.textContent = "";
    renderPin();
  });

  document.querySelectorAll(".pin-btn[data-digit]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (pinSubmitting || pinValue.length >= 4) return;
      pinValue += btn.dataset.digit;
      pinError.textContent = "";
      renderPin();
      if (pinValue.length === 4) {
        void submitPin();
      }
    });
  });

  document.getElementById("pin-submit").addEventListener("click", () => {
    void submitPin();
  });

  async function submitPin() {
    if (pinSubmitting) return;
    if (pinValue.length !== 4) {
      pinError.textContent = "ENTER 4 DIGITS";
      return;
    }

    pinSubmitting = true;
    pinError.textContent = "";

    try {
      const res = await fetch("/pin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin: pinValue })
      });
      const data = await res.json();

      if (!res.ok) {
        pinError.textContent = data.error || "PIN FAILED";
        pinSubmitting = false;
        return;
      }

      closePinPad();
    } catch (err) {
      pinError.textContent = "NETWORK ERROR";
    } finally {
      pinSubmitting = false;
    }
  }

  async function checkScan() {
    try {
      const res = await fetch("/latest");
      const data = await res.json();

      if ((data.uid || data.pin) && data.scan_id !== lastScanId) {
        lastScanId = data.scan_id;

        document.getElementById("card-name").textContent = data.name;
        document.getElementById("card-time").textContent = data.time;
        document.getElementById("card-date").textContent = data.date;

        const weekBlock = document.getElementById("week-hours-block");
        if (data.weekHours != null && data.weekHours !== "") {
          document.getElementById("card-week-hours").textContent =
            Number(data.weekHours).toFixed(2) + " hrs";
          weekBlock.classList.add("show");
        } else {
          weekBlock.classList.remove("show");
        }

        if (data.method === "pin") {
          document.getElementById("card-tag").textContent = "PIN LOGGED";
          const isUnknown =
            data.name &&
            (data.name.startsWith("Unknown PIN") || data.name.startsWith("Unknown tag"));
          document.getElementById("card-uid").textContent = isUnknown
            ? "PIN: " + data.pin + " (not mapped)"
            : "PIN: ••••";
        } else {
          document.getElementById("card-tag").textContent = "SCAN LOGGED";
          document.getElementById("card-uid").textContent = "UID: " + data.uid;
        }

        const progress = document.getElementById("progress");
        progress.style.animation = "none";
        progress.offsetHeight;
        progress.style.animation = "shrink 4s linear forwards";

        document.getElementById("overlay").classList.add("show");

        clearTimeout(popupTimer);
        popupTimer = setTimeout(() => {
          document.getElementById("overlay").classList.remove("show");
        }, 4000);
      }
    } catch (err) {
      console.log("Scan check failed:", err);
    }
  }

  setInterval(checkScan, 400);
</script>

</body>
</html>
"""


@app.route("/")
def home():
    return render_template_string(HTML, logo_b64=LOGO_B64)


@app.route("/latest")
def latest():
    return jsonify(latest_scan)


@app.route("/pin", methods=["POST"])
def submit_pin():
    body = request.get_json(silent=True) or {}
    pin = normalize_pin(body.get("pin"))

    if not pin:
        return jsonify({"error": "PIN must be exactly 4 digits"}), 400

    payload = {
        "pin": pin,
        "uid": f"PIN-{pin}",
        "scannedAt": datetime.now(timezone.utc).isoformat(),
        "source": "kiosk-pin",
        "deviceLabel": DEVICE_LABEL,
    }

    name, err, week_hours = submit_to_paarth(payload)
    if err or name in ("Config Error", "Network Error", "Server Error"):
        name = f"Unknown PIN ({pin})"
        week_hours = None
        if err:
            print(f"Paarth PIN log failed, recorded locally: {err}", flush=True)

    record_scan_result(name, pin=pin, uid=f"PIN-{pin}", method="pin", week_hours=week_hours)
    return jsonify({"success": True, "name": name})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Paarth shop RFID kiosk")
    parser.add_argument(
        "--kiosk-only",
        action="store_true",
        help="Run the web UI and PIN entry only (no RFID reader). Use on a laptop for testing.",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5050,
        help="Port to bind (default: 5050)",
    )
    args = parser.parse_args()

    if not args.kiosk_only:
        init_rfid_reader()
    else:
        print("Kiosk-only mode — RFID reader disabled.", flush=True)

    try:
        if HAS_RFID:
            scanner_thread = threading.Thread(target=scan_loop, daemon=True)
            scanner_thread.start()
            print("RFID reader active.", flush=True)

        print(f"RFID kiosk running at http://localhost:{args.port}", flush=True)
        if not HAS_RFID:
            print("Open that URL in a browser. Tap the logo to test PIN entry.", flush=True)

        app.run(host=args.host, port=args.port, debug=False)

    except KeyboardInterrupt:
        print("\nStopping RFID kiosk...", flush=True)

    finally:
        if HAS_RFID and GPIO is not None:
            GPIO.cleanup()
