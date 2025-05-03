import socket
import uvicorn
import netifaces
from klvr_emulator.main import app

def find_available_port(start_port=8000, max_tries=50):
    for port in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError("❌ No available ports found in range 8000–8050")

def get_local_ip(interface="en0"):
    try:
        return netifaces.ifaddresses(interface)[netifaces.AF_INET][0]["addr"]
    except Exception:
        return "localhost"

if __name__ == "__main__":
    port = find_available_port()
    ip = get_local_ip()
    print(f"✅ Emulator running at http://{ip}:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
