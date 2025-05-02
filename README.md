# KLVR Charger Emulator

A 48-slot charger emulator that replicates the KLVR Charger Pro API and device behavior.

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

---

## 🌐 Access the Web UI

Once running, open your browser to:
- [http://localhost:8000](http://localhost:8000)
- Or on LAN: `http://<your-ip>:8000`

---

## 💡 Features

- Web UI showing 48 charger slots in physical layout
- Individual Insert AA, Insert AAA, and Eject controls per slot
- Simulates charging with live SoC updates
- CORS-enabled API for integration testing
- Bonjour/mDNS service broadcast (_klvrcharger._tcp.local)

---

## 📡 API Endpoints

### Charger Status
```
GET /api/v2/charger/status
```

### Device Info
```
GET /api/v2/device/info
```

### Insert Battery
```
POST /api/v2/charger/insert/{slot}?type=KLVR-AA
```

### Eject Battery
```
POST /api/v2/charger/eject/{slot}
```

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