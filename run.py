import socket
import uvicorn
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

def get_local_ip():
    try:
        # Create a dummy socket connection to detect local IP (no traffic sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


from klvr_emulator import main as emulator_main

if __name__ == "__main__":
    port = find_available_port()
    ip = get_local_ip()

    # Pass the actual port to main.py
    emulator_main.runtime_port = port

    print(f"✅ Emulator running at http://{ip}:{port}")
    uvicorn.run(emulator_main.app, host="0.0.0.0", port=port, log_level="info")

