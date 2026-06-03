import socket
import threading
import time
import atexit
import asyncio
import json
from fastapi import FastAPI, Request, Query, HTTPException, WebSocket, WebSocketDisconnect
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
    "deviceStatus": "ok",
    "network": {
        "ipAddress": host_ip,
        "gatewayAddress": "10.0.0.1",
        "mask": "255.255.255.0",
        "macAddress": "00:B0:D0:63:C2:26",
        "method": "dhcp"
    },
    "batteries": [
        {
            "index": i,
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
    "main_firmware_version": None,
    "rear_firmware_version": None,
    "target_version": None,
    "main_rebooting": False,
    "rear_rebooting": False
}

# WebSocket state — firmware supports WS_MAX_CLIENTS=8; emulator caps at 2
ws_clients: list[WebSocket] = []
ws_lock = asyncio.Lock()
pending_push: bool = False  # debounce flag
_event_loop: asyncio.AbstractEventLoop | None = None  # captured at startup


async def ws_broadcast():
    """Send current status to all connected WS clients."""
    payload = {
        "event": "battery_status",
        "data": {
            "deviceStatus": charger_state.get("deviceStatus", "ok"),
            "batteries": charger_state["batteries"]
        }
    }
    msg = json.dumps(payload)
    async with ws_lock:
        dead = []
        for client in ws_clients:
            try:
                await client.send_text(msg)
            except Exception:
                dead.append(client)
        for d in dead:
            ws_clients.remove(d)


async def ws_heartbeat():
    """Push every 10 seconds regardless of state changes (WS_HEARTBEAT_MS=10000)."""
    while True:
        await asyncio.sleep(10)
        await ws_broadcast()


async def ws_debounce_push():
    """Push 250 ms after a state change is flagged (WS_DEBOUNCE_MS=250)."""
    global pending_push
    await asyncio.sleep(0.25)
    pending_push = False
    await ws_broadcast()


def notify_state_change():
    """Call whenever battery state changes. Triggers a debounced WS push.

    Safe to call from sync FastAPI endpoints (thread-pool executors): uses
    run_coroutine_threadsafe so we always schedule on the uvicorn event loop
    rather than calling asyncio.create_task(), which only works from inside
    a running coroutine.
    """
    global pending_push
    if not pending_push and _event_loop is not None:
        pending_push = True
        asyncio.run_coroutine_threadsafe(ws_debounce_push(), _event_loop)


@app.websocket("/api/v2/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    async with ws_lock:
        if len(ws_clients) >= 2:
            await websocket.close(code=1008)  # Policy violation — max clients
            return
        ws_clients.append(websocket)
    # Send immediately on connect
    await ws_broadcast()
    try:
        while True:
            await websocket.receive_text()  # Keep connection alive; ignore incoming
    except WebSocketDisconnect:
        async with ws_lock:
            if websocket in ws_clients:
                ws_clients.remove(websocket)


@app.on_event("startup")
async def startup():
    global _event_loop
    _event_loop = asyncio.get_running_loop()
    asyncio.create_task(ws_heartbeat())


@app.middleware("http")
async def add_cors(request: Request, call_next):
    response = await call_next(request)
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


@app.get("/api/v2/charger/status")
def get_status():
    return {
        "deviceStatus": charger_state.get("deviceStatus", "ok"),
        "batteries": charger_state["batteries"]
    }


@app.get("/api/v2/device/info")
def get_device_info():
    main_rebooting = firmware_state.get("main_rebooting", False)
    rear_rebooting = firmware_state.get("rear_rebooting", False)

    if main_rebooting and rear_rebooting:
        status = "rebooting_both"
    elif main_rebooting:
        status = "rebooting_main"
    elif rear_rebooting:
        status = "rebooting_rear"
    else:
        status = "ready"

    return {
        "name": f"KLVR Emulator Port {runtime_port}",
        "firmwareVersion": charger_state["firmwareVersion"],
        "ip": {
            "ipAddress": charger_state["network"]["ipAddress"],
            "networkMask": charger_state["network"]["mask"],
            "gateway": charger_state["network"]["gatewayAddress"],
            "method": charger_state["network"]["method"]
        },
        "status": status
    }


@app.get("/api/v2/device/firmware_version")
def get_firmware_version():
    return {
        "firmwareRear": charger_state["firmwareVersion"],
        "firmwareMain": charger_state["firmwareVersion"]
    }


@app.post("/api/v2/charger/insert/{slot}")
async def insert(slot: int, type: str = Query(...)):
    b = charger_state["batteries"][slot]
    # Recovery: cold/warm batteries transition back to charging on re-insert
    b["slotState"] = "charging"
    b["stateOfChargePercent"] = 5.0
    b["timeRemainingSeconds"] = 7200
    b["batteryDetected"] = type
    b["errorMsg"] = ""
    notify_state_change()
    return {"ok": True}


@app.post("/api/v2/charger/eject/{slot}")
async def eject(slot: int):
    b = charger_state["batteries"][slot]
    b["slotState"] = "empty"
    b["stateOfChargePercent"] = 0.0
    b["timeRemainingSeconds"] = 0
    b["batteryDetected"] = ""
    b["errorMsg"] = ""
    notify_state_change()
    return {"ok": True}


@app.post("/api/v2/charger/bulk_insert")
async def bulk_insert():
    """Insert 37 batteries with random types (AA/AAA) and SOC levels, always including 4-5 full batteries"""
    import random

    for b in charger_state["batteries"]:
        b["slotState"] = "empty"
        b["stateOfChargePercent"] = 0.0
        b["timeRemainingSeconds"] = 0
        b["batteryDetected"] = ""
        b["errorMsg"] = ""

    battery_types = ["KLVR-AA", "KLVR-AAA"]
    slots_to_fill = random.sample(range(48), 37)
    full_batteries_count = random.randint(4, 5)
    full_battery_slots = random.sample(slots_to_fill, full_batteries_count)

    batteries_inserted = []
    full_count = 0

    for slot in slots_to_fill:
        battery_type = random.choice(battery_types)

        if slot in full_battery_slots:
            soc = 100.0
            full_count += 1
            slot_state = "done"
            time_remaining = 0
        else:
            soc = random.uniform(5.0, 95.0)
            slot_state = "charging"
            remaining_charge_needed = max(0, 100.0 - soc)
            total_charge_range = 95.0
            total_time_for_full_charge = 7200
            time_remaining = int((remaining_charge_needed / total_charge_range) * total_time_for_full_charge)

        b = charger_state["batteries"][slot]
        b["slotState"] = slot_state
        b["stateOfChargePercent"] = soc
        b["timeRemainingSeconds"] = time_remaining
        b["batteryDetected"] = battery_type
        b["errorMsg"] = ""

        batteries_inserted.append({
            "index": slot,
            "type": battery_type,
            "soc": round(soc, 1),
            "timeRemaining": time_remaining
        })

    print(f"🔋 Bulk inserted 37 batteries: {len([b for b in batteries_inserted if b['type'] == 'KLVR-AA'])} AA, {len([b for b in batteries_inserted if b['type'] == 'KLVR-AAA'])} AAA ({full_count} full)")

    notify_state_change()
    return {
        "ok": True,
        "message": f"Successfully inserted 37 batteries ({full_count} full)",
        "batteries": batteries_inserted,
        "summary": {
            "total": len(batteries_inserted),
            "aa_count": len([b for b in batteries_inserted if b["type"] == "KLVR-AA"]),
            "aaa_count": len([b for b in batteries_inserted if b["type"] == "KLVR-AAA"]),
            "avg_soc": round(sum(b["soc"] for b in batteries_inserted) / len(batteries_inserted), 1),
            "full_batteries": full_count
        }
    }


@app.post("/api/v2/charger/set_cold/{slot}")
async def set_cold(slot: int):
    """Set a slot to cold state (BATTERY_STATE_TEMP_COLD)."""
    if slot < 0 or slot >= 48:
        raise HTTPException(status_code=400, detail="Invalid slot number")
    b = charger_state["batteries"][slot]
    b["slotState"] = "cold"
    b["errorMsg"] = "cold"
    notify_state_change()
    return {"ok": True}


@app.post("/api/v2/charger/set_warm/{slot}")
async def set_warm(slot: int):
    """Set a slot to warm state (BATTERY_STATE_TEMP_WARM)."""
    if slot < 0 or slot >= 48:
        raise HTTPException(status_code=400, detail="Invalid slot number")
    b = charger_state["batteries"][slot]
    b["slotState"] = "warm"
    b["errorMsg"] = "warm"
    notify_state_change()
    return {"ok": True}


@app.post("/api/v2/charger/bulk_clear")
async def bulk_clear():
    """Eject all batteries at once."""
    for b in charger_state["batteries"]:
        b["slotState"] = "empty"
        b["stateOfChargePercent"] = 0.0
        b["timeRemainingSeconds"] = 0
        b["batteryDetected"] = ""
        b["errorMsg"] = ""
    notify_state_change()
    return {"ok": True}


VALID_ERROR_TYPES = {"overtemp", "undertemp", "overcurrent", "faulty", "detect_err"}

@app.post("/api/v2/charger/set_error/{slot}")
async def set_error(slot: int, type: str = Query(...)):
    """Set a slot to error state with a specific errorMsg."""
    if slot < 0 or slot >= 48:
        raise HTTPException(status_code=400, detail="Invalid slot number")
    if type not in VALID_ERROR_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid error type. Must be one of: {', '.join(sorted(VALID_ERROR_TYPES))}")
    b = charger_state["batteries"][slot]
    b["slotState"] = "error"
    b["errorMsg"] = type
    notify_state_change()
    return {"ok": True}


# Firmware Update Endpoints

@app.post("/api/v2/device/firmware_charger")
async def upload_main_firmware(request: Request, version: str = Query(default=None)):
    """Upload main board firmware"""
    try:
        firmware_data = await request.body()
        firmware_size = len(firmware_data)

        print(f"🔍 MAIN: Query params: {dict(request.query_params)}")
        print(f"🔍 MAIN: All headers: {dict(request.headers)}")

        simulated_version = (
            version or
            request.headers.get("x-firmware-version") or
            request.headers.get("firmware-version") or
            "1.6.3"
        )

        print(f"✅ Received main firmware: {firmware_size:,} bytes (version: {simulated_version})")
        firmware_state["main_firmware_size"] = firmware_size
        firmware_state["main_firmware_pending"] = True
        firmware_state["main_firmware_version"] = simulated_version

        if not firmware_state["target_version"]:
            firmware_state["target_version"] = simulated_version

        print(f"📊 Updated Firmware State: main_pending={firmware_state['main_firmware_pending']}, rear_pending={firmware_state['rear_firmware_pending']}, target_version={firmware_state['target_version']}")

        await asyncio.sleep(2)

        return {"status": "success", "message": f"Main firmware uploaded successfully (version: {simulated_version})"}
    except Exception as e:
        print(f"❌ Error uploading main firmware: {e}")
        raise HTTPException(status_code=500, detail="Firmware upload failed")


@app.post("/api/v2/device/firmware_rear")
async def upload_rear_firmware(request: Request, version: str = Query(default=None)):
    """Upload rear board firmware"""
    try:
        firmware_data = await request.body()
        firmware_size = len(firmware_data)

        print(f"🔍 REAR: Query params: {dict(request.query_params)}")
        print(f"🔍 REAR: All headers: {dict(request.headers)}")

        simulated_version = (
            version or
            request.headers.get("x-firmware-version") or
            request.headers.get("firmware-version") or
            firmware_state.get("target_version") or
            "1.6.3"
        )

        print(f"✅ Received rear firmware: {firmware_size:,} bytes (version: {simulated_version})")
        firmware_state["rear_firmware_size"] = firmware_size
        firmware_state["rear_firmware_pending"] = True
        firmware_state["rear_firmware_version"] = simulated_version

        if not firmware_state["target_version"]:
            firmware_state["target_version"] = simulated_version

        print(f"📊 Updated Firmware State: main_pending={firmware_state['main_firmware_pending']}, rear_pending={firmware_state['rear_firmware_pending']}, target_version={firmware_state['target_version']}")

        await asyncio.sleep(2)

        return {"status": "success", "message": f"Rear firmware uploaded successfully (version: {simulated_version})"}
    except Exception as e:
        print(f"❌ Error uploading rear firmware: {e}")
        raise HTTPException(status_code=500, detail="Firmware upload failed")


@app.post("/api/v2/device/reboot")
async def reboot_board(request: Request):
    """Reboot main or rear board — emulated with realistic delays"""
    try:
        board = (await request.body()).decode().strip()

        if board not in ["main", "rear"]:
            raise HTTPException(status_code=400, detail="Invalid board name. Use 'main' or 'rear'")

        print(f"🔄 Emulating {board} board reboot...")

        reboot_key = f"{board}_rebooting"
        firmware_state[reboot_key] = True

        async def emulated_reboot():
            await asyncio.sleep(2)

            if board == "main" and firmware_state["main_firmware_pending"]:
                firmware_state["main_firmware_pending"] = False
                print(f"✅ Main firmware applied! ({firmware_state['main_firmware_size']:,} bytes)")
            elif board == "rear" and firmware_state["rear_firmware_pending"]:
                firmware_state["rear_firmware_pending"] = False
                print(f"✅ Rear firmware applied! ({firmware_state['rear_firmware_size']:,} bytes)")

            if not firmware_state["main_firmware_pending"] and not firmware_state["rear_firmware_pending"] and \
               firmware_state["main_firmware_size"] > 0 and firmware_state["rear_firmware_size"] > 0:
                target_version = firmware_state["target_version"] or "0.1.5"
                charger_state["firmwareVersion"] = target_version
                print(f"🎉 Firmware update complete! Version: {target_version}")

            firmware_state[reboot_key] = False
            print(f"✅ {board.capitalize()} board reboot complete")
            print(f"📊 Firmware State: main_pending={firmware_state['main_firmware_pending']}, rear_pending={firmware_state['rear_firmware_pending']}, version={charger_state['firmwareVersion']}")

        asyncio.create_task(emulated_reboot())
        return {"status": "rebooting"}

    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Invalid request body format")
    except Exception as e:
        print(f"❌ Error during reboot: {e}")
        raise HTTPException(status_code=500, detail="Reboot failed")


@app.get("/api/v2/device/firmware_state")
def get_firmware_state():
    """Debug endpoint to check current firmware state"""
    return {
        "current_version": charger_state["firmwareVersion"],
        "target_version": firmware_state["target_version"],
        "main_firmware_pending": firmware_state["main_firmware_pending"],
        "rear_firmware_pending": firmware_state["rear_firmware_pending"],
        "main_firmware_size": firmware_state["main_firmware_size"],
        "rear_firmware_size": firmware_state["rear_firmware_size"],
        "main_firmware_version": firmware_state["main_firmware_version"],
        "rear_firmware_version": firmware_state["rear_firmware_version"],
        "main_rebooting": firmware_state["main_rebooting"],
        "rear_rebooting": firmware_state["rear_rebooting"]
    }


@app.post("/api/v2/device/set_firmware_version")
async def set_firmware_version(request: Request):
    """Manually set firmware version for testing"""
    try:
        data = await request.json()
        version = data.get("version", "0.1.0")
        charger_state["firmwareVersion"] = version
        print(f"🔧 Manually set firmware version to: {version}")
        return {"status": "success", "firmwareVersion": version}
    except Exception as e:
        print(f"❌ Error setting firmware version: {e}")
        raise HTTPException(status_code=400, detail="Invalid request")


@app.post("/api/v2/device/set_target_version")
async def set_target_version(request: Request):
    """Manually set target firmware version for testing firmware uploads"""
    try:
        data = await request.json()
        version = data.get("version", "1.6.3")
        firmware_state["target_version"] = version
        print(f"🎯 Manually set target firmware version to: {version}")
        return {"status": "success", "targetVersion": version}
    except Exception as e:
        print(f"❌ Error setting target version: {e}")
        raise HTTPException(status_code=400, detail="Invalid request")


@app.post("/api/v2/charger/set_charge/{slot}")
async def set_charge_percentage(slot: int, request: Request):
    """Manually set charge percentage for a specific slot"""
    try:
        data = await request.json()
        percentage = float(data.get("percentage", 0.0))

        if slot < 0 or slot >= 48:
            raise HTTPException(status_code=400, detail="Invalid slot number")
        if percentage < 0 or percentage > 100:
            raise HTTPException(status_code=400, detail="Percentage must be between 0 and 100")

        battery = charger_state["batteries"][slot]
        battery["stateOfChargePercent"] = percentage

        if battery["slotState"] == "charging" and percentage < 100:
            remaining_charge_needed = max(0, 100.0 - percentage)
            total_charge_range = 95.0
            total_time_for_full_charge = 7200
            battery["timeRemainingSeconds"] = int((remaining_charge_needed / total_charge_range) * total_time_for_full_charge)
        elif percentage >= 100:
            battery["slotState"] = "done"
            battery["timeRemainingSeconds"] = 0
        elif battery["slotState"] == "empty":
            battery["timeRemainingSeconds"] = 0

        print(f"🔋 Set slot {slot} charge to: {percentage}% (remaining: {battery['timeRemainingSeconds']}s)")
        notify_state_change()
        return {"status": "success", "index": slot, "percentage": percentage, "timeRemaining": battery["timeRemainingSeconds"]}
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid percentage value")
    except Exception as e:
        print(f"❌ Error setting charge percentage: {e}")
        raise HTTPException(status_code=500, detail="Failed to set charge percentage")


@app.get("/", response_class=HTMLResponse)
def ui():
    return Path("templates/index.html").read_text()

app.mount("/static", StaticFiles(directory="static"), name="static")


# Charging simulation — runs in a background thread
def loop():
    while True:
        changed = False
        for b in charger_state["batteries"]:
            if b["slotState"] in ("cold", "warm"):
                # Temperature-paused states — do not increment charge
                continue
            if b["slotState"] == "charging":
                # 95% in 2 hours = 0.0264% every 2 seconds
                b["stateOfChargePercent"] = min(100.0, b["stateOfChargePercent"] + 0.0264)
                b["timeRemainingSeconds"] = max(0, b["timeRemainingSeconds"] - 2)
                if b["stateOfChargePercent"] >= 100.0:
                    b["slotState"] = "done"
                    b["timeRemainingSeconds"] = 0
                changed = True

        if changed and _event_loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws_broadcast(), _event_loop)
            except Exception:
                pass

        time.sleep(2)


def bonjour():
    z = Zeroconf()
    simulated_mac = f"0080e1{runtime_port:06x}"
    service_name = f"klvr{simulated_mac}"

    info = ServiceInfo(
        type_="_klvrcharger._tcp.local.",
        name=f"{service_name}._klvrcharger._tcp.local.",
        addresses=[socket.inet_aton(host_ip)],
        port=runtime_port,
        properties={"version": "0.1.0", "model": "emulator", "port": str(runtime_port)},
        server=f"klvr-emulator-{runtime_port}.local."
    )
    z.register_service(info)
    atexit.register(lambda: z.unregister_service(info))
    print(f"✅ Emulator running at http://{host_ip}:{runtime_port}")
    print(f"🔗 Bonjour service registered as: {service_name}")


# Start background threads
threading.Thread(target=loop, daemon=True).start()
threading.Thread(target=bonjour, daemon=True).start()
