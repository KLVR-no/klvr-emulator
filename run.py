import socket
import uvicorn
from klvr_emulator.main import app

def find_available_port(start_port=9000, max_tries=50):
    for port in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError("❌ No available ports found in range 8000–8050")

if __name__ == "__main__":
    port = find_available_port()
    print(f"✅ Starting emulator on available port: {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
