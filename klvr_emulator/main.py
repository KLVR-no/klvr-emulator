import socket
import threading
import time
import atexit
import asyncio
import json
from fastapi import FastAPI, Request, Query, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from zeroconf import ServiceInfo, Zeroconf

from klvr_emulator import snapshot as twin_snapshot

# Firmware: system_settings.name is char[20]; default brand string is Klvr.
DEFAULT_DEVICE_NAME = "Klvr"
DEVICE_NAME_MAX_LEN = 19
# network.c klvr_dns_txt — RFC 6763 key=value pairs, nothing else.
MDNS_TXT = {"ws": "1", "api": "v2"}
SPEC_BATTERY_TYPES = {"KLVR-AA", "KLVR-AAA"}
CORS_PREFLIGHT_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Content-Length",
}
# Firmware registers OPTIONS per HTTP resource (http_server.c). /ws has none.
FIRMWARE_OPTIONS_PATHS = frozenset({
    "/api/v2/charger/status",
    "/api/v2/device/info",
    "/api/v2/device/identify",
    "/api/v2/device/network",
    "/api/v2/device/name",
    "/api/v2/device/firmware_version",
    "/api/v2/device/firmware_charger",
    "/api/v2/device/firmware_rear",
    "/api/v2/device/firmware_abort",
    "/api/v2/device/reboot",
})
NETWORK_JSON_FIELDS = ("type", "ipAddress", "gatewayAddress", "mask")
# Firmware CHARGER_COMM_TIMEOUT_MS / COMM_TIMEOUT_MS. Timeout uses `>`, so 5 s is still ok.
COMM_TIMEOUT_MS = 5 * 1000
# Firmware ChargerStatus.required. totals / etaSeconds are additive (C7); 1.9.0 omits them.
STATUS_REQUIRED_KEYS = frozenset({"deviceStatus", "batteries"})


def map_device_status(seen_frame: bool, ms_since_frame: int) -> str:
    """charger_map_device_status(): starting until the first frame; then age > 5 s."""
    if not seen_frame:
        return "starting"
    if ms_since_frame > COMM_TIMEOUT_MS:
        return "charger_unreachable"
    return "ok"


# Emulator is both boards, so a frame is already here. Tests / set_frame change this.
charger_frame = {"seen": True, "age_ms": 0}


def device_status_now() -> str:
    return map_device_status(charger_frame["seen"], charger_frame["age_ms"])


twin = twin_snapshot.FirmwareSnapshot()
_last_snap: dict = {}
# Lab injects (set_cold / set_warm / set_error) until the next seat/pull clears them.
bay_overrides: dict[int, dict] = {}


def apply_twin_snap(snap: dict) -> None:
    """Copy one firmware snapshot into the HTTP/WS surfaces. No local totals."""
    global _last_snap
    _last_snap = snap
    bats = []
    for b in snap.get("batteries", []):
        row = {
            "index": b["index"],
            "batteryBayTempC": b["batteryBayTempC"],
            "slotState": b["slotState"],
            "stateOfChargePercent": b["stateOfChargePercent"],
            "timeRemainingSeconds": b["timeRemainingSeconds"],
            "errorMsg": b.get("errorMsg", ""),
            "batteryDetected": b.get("batteryDetected", ""),
        }
        ov = bay_overrides.get(b["index"])
        if ov:
            row.update(ov)
        bats.append(row)
    charger_state["batteries"] = bats
    charger_state["displayOverlay"] = snap.get("displayOverlay", "none")
    charger_state["thermalThrottleActive"] = bool(snap.get("thermalThrottleActive", False))
    charger_state["bay_lights"] = [b.get("led") for b in snap.get("batteries", [])]


