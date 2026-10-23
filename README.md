# KLVR Charger Pro Emulator

A 48-slot charger emulator that replicates the KLVR Charger Pro API and device behavior.

## Canon

The emulator follows the firmware; it never leads. What the charger sends, and when, is decided in the firmware repo `KLVR-no/klvr-chargerpro-firmware` on the active train branch (today `release/v1.9.1`):

- `docs/user-facing-contract.md`: states, words, compatibility rules (§8), the gap ledger
- `docs/api-integration-guide.md`: endpoints and fields
- `docs/agents/README.md`: how a firmware API change reaches the emulator and Klvr Control in the same release train

An API change lands in the firmware first, then here, then in Klvr Control. If the emulator and a real charger disagree, the charger is right and the emulator gets an issue.

---

## 🧰 Requirements

- Python 3.11+
- Git
- macOS or Linux

---

## ⚡ Quickstart

```bash
git clone https://github.com/klvr-no/klvr-emulator.git
cd klvr-emulator
chmod +x start.sh
./start.sh
```

This script will:
- Create a virtual environment
- Install dependencies
- Start the emulator server
- Print the active IP address the emulator is running on

Example output:
```
Resolved local IP address: 10.0.1.42
Emulator running at http://10.0.1.42:8000
```

---

## 🌐 Access the Web UI

Once running, open your browser to:
- [http://localhost:8000](http://localhost:8000)
- Or on LAN: `http://<your-ip>:8000` (printed in terminal)

---

## 💡 Features

- Web UI showing 48 charger slots in physical layout
- Individual Insert AA, Insert AAA, and Eject controls per slot
- Simulates charging with live SoC updates
- CORS-enabled API for integration testing
- Bonjour/mDNS service broadcast (_klvrcharger._tcp.local)
- IP address printed at startup for discovery on local network

---

## 📡 API Endpoints

These endpoints simulate the KLVR Charger Pro firmware interface. All endpoints support CORS and return JSON. Designed for local testing and third-party integrations.

### Charger Status
Returns a full list of all 48 slots and their current charge state.
```
GET /api/v2/charger/status
```
Response:
```json
{
  "deviceStatus": "ok",
  "batteries": [
    { "slot": 0, "slotState": "charging", ... },
    ...
  ]
}
```

### Device Info
Returns static info about the charger including firmware and network info.
```
GET /api/v2/device/info
```
Response:
```json
{
  "name": "Laddare #1",
  "firmwareVersion": "0.1.0",
  "firmwareBuild": "abc123",
  "deviceInternalTemperatureC": 24.01,
  "ip": {
    "ipAddress": "...",
    "gatewayAddress": "...",
    "mask": "...",
    "macAddress": "...",
    "method": "dhcp"
  }
}
```

### Insert Battery
Simulates inserting a battery into a slot.
```
POST /api/v2/charger/insert/{slot}?type=KLVR-AA
```
Accepted values for `type`: `KLVR-AA`, `KLVR-AAA`

### Eject Battery
Removes the battery and resets slot state.
```
POST /api/v2/charger/eject/{slot}
```

### (Planned for extension)
```
POST /api/v2/device/firmware         # Stub: accept firmware update
POST /api/v2/device/identify         # Stub: trigger blink indicator
POST /api/v2/device/network          # Stub: simulate network config
POST /api/v2/device/name             # Stub: rename device
```

These are exposed but not fully implemented.

---

## 🧼 Cleanup
To stop:
```bash
Ctrl+C
```
To deactivate environment:
```bash
deactivate
```

---

## 📡 Bonjour / mDNS
Broadcasts via:
```
_KLVR Charger Pro._klvrcharger._tcp.local
```

Discovered automatically by compatible KLVR client apps.
