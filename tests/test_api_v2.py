"""Shape checks for the API v2 surface the firmware serves (docs/api/v2.openapi.json in
klvr-chargerpro-firmware). The full check is the firmware's scripts/api_conformance.py."""
import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request

import uvicorn
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from klvr_emulator import main

IP_KEYS = {"type", "ipAddress", "gatewayAddress", "mask", "macAddress"}
OVERLAYS = ["none", "too_hot", "warm", "cold", "throttled", "too_cold"]  # charger_overlay_api_name()
INFO_KEYS = {"name", "firmwareVersion", "firmwareBuild", "ip", "thermalThrottleActive",
             "fanSpeedPercent", "displayOverlay"}


class ApiV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"
        cls.server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=cls.server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                cls.server.should_exit = True
                raise RuntimeError("emulator server did not start")
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True

    def call(self, path, method="GET"):
        req = urllib.request.Request(f"{self.base}/api/v2{path}", method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, None

    def test_info_has_firmware_fields(self):
        _, info = self.call("/device/info")
        self.assertLessEqual(INFO_KEYS, info.keys())
        self.assertIsInstance(info["firmwareBuild"], str)
        self.assertIs(info["thermalThrottleActive"], False)
        self.assertIs(type(info["fanSpeedPercent"]), int)  # a bool is an int in Python
        self.assertIs(type(info["thermalThrottleActive"]), bool)
        self.assertIn(info["displayOverlay"], OVERLAYS)
        self.assertLessEqual(IP_KEYS, info["ip"].keys())
        self.assertIn(info["ip"]["type"], ["dhcp", "static"])
        self.assertEqual(info["ip"]["macAddress"], info["ip"]["macAddress"].lower())

    def test_info_coerces_like_the_firmware_encoder(self):
        state = main.charger_state
        saved = {k: state[k] for k in ("fanSpeedPercent", "thermalThrottleActive", "displayOverlay")}
        self.addCleanup(state.update, saved)
        state.update(fanSpeedPercent=True, thermalThrottleActive=1, displayOverlay="bogus")
        _, info = self.call("/device/info")
        self.assertIs(type(info["fanSpeedPercent"]), int)
        self.assertIs(type(info["thermalThrottleActive"]), bool)
        self.assertEqual(info["displayOverlay"], "none")
        state["fanSpeedPercent"] = 42.5
        self.assertEqual(self.call("/device/info")[1]["fanSpeedPercent"], 42)
        for overlay in OVERLAYS:
            state["displayOverlay"] = overlay
            self.assertEqual(self.call("/device/info")[1]["displayOverlay"], overlay)

    def test_network_is_the_ip_object_of_info(self):
        status, net = self.call("/device/network")
        self.assertEqual(status, 200)
        self.assertEqual(net, self.call("/device/info")[1]["ip"])

    def test_set_error_refuses_undertemp(self):
        self.assertEqual(self.call("/charger/set_error/0?type=undertemp", "POST")[0], 400)
        self.assertEqual(self.call("/charger/set_error/0?type=overtemp", "POST")[0], 200)
        self.call("/charger/eject/0", "POST")

    def test_websocket_carries_device_info(self):
        with connect(self.base.replace("http", "ws") + "/api/v2/ws") as ws:
            msg = json.loads(ws.recv(timeout=5))
        self.assertEqual(msg["event"], "battery_status")
        self.assertEqual(msg["data"]["deviceInfo"].keys() & INFO_KEYS, INFO_KEYS)

    def test_ninth_websocket_client_gets_1013_server_full(self):
        """Firmware WS_MAX_CLIENTS=8; overflow closes with 1013 and reason 'server full' (#171)."""
        ws_url = self.base.replace("http", "ws") + "/api/v2/ws"
        held = []
        try:
            for _ in range(8):
                held.append(connect(ws_url))
            ninth = connect(ws_url)
            try:
                with self.assertRaises(ConnectionClosed) as ctx:
                    ninth.recv(timeout=5)
                closed = ctx.exception
            finally:
                ninth.close()
            self.assertEqual(closed.code, 1013)
            self.assertEqual(closed.reason, "server full")
        finally:
            for ws in held:
                ws.close()


if __name__ == "__main__":
    unittest.main()