def charger_status_fields():
    if twin.available:
        apply_twin_snap(twin.tick())
    fields = {
        "deviceStatus": device_status_now(),
        "batteries": charger_state["batteries"],
    }
    if _last_snap.get("totals") is not None:
        fields["totals"] = _last_snap["totals"]
    if _last_snap.get("etaSeconds") is not None:
        fields["etaSeconds"] = _last_snap["etaSeconds"]
    return fields


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
    "name": DEFAULT_DEVICE_NAME,
    "deviceInternalTemperatureC": 24.01,
    "firmwareVersion": "0.1.0",
    "firmwareBuild": "abc123",
    "network": {  # the firmware's `ip` object (spec IpConfig)
        "type": "dhcp",
        "ipAddress": host_ip,
        "gatewayAddress": "10.0.0.1",
        "mask": "255.255.255.0",
        "macAddress": "00:b0:d0:63:c2:26"
    },
    "thermalThrottleActive": False,
    "fanSpeedPercent": 0,
    "displayOverlay": "none",
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
    ],
    "bay_lights": [None] * 48,
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

# WebSocket state — matches firmware WS_MAX_CLIENTS=8
WS_MAX_CLIENTS = 8
ws_clients: list[WebSocket] = []
ws_lock = asyncio.Lock()
pending_push: bool = False  # debounce flag
_event_loop: asyncio.AbstractEventLoop | None = None  # captured at startup


