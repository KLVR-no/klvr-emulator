import socket
import threading
import time
import atexit
import asyncio
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from zeroconf import ServiceInfo, Zeroconf

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
runtime_port = 8000  # Will be overwritten by run.py

app = FastAPI()

charger_state = {
    "name": "KLVR Emulator",
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

# Firmware update state
firmware_state = {
    "main_firmware_pending": False,
    "rear_firmware_pending": False,
    "main_firmware_size": 0,
    "rear_firmware_size": 0,
    "is_rebooting": False
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
    if firmware_state["is_rebooting"]:
        raise HTTPException(status_code=503, detail="Device rebooting")
    
    return {
        "name": charger_state["name"],
        "firmwareVersion": charger_state["firmwareVersion"],
        "ip": {
            "ipAddress": charger_state["network"]["ipAddress"],
            "networkMask": charger_state["network"]["mask"],
            "gateway": charger_state["network"]["gatewayAddress"],
            "method": charger_state["network"]["method"]
        },
        "status": "ready"
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

# Firmware Update Endpoints

@app.post("/api/v2/device/firmware_charger")
async def upload_main_firmware(request: Request):
    """Upload main board firmware"""
    try:
        firmware_data = await request.body()
        firmware_size = len(firmware_data)
        
        print(f"✅ Received main firmware: {firmware_size:,} bytes")
        firmware_state["main_firmware_size"] = firmware_size
        firmware_state["main_firmware_pending"] = True
        
        # Simulate processing time
        await asyncio.sleep(2)
        
        return {"status": "success"}
    except Exception as e:
        print(f"❌ Error uploading main firmware: {e}")
        raise HTTPException(status_code=500, detail="Firmware upload failed")

@app.post("/api/v2/device/firmware_rear")
async def upload_rear_firmware(request: Request):
    """Upload rear board firmware"""
    try:
        firmware_data = await request.body()
        firmware_size = len(firmware_data)
        
        print(f"✅ Received rear firmware: {firmware_size:,} bytes")
        firmware_state["rear_firmware_size"] = firmware_size
        firmware_state["rear_firmware_pending"] = True
        
        # Simulate processing time
        await asyncio.sleep(2)
        
        return {"status": "success"}
    except Exception as e:
        print(f"❌ Error uploading rear firmware: {e}")
        raise HTTPException(status_code=500, detail="Firmware upload failed")

@app.post("/api/v2/device/reboot")
async def reboot_board(request: Request):
    """Reboot main or rear board"""
    try:
        board = (await request.body()).decode().strip()
        
        if board not in ["main", "rear"]:
            raise HTTPException(status_code=400, detail="Invalid board name. Use 'main' or 'rear'")
        
        print(f"🔄 Rebooting {board} board...")
        
        # Set rebooting state
        firmware_state["is_rebooting"] = True
        
        # Simulate reboot delay in background
        async def reboot_process():
            await asyncio.sleep(3)  # 3 second reboot delay
            
            if board == "main" and firmware_state["main_firmware_pending"]:
                firmware_state["main_firmware_pending"] = False
                print(f"✅ Main firmware applied! ({firmware_state['main_firmware_size']:,} bytes)")
                
            elif board == "rear" and firmware_state["rear_firmware_pending"]:
                firmware_state["rear_firmware_pending"] = False
                print(f"✅ Rear firmware applied! ({firmware_state['rear_firmware_size']:,} bytes)")
                
                # Check if both boards are updated
                if not firmware_state["main_firmware_pending"]:
                    charger_state["firmwareVersion"] = "0.1.5"
                    print("🎉 Firmware update complete! Version: 0.1.5")
            
            firmware_state["is_rebooting"] = False
            print(f"✅ {board.capitalize()} board reboot complete")
        
        # Start reboot process in background
        asyncio.create_task(reboot_process())
        
        return {"status": "rebooting"}
        
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Invalid request body format")
    except Exception as e:
        firmware_state["is_rebooting"] = False
        print(f"❌ Error during reboot: {e}")
        raise HTTPException(status_code=500, detail="Reboot failed")

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
        name="KLVR " + socket.gethostname() + "._klvrcharger._tcp.local.",
        addresses=[socket.inet_aton(host_ip)],
        port=runtime_port,
        properties={"version": "0.1.0", "model": "emulator"},
        server="klvr-emulator.local."
    )
    z.register_service(info)
    atexit.register(lambda: z.unregister_service(info))
    print(f"✅ Emulator running at http://{host_ip}:{runtime_port}")

# Start background threads
threading.Thread(target=loop, daemon=True).start()
threading.Thread(target=bonjour, daemon=True).start()
