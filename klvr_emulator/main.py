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
    "main_firmware_version": None,
    "rear_firmware_version": None,
    "target_version": None,
    "main_rebooting": False,
    "rear_rebooting": False
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
    # Check if any board is rebooting
    main_rebooting = firmware_state.get("main_rebooting", False)
    rear_rebooting = firmware_state.get("rear_rebooting", False)
    
    # Determine status based on rebooting state
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

@app.post("/api/v2/charger/insert/{slot}")
def insert(slot: int, type: str = Query(...)):
    b = charger_state["batteries"][slot]
    b["slotState"] = "charging"
    b["stateOfChargePercent"] = 5.0
    b["timeRemainingSeconds"] = 7200  # 2 hours = 7200 seconds
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

@app.post("/api/v2/charger/bulk_insert")
def bulk_insert():
    """Insert 37 batteries with random types (AA/AAA) and SOC levels, always including 4-5 full batteries"""
    import random
    
    # First, clear all slots
    for b in charger_state["batteries"]:
        b["slotState"] = "empty"
        b["stateOfChargePercent"] = 0.0
        b["timeRemainingSeconds"] = 0
        b["batteryDetected"] = ""
    
    # Define battery types
    battery_types = ["KLVR-AA", "KLVR-AAA"]
    
    # Insert 37 batteries in random slots (0-47, total 48 slots)
    slots_to_fill = random.sample(range(48), 37)
    
    # Determine how many full batteries to include (4-5)
    full_batteries_count = random.randint(4, 5)
    full_battery_slots = random.sample(slots_to_fill, full_batteries_count)
    
    batteries_inserted = []
    full_count = 0
    
    for slot in slots_to_fill:
        # Random battery type
        battery_type = random.choice(battery_types)
        
        # Set SOC - 100% for selected full battery slots, random for others
        if slot in full_battery_slots:
            soc = 100.0
            full_count += 1
            slot_state = "done"  # ← FIXED: Use "done" not "complete"
            time_remaining = 0
        else:
            # Random SOC between 5% and 95% for non-full batteries
            soc = random.uniform(5.0, 95.0)
            slot_state = "charging"
            # Calculate remaining time based on SOC
            # Charging goes from 5% to 100% (95% range) in 7200 seconds (2 hours)
            remaining_charge_needed = max(0, 100.0 - soc)
            total_charge_range = 95.0  # From 5% to 100%
            total_time_for_full_charge = 7200  # 2 hours in seconds
            time_remaining = int((remaining_charge_needed / total_charge_range) * total_time_for_full_charge)
        
        # Update battery
        b = charger_state["batteries"][slot]
        b["slotState"] = slot_state
        b["stateOfChargePercent"] = soc
        b["timeRemainingSeconds"] = time_remaining
        b["batteryDetected"] = battery_type
        
        batteries_inserted.append({
            "slot": slot,
            "type": battery_type,
            "soc": round(soc, 1),
            "timeRemaining": time_remaining
        })
    
    print(f"🔋 Bulk inserted 37 batteries: {len([b for b in batteries_inserted if b['type'] == 'KLVR-AA'])} AA, {len([b for b in batteries_inserted if b['type'] == 'KLVR-AAA'])} AAA ({full_count} full)")
    
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

# Firmware Update Endpoints

@app.post("/api/v2/device/firmware_charger")
async def upload_main_firmware(request: Request, version: str = Query(default=None)):
    """Upload main board firmware"""
    try:
        firmware_data = await request.body()
        firmware_size = len(firmware_data)
        
        # Simple version detection with debugging
        print(f"🔍 MAIN: Query params: {dict(request.query_params)}")
        print(f"🔍 MAIN: All headers: {dict(request.headers)}")
        
        # Get version (simplified - try multiple sources)
        simulated_version = (
            version or  # Query parameter
            request.headers.get("x-firmware-version") or 
            request.headers.get("firmware-version") or
            "1.6.3"  # Default to expected version
        )
        
        print(f"✅ Received main firmware: {firmware_size:,} bytes (version: {simulated_version})")
        firmware_state["main_firmware_size"] = firmware_size
        firmware_state["main_firmware_pending"] = True
        firmware_state["main_firmware_version"] = simulated_version
        
        # Set target version when first firmware is uploaded
        if not firmware_state["target_version"]:
            firmware_state["target_version"] = simulated_version
        
        print(f"📊 Updated Firmware State: main_pending={firmware_state['main_firmware_pending']}, rear_pending={firmware_state['rear_firmware_pending']}, target_version={firmware_state['target_version']}")
        
        # Simulate processing time
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
        
        # Simple version detection with debugging
        print(f"🔍 REAR: Query params: {dict(request.query_params)}")
        print(f"🔍 REAR: All headers: {dict(request.headers)}")
        
        # Get version (simplified - try multiple sources)
        simulated_version = (
            version or  # Query parameter
            request.headers.get("x-firmware-version") or 
            request.headers.get("firmware-version") or
            firmware_state.get("target_version") or  # Use same version as main
            "1.6.3"  # Default to expected version
        )
        
        print(f"✅ Received rear firmware: {firmware_size:,} bytes (version: {simulated_version})")
        firmware_state["rear_firmware_size"] = firmware_size
        firmware_state["rear_firmware_pending"] = True
        firmware_state["rear_firmware_version"] = simulated_version
        
        # Set target version when first firmware is uploaded
        if not firmware_state["target_version"]:
            firmware_state["target_version"] = simulated_version
        
        print(f"📊 Updated Firmware State: main_pending={firmware_state['main_firmware_pending']}, rear_pending={firmware_state['rear_firmware_pending']}, target_version={firmware_state['target_version']}")
        
        # Simulate processing time
        await asyncio.sleep(2)
        
        return {"status": "success", "message": f"Rear firmware uploaded successfully (version: {simulated_version})"}
    except Exception as e:
        print(f"❌ Error uploading rear firmware: {e}")
        raise HTTPException(status_code=500, detail="Firmware upload failed")

