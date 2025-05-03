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

if __name__ == "__main__":
    port = find_available_port()
    ip = get_local_ip()
    print(f"✅ Emulator running at http://{ip}:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