async def ws_broadcast():
    """Send current status to all connected WS clients."""
    payload = {
        "event": "battery_status",
        "data": {
            **charger_status_fields(),
            "deviceInfo": build_device_info()
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
        if len(ws_clients) >= WS_MAX_CLIENTS:
            await websocket.close(code=1013, reason="server full")
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
    # Firmware OPTIONS is 204 + CORS on registered HTTP resources only.
    if request.method == "OPTIONS" and request.url.path in FIRMWARE_OPTIONS_PATHS:
        return Response(status_code=204, headers=CORS_PREFLIGHT_HEADERS)
    response = await call_next(request)
    for key, value in CORS_PREFLIGHT_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


@app.get("/api/v2/charger/status")
def get_status():
    return charger_status_fields()


@app.post("/api/v2/charger/set_frame")
def set_frame(seen: bool = Query(...), age_ms: int = Query(0)):
    """Emulator-only: set the charger-board frame inputs that pick deviceStatus."""
    if age_ms < 0:
        raise HTTPException(status_code=400, detail="age_ms must be >= 0")
    charger_frame["seen"] = seen
    charger_frame["age_ms"] = age_ms
    notify_state_change()
    return {"ok": True, "deviceStatus": device_status_now()}


# charger_overlay_api_name(): anything else the firmware reports as "none"
DISPLAY_OVERLAYS = {"none", "too_hot", "warm", "cold", "throttled", "too_cold"}


def build_device_info():
    """One builder for GET /device/info and the WebSocket deviceInfo (json_content.c emit_device_info)."""
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

    overlay = charger_state["displayOverlay"]
    return {
        "name": charger_state["name"],
        "firmwareVersion": charger_state["firmwareVersion"],
        "firmwareBuild": charger_state["firmwareBuild"],
        "ip": charger_state["network"],
        "thermalThrottleActive": bool(charger_state["thermalThrottleActive"]),
        "fanSpeedPercent": int(charger_state["fanSpeedPercent"]),
        "displayOverlay": overlay if overlay in DISPLAY_OVERLAYS else "none",
        "status": status
    }


@app.get("/api/v2/device/info")
def get_device_info():
    return build_device_info()


def _plain(status: int, body: str = "") -> Response:
    return Response(content=body, status_code=status)


def _cut_device_name(name: str) -> str:
    """device_name_from_json(): reject empty / NUL / bad UTF-8; cut at 19 whole UTF-8 bytes."""
    if "\x00" in name:
        raise ValueError("Invalid name")
    try:
        raw = name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Invalid name") from exc
    cut = raw[:DEVICE_NAME_MAX_LEN].decode("utf-8", errors="ignore")
    if not cut:
        raise ValueError("Invalid name")
    return cut


def _parse_json_object(raw: bytes):
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_ipv4(value):
    """parse_ip_address(): sscanf %u.%u.%u.%u, then encode as %d.%d.%d.%d."""
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) != 4:
        return None
    nums = []
    for part in parts:
        if not part.isascii() or not part.isdigit():
            return None
        n = int(part)
        if n > 255:
            return None
        nums.append(n)
    return "{}.{}.{}.{}".format(*nums)


def require_slot(slot: int):
    if slot < 0 or slot >= 48:
        raise HTTPException(status_code=400, detail="Invalid slot number")


def require_occupied(slot: int):
    require_slot(slot)
    if not charger_state["batteries"][slot]["batteryDetected"]:
        raise HTTPException(status_code=400, detail="Invalid slot number")


@app.get("/api/v2/device/name")
def get_device_name():
    return _plain(200, charger_state["name"])


@app.post("/api/v2/device/name")
async def post_device_name(request: Request):
    parsed = _parse_json_object(await request.body())
    if parsed is None or not isinstance(parsed.get("name"), str):
        return _plain(400, "Invalid JSON object given")
    try:
        charger_state["name"] = _cut_device_name(parsed["name"])
    except ValueError:
        return _plain(400, "Invalid name")
    notify_state_change()
    return _plain(200)


@app.api_route("/api/v2/device/identify", methods=["GET", "POST"])
def device_identify():
    return _plain(200)


@app.get("/api/v2/device/network")
def get_device_network():
    return charger_state["network"]


@app.post("/api/v2/device/network")
async def post_device_network(request: Request):
    parsed = _parse_json_object(await request.body())
    if parsed is None:
        return _plain(400, "Invalid JSON object given")
    if any(key in parsed and not isinstance(parsed[key], str) for key in NETWORK_JSON_FIELDS):
        return _plain(400, "Invalid JSON object given")
    addr_keys = ("ipAddress", "gatewayAddress", "mask")
    typ = parsed.get("type")
    all_fields = typ is not None and all(k in parsed for k in addr_keys)
    if typ == "dhcp":
        charger_state["network"]["type"] = "dhcp"
        notify_state_change()
        return _plain(200)
    if typ == "static" and all_fields:
        addrs = [_parse_ipv4(parsed[k]) for k in addr_keys]
        if any(addr is None for addr in addrs):
            return _plain(400, "Invalid address given")
        charger_state["network"]["type"] = "static"
        for key, addr in zip(addr_keys, addrs):
            charger_state["network"][key] = addr
        notify_state_change()
        return _plain(200)
    if all_fields and typ not in ("dhcp", "static"):
        return _plain(400, "Invalid address type")
    return _plain(400, "Invalid JSON object given")


@app.api_route("/api/v2/device/firmware_abort", methods=["GET", "POST"])
def firmware_abort():
    firmware_state["main_firmware_pending"] = False
    firmware_state["rear_firmware_pending"] = False
    firmware_state["main_firmware_size"] = 0
    firmware_state["rear_firmware_size"] = 0
    firmware_state["target_version"] = None
    return _plain(200, "OTA cancelled")


@app.get("/api/v2/device/firmware_version")
def get_firmware_version():
    return {
        "firmwareRear": charger_state["firmwareVersion"],
        "firmwareMain": charger_state["firmwareVersion"]
    }


@app.post("/api/v2/charger/insert/{slot}")
async def insert(slot: int, type: str = Query(...)):
    require_slot(slot)
    if type not in SPEC_BATTERY_TYPES:
        raise HTTPException(status_code=400, detail="Invalid battery type")
    if not twin.available:
        raise HTTPException(status_code=503, detail="firmware snapshot library not loaded")
    bay_overrides.pop(slot, None)
    # Injected readings: low SoC cell, remaining for the twin Fill/seat path.
    rem = 90 * 60
    apply_twin_snap(twin.seat(slot, twin_snapshot.type_code(type), 5, 24, rem))
    notify_state_change()
    return {"ok": True}


@app.post("/api/v2/charger/eject/{slot}")
async def eject(slot: int):
    require_slot(slot)
    if not twin.available:
        raise HTTPException(status_code=503, detail="firmware snapshot library not loaded")
    bay_overrides.pop(slot, None)
    apply_twin_snap(twin.pull(slot))
    notify_state_change()
    return {"ok": True}


@app.post("/api/v2/charger/bulk_insert")
async def bulk_insert(
    no_aa: bool = Query(False, description="When true, only insert AAA batteries"),
    no_aaa: bool = Query(False, description="When true, only insert AA batteries"),
):
    """Fill: deal a different rack each time. States come from the firmware seat step."""
    import random

    if no_aa and no_aaa:
        raise HTTPException(status_code=400, detail="Cannot set both no_aa and no_aaa")
    if not twin.available:
        raise HTTPException(status_code=503, detail="firmware snapshot library not loaded")

    bay_overrides.clear()
    twin.clear_all()
    battery_types = ["KLVR-AA", "KLVR-AAA"]
    if no_aa:
        battery_types = ["KLVR-AAA"]
    elif no_aaa:
        battery_types = ["KLVR-AA"]

    n = random.randint(12, 40)
    slots_to_fill = random.sample(range(48), n)
    full_batteries_count = random.randint(2, max(3, n // 8))
    full_battery_slots = set(random.sample(slots_to_fill, min(full_batteries_count, n)))

    batteries_inserted = []
    for slot in slots_to_fill:
        battery_type = random.choice(battery_types)
        if slot in full_battery_slots:
            soc = 100
            rem = 0
        else:
            soc = random.randint(5, 90)
            rem = random.randint(600, 8000)
        twin.seat(slot, twin_snapshot.type_code(battery_type), soc, 24, rem)
        batteries_inserted.append({"index": slot, "type": battery_type, "soc": soc, "timeRemaining": rem})

    apply_twin_snap(twin.tick())
    full_count = len(full_battery_slots)
    aa_count = len([b for b in batteries_inserted if b["type"] == "KLVR-AA"])
    aaa_count = len([b for b in batteries_inserted if b["type"] == "KLVR-AAA"])
    notify_state_change()
    return {
        "ok": True,
        "message": f"Filled {len(batteries_inserted)} bays ({full_count} full)",
        "batteries": batteries_inserted,
        "summary": {
            "total": len(batteries_inserted),
            "aa_count": aa_count,
            "aaa_count": aaa_count,
            "avg_soc": round(sum(b["soc"] for b in batteries_inserted) / len(batteries_inserted), 1),
            "full_batteries": full_count,
            "no_aa": no_aa,
            "no_aaa": no_aaa,
        },
    }


@app.post("/api/v2/charger/set_cold/{slot}")
async def set_cold(slot: int):
    """Set a slot to cold state (BATTERY_STATE_TEMP_COLD)."""
    require_occupied(slot)
    bay_overrides[slot] = {"slotState": "cold", "errorMsg": "cold", "timeRemainingSeconds": 0}
    if twin.available:
        apply_twin_snap(twin.tick())
    notify_state_change()
    return {"ok": True}


@app.post("/api/v2/charger/set_warm/{slot}")
async def set_warm(slot: int):
    """Set a slot to warm state (BATTERY_STATE_TEMP_WARM)."""
    require_occupied(slot)
    bay_overrides[slot] = {"slotState": "warm", "errorMsg": "warm", "timeRemainingSeconds": 0}
    if twin.available:
        apply_twin_snap(twin.tick())
    notify_state_change()
    return {"ok": True}


@app.post("/api/v2/charger/bulk_clear")
async def bulk_clear():
    """Empty the rack through the firmware pull step."""
    if not twin.available:
        raise HTTPException(status_code=503, detail="firmware snapshot library not loaded")
    bay_overrides.clear()
    apply_twin_snap(twin.clear_all())
    notify_state_change()
    return {"ok": True}


VALID_ERROR_TYPES = {"overtemp", "overcurrent", "faulty", "detect_err"}

@app.post("/api/v2/charger/set_error/{slot}")
async def set_error(slot: int, type: str = Query(...)):
    """Set a slot to error state with a specific errorMsg."""
    require_occupied(slot)
    if type not in VALID_ERROR_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid error type. Must be one of: {', '.join(sorted(VALID_ERROR_TYPES))}")
    bay_overrides[slot] = {"slotState": "error", "errorMsg": type, "timeRemainingSeconds": 0}
    if twin.available:
        apply_twin_snap(twin.tick())
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
    board_bytes = await request.body()
    if board_bytes not in (b"main", b"rear"):
        return _plain(400, "Expected 'rear' or 'main'")
    board = board_bytes.decode()

    try:
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
    """Inject a SoC reading and re-seat through the firmware step."""
    try:
        data = await request.json()
        percentage = float(data.get("percentage", 0.0))
        require_occupied(slot)
        if percentage < 0 or percentage > 100:
            raise HTTPException(status_code=400, detail="Percentage must be between 0 and 100")
        if not twin.available:
            raise HTTPException(status_code=503, detail="firmware snapshot library not loaded")
        b = charger_state["batteries"][slot]
        detected = b.get("batteryDetected") or "KLVR-AA"
        soc = int(round(percentage))
        rem = 0 if soc >= 100 else max(60, (100 - soc) * 60)
        bay_overrides.pop(slot, None)
        apply_twin_snap(twin.seat(slot, twin_snapshot.type_code(detected), soc, 24, rem))
        notify_state_change()
        out = charger_state["batteries"][slot]
        return {
            "status": "success",
            "index": slot,
            "percentage": out["stateOfChargePercent"],
            "timeRemaining": out["timeRemainingSeconds"],
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid percentage value")
    except Exception as e:
        print(f"❌ Error setting charge percentage: {e}")
        raise HTTPException(status_code=500, detail="Failed to set charge percentage")


@app.get("/api/v2/twin/meta")
def twin_meta():
    return {
        "firmware_pin": twin_snapshot.firmware_pin(),
        "lib_path": twin.lib_path,
        "available": twin.available,
    }


@app.get("/api/v2/twin/lights")
def twin_lights():
    if twin.available:
        apply_twin_snap(twin.tick())
    return {"lights": charger_state.get("bay_lights") or []}


@app.get("/", response_class=HTMLResponse)
def ui():
    return Path("templates/index.html").read_text()

app.mount("/static", StaticFiles(directory="static"), name="static")


def bonjour():
    z = Zeroconf()
    simulated_mac = f"0080e1{runtime_port:06x}"
    service_name = f"klvr{simulated_mac}"

    info = ServiceInfo(
        type_="_klvrcharger._tcp.local.",
        name=f"{service_name}._klvrcharger._tcp.local.",
        addresses=[socket.inet_aton(host_ip)],
        port=runtime_port,
        properties=MDNS_TXT,
        server=f"klvr-emulator-{runtime_port}.local."
    )
    z.register_service(info)
    atexit.register(lambda: z.unregister_service(info))
    print(f"✅ Emulator running at http://{host_ip}:{runtime_port}")
    print(f"🔗 Bonjour service registered as: {service_name}")


# Start background threads
threading.Thread(target=bonjour, daemon=True).start()

if twin.available:
    apply_twin_snap(twin.tick())
    print(f"🔌 Firmware twin snapshot: {twin.lib_path} (pin {twin_snapshot.firmware_pin()[:12]})")
else:
    print("⚠️  libcharger_twin not loaded — seat/Fill/status need CHARGER_TWIN_LIB")
