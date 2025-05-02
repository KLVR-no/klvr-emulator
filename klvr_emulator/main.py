# Directory structure
#
# klvr-emulator/
# ├── klvr_emulator/
# │   ├── __init__.py
# │   └── main.py
# ├── templates/
# │   └── index.html
# ├── static/
# │   └── style.css (optional)
# ├── run.py
# ├── start.sh
# ├── requirements.txt
# └── README.md (already in canvas)

# klvr_emulator/main.py

import socket
import threading
import time
import atexit
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from zeroconf import ServiceInfo, Zeroconf
import uvicorn

# Get local IP

def get_host_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except:
        return "127.0.0.1"
    finally:
        s.close()

host_ip = get_host_ip()

app = FastAPI()

charger_state = {
    "name": "Laddare #1",
    "deviceInternalTemperatureC": 24.01,
    "firmwareVersion": "0.1.0",
    "firmwareBuild": "abc123",
    "network": {
        "ipAddress": host_ip,
        "gatewayAddress": "10.0.0.1",
        "mask": "255.255.255.0",
        "macAddress": "00:B0:D0:63:C2:26",
        "method": "dhcp"
    },
    "batteries": [
        {
            "slot": i,
            "batteryBayTempC": 24.0,
            "slotState": "empty",
            "stateOfChargePercent": 0.0,
            "timeRemainingSeconds": 0,
            "errorMsg": "",
            "batteryDetected": ""
        } for i in range(48)
    ]
}

@app.middleware("http")
async def add_cors(request: Request, call_next):
    response = await call_next(request)
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

@app.get("/api/v2/charger/status")
def get_status():
    return {
        "deviceStatus": "ok",
        "batteries": charger_state["batteries"]
    }

@app.get("/api/v2/device/info")
def get_device_info():
    return {
        "deviceInternalTemperatureC": charger_state["deviceInternalTemperatureC"],
        "name": charger_state["name"],
        "firmwareVersion": charger_state["firmwareVersion"],
        "firmwareBuild": charger_state["firmwareBuild"],
        "ip": charger_state["network"]
    }

@app.post("/api/v2/charger/insert/{slot}")
def insert(slot: int, type: str = Query(...)):
    b = charger_state["batteries"][slot]
    b["slotState"] = "charging"
    b["stateOfChargePercent"] = 5.0
    b["timeRemainingSeconds"] = 600
    b["batteryDetected"] = type
    return {"ok": True}

@app.post("/api/v2/charger/eject/{slot}")
def eject(slot: int):
    b = charger_state["batteries"][slot]
    b["slotState"] = "empty"
    b["stateOfChargePercent"] = 0.0
    b["timeRemainingSeconds"] = 0
    b["batteryDetected"] = ""
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
def ui():
    return Path("templates/index.html").read_text()

app.mount("/static", StaticFiles(directory="static"), name="static")

# Charging simulation

def loop():
    while True:
        for b in charger_state["batteries"]:
            if b["slotState"] == "charging":
                b["stateOfChargePercent"] = min(100.0, b["stateOfChargePercent"] + 0.2)
                b["timeRemainingSeconds"] = max(0, b["timeRemainingSeconds"] - 2)
                if b["stateOfChargePercent"] >= 100.0:
                    b["slotState"] = "done"
        time.sleep(2)

def bonjour():
    z = Zeroconf()
    info = ServiceInfo(
        type_="_klvrcharger._tcp.local.",
        name="KLVR Charger Pro._klvrcharger._tcp.local.",
        addresses=[socket.inet_aton(host_ip)],
        port=8000,
        properties={"version": "0.1.0", "model": "emulator"},
        server="klvr-emulator.local."
    )
    z.register_service(info)
    atexit.register(lambda: z.unregister_service(info))

    print(f"Emulator running at http://{host_ip}:8000")


threading.Thread(target=loop, daemon=True).start()
threading.Thread(target=bonjour, daemon=True).start()