@app.post("/api/v2/device/reboot")
async def reboot_board(request: Request):
    """Reboot main or rear board - EMULATED with realistic delays"""
    try:
        board = (await request.body()).decode().strip()
        
        if board not in ["main", "rear"]:
            raise HTTPException(status_code=400, detail="Invalid board name. Use 'main' or 'rear'")
        
        print(f"🔄 Emulating {board} board reboot...")
        
        # Set rebooting state for this specific board
        reboot_key = f"{board}_rebooting"
        firmware_state[reboot_key] = True
        
        # Emulate reboot process in background
        async def emulated_reboot():
            # Realistic reboot delay for emulation
            await asyncio.sleep(2)
            
            # Apply firmware for the rebooted board
            if board == "main" and firmware_state["main_firmware_pending"]:
                firmware_state["main_firmware_pending"] = False
                print(f"✅ Main firmware applied! ({firmware_state['main_firmware_size']:,} bytes)")
                
            elif board == "rear" and firmware_state["rear_firmware_pending"]:
                firmware_state["rear_firmware_pending"] = False
                print(f"✅ Rear firmware applied! ({firmware_state['rear_firmware_size']:,} bytes)")
            
            # Check if both firmwares have been applied and update version
            if not firmware_state["main_firmware_pending"] and not firmware_state["rear_firmware_pending"] and \
               firmware_state["main_firmware_size"] > 0 and firmware_state["rear_firmware_size"] > 0:
                target_version = firmware_state["target_version"] or "0.1.5"
                charger_state["firmwareVersion"] = target_version
                print(f"🎉 Firmware update complete! Version: {target_version}")
            
            # Clear rebooting state
            firmware_state[reboot_key] = False
            print(f"✅ {board.capitalize()} board reboot complete")
            print(f"📊 Firmware State: main_pending={firmware_state['main_firmware_pending']}, rear_pending={firmware_state['rear_firmware_pending']}, version={charger_state['firmwareVersion']}")
        
        # Start emulated reboot in background (non-blocking)
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
        
        # Validate slot number
        if slot < 0 or slot >= 48:
            raise HTTPException(status_code=400, detail="Invalid slot number")
        
        # Validate percentage
        if percentage < 0 or percentage > 100:
            raise HTTPException(status_code=400, detail="Percentage must be between 0 and 100")
        
        battery = charger_state["batteries"][slot]
        
        # Update the battery charge percentage
        battery["stateOfChargePercent"] = percentage
        
        # Calculate remaining time based on charge percentage
        # Charging goes from 5% to 100% (95% total) in 7200 seconds (2 hours)
        # Formula: remaining_time = (100 - current_percentage) / 95 * 7200
        if battery["slotState"] == "charging" and percentage < 100:
            remaining_charge_needed = max(0, 100.0 - percentage)
            total_charge_range = 95.0  # From 5% to 100%
            total_time_for_full_charge = 7200  # 2 hours in seconds
            
            battery["timeRemainingSeconds"] = int((remaining_charge_needed / total_charge_range) * total_time_for_full_charge)
        elif percentage >= 100:
            battery["slotState"] = "done"
            battery["timeRemainingSeconds"] = 0
        elif battery["slotState"] == "empty":
            battery["timeRemainingSeconds"] = 0
            
        print(f"🔋 Set slot {slot} charge to: {percentage}% (remaining: {battery['timeRemainingSeconds']}s)")
        
        return {"status": "success", "slot": slot, "percentage": percentage, "timeRemaining": battery["timeRemainingSeconds"]}
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid percentage value")
    except Exception as e:
        print(f"❌ Error setting charge percentage: {e}")
        raise HTTPException(status_code=500, detail="Failed to set charge percentage")

@app.get("/", response_class=HTMLResponse)
def ui():
    return Path("templates/index.html").read_text()

app.mount("/static", StaticFiles(directory="static"), name="static")

# Charging simulation
def loop():
    while True:
        for b in charger_state["batteries"]:
            if b["slotState"] == "charging":
                # Realistic charging: 95% in 2 hours = 0.0132% per second
                # For simulation speed, use 0.0264% every 2 seconds (still 95% in 2 hours)
                charge_increment = 0.0264  # This ensures 95% charge takes exactly 7200 seconds
                b["stateOfChargePercent"] = min(100.0, b["stateOfChargePercent"] + charge_increment)
                
                # Decrease remaining time by 2 seconds each loop
                b["timeRemainingSeconds"] = max(0, b["timeRemainingSeconds"] - 2)
                
                if b["stateOfChargePercent"] >= 100.0:
                    b["slotState"] = "done"
                    b["timeRemainingSeconds"] = 0
        time.sleep(2)

def bonjour():
    z = Zeroconf()
    # Generate MAC-like identifier for this emulator instance (based on port)
    # Format: klvrXXXXXXXXXXXX (simulating MAC address pattern)
    simulated_mac = f"0080e1{runtime_port:06x}"  # Use port to create unique MAC-like ID
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
