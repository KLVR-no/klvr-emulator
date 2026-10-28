"""Shape checks for the API v2 surface the firmware serves (docs/api/v2.openapi.json in
klvr-chargerpro-firmware). The full check is the firmware's scripts/api_conformance.py."""
import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect
from zeroconf import ServiceInfo

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

    def raw(self, path, method="GET", data=None, content_type=None):
        headers = {}
        body = None
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        elif isinstance(data, str):
            body = data.encode()
        elif isinstance(data, bytes):
            body = data
        if content_type is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(
            f"{self.base}/api/v2{path}", data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            payload = e.read()
            headers = {k.lower(): v for k, v in e.headers.items()}
            e.close()
            return e.code, payload, headers

    def _restore_name(self, name):
        main.charger_state["name"] = name

    def _restore_network(self, network):
        main.charger_state["network"] = network

    def _eject(self, slot):
        self.call(f"/charger/eject/{slot}", "POST")

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
        self.call("/charger/insert/0?type=KLVR-AA", "POST")
        self.addCleanup(self._eject, 0)
        self.assertEqual(self.call("/charger/set_error/0?type=undertemp", "POST")[0], 400)
        self.assertEqual(self.call("/charger/set_error/0?type=overtemp", "POST")[0], 200)

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

    def test_device_name_defaults_to_klvr(self):
        """Issue #4: GET /device/name and deviceInfo.name default to Klvr, not KLVR."""
        status, body, headers = self.raw("/device/name")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"Klvr")
        self.assertNotEqual(headers.get("content-type", ""), "application/json")
        self.assertEqual(self.call("/device/info")[1]["name"], "Klvr")

    def test_device_name_post_matches_firmware(self):
        """POST /device/name: 19-byte UTF-8 cut, 400 on invalid JSON or \\u0000 (http_server.c)."""
        saved = main.charger_state["name"]
        self.addCleanup(self._restore_name, saved)

        status, body, _ = self.raw("/device/name", "POST", {"name": "Stage Left"})
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(self.raw("/device/name")[1], b"Stage Left")
        self.assertEqual(self.call("/device/info")[1]["name"], "Stage Left")

        # 20 ASCII bytes → last whole character that fits is 19.
        status, body, _ = self.raw("/device/name", "POST", {"name": "A" * 20})
        self.assertEqual(status, 200)
        self.assertEqual(self.raw("/device/name")[1], b"A" * 19)

        # "Ladestasjonen Nordøst": 18 + 2-byte ø + 2. Cut before ø (device_name.h).
        status, _, _ = self.raw("/device/name", "POST", {"name": "Ladestasjonen Nordøst"})
        self.assertEqual(status, 200)
        self.assertEqual(self.raw("/device/name")[1], "Ladestasjonen Nord".encode())

        status, body, _ = self.raw("/device/name", "POST", "not-json", "text/plain")
        self.assertEqual(status, 400)
        self.assertEqual(body, b"Invalid JSON object given")

        status, body, _ = self.raw("/device/name", "POST", {"nome": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(body, b"Invalid JSON object given")

        status, body, _ = self.raw("/device/name", "POST", {"name": "Lab\u0000"})
        self.assertEqual(status, 400)
        self.assertEqual(body, b"Invalid name")

    def test_device_identify(self):
        """GET|POST /device/identify return 200 and an empty body (Done)."""
        for method in ("GET", "POST"):
            status, body, _ = self.raw("/device/identify", method)
            self.assertEqual(status, 200, method)
            self.assertEqual(body, b"", method)

    def test_device_network_post_matches_firmware(self):
        """POST /device/network: dhcp/static bodies and firmware 400 texts."""
        saved = dict(main.charger_state["network"])
        self.addCleanup(self._restore_network, saved)

        status, body, _ = self.raw("/device/network", "POST", {"type": "dhcp"})
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(self.call("/device/network")[1]["type"], "dhcp")

        static = {
            "type": "static",
            "ipAddress": "192.168.1.66",
            "gatewayAddress": "192.168.0.1",
            "mask": "255.255.0.0",
        }
        status, body, _ = self.raw("/device/network", "POST", static)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        net = self.call("/device/network")[1]
        self.assertEqual(net["type"], "static")
        self.assertEqual(net["ipAddress"], "192.168.1.66")
        self.assertEqual(net["gatewayAddress"], "192.168.0.1")
        self.assertEqual(net["mask"], "255.255.0.0")

        status, body, _ = self.raw("/device/network", "POST", "not-json", "text/plain")
        self.assertEqual(status, 400)
        self.assertEqual(body, b"Invalid JSON object given")

        status, body, _ = self.raw(
            "/device/network",
            "POST",
            {**static, "type": "wifi"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body, b"Invalid address type")

        status, body, _ = self.raw(
            "/device/network",
            "POST",
            {**static, "ipAddress": "999.1.1.1"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body, b"Invalid address given")

        status, body, _ = self.raw("/device/network", "POST", {"type": "static"})
        self.assertEqual(status, 400)
        self.assertEqual(body, b"Invalid JSON object given")

    def test_firmware_abort(self):
        """GET|POST /device/firmware_abort return 200 and 'OTA cancelled'."""
        for method in ("GET", "POST"):
            status, body, _ = self.raw("/device/firmware_abort", method)
            self.assertEqual(status, 200, method)
            self.assertEqual(body, b"OTA cancelled", method)

    def test_options_where_firmware_serves_them(self):
        """OPTIONS 204 + CORS on firmware HTTP routes; /ws has no OPTIONS (spec)."""
        for path in (
            "/device/name",
            "/device/identify",
            "/device/network",
            "/device/firmware_abort",
            "/charger/status",
            "/device/info",
        ):
            status, body, headers = self.raw(path, "OPTIONS")
            self.assertEqual(status, 204, path)
            self.assertEqual(body, b"", path)
            self.assertEqual(headers.get("access-control-allow-origin"), "*")
            self.assertEqual(headers.get("access-control-allow-methods"), "GET, POST, OPTIONS")
            self.assertEqual(
                headers.get("access-control-allow-headers"),
                "Content-Type, Content-Length",
            )
        status, _, _ = self.raw("/ws", "OPTIONS")
        self.assertNotEqual(status, 204)

    def test_websocket_device_info_always_in_data(self):
        """Every battery_status data object includes deviceInfo (json_content.c)."""
        ws_url = self.base.replace("http", "ws") + "/api/v2/ws"
        with connect(ws_url) as ws:
            first = json.loads(ws.recv(timeout=5))
            self.assertIn("deviceInfo", first["data"])
            self.assertEqual(first["data"]["deviceInfo"].keys() & INFO_KEYS, INFO_KEYS)
            self.call("/charger/insert/1?type=KLVR-AA", "POST")
            self.addCleanup(self._eject, 1)
            second = json.loads(ws.recv(timeout=5))
            self.assertEqual(second["event"], "battery_status")
            self.assertIn("deviceInfo", second["data"])
            self.assertEqual(second["data"]["deviceInfo"].keys() & INFO_KEYS, INFO_KEYS)

    def test_set_cold_and_set_error_clear_time_remaining(self):
        """charger_map_api_slot reports time only while charging; cold/error clear it."""
        self.call("/charger/insert/2?type=KLVR-AA", "POST")
        self.addCleanup(self._eject, 2)
        charging = self.call("/charger/status")[1]["batteries"][2]
        self.assertGreater(charging["timeRemainingSeconds"], 0)

        self.call("/charger/set_cold/2", "POST")
        cold = self.call("/charger/status")[1]["batteries"][2]
        self.assertEqual(cold["slotState"], "cold")
        self.assertEqual(cold["errorMsg"], "cold")
        self.assertEqual(cold["timeRemainingSeconds"], 0)

        self.call("/charger/insert/2?type=KLVR-AA", "POST")
        self.call("/charger/set_error/2?type=overtemp", "POST")
        err = self.call("/charger/status")[1]["batteries"][2]
        self.assertEqual(err["slotState"], "error")
        self.assertEqual(err["errorMsg"], "overtemp")
        self.assertEqual(err["timeRemainingSeconds"], 0)

    def test_set_warm_matches_firmware_temp_warm(self):
        """TEMP_WARM (reserved) is slotState=warm, errorMsg=warm, no time remaining."""
        self.call("/charger/insert/3?type=KLVR-AAA", "POST")
        self.addCleanup(self._eject, 3)
        self.call("/charger/set_warm/3", "POST")
        bay = self.call("/charger/status")[1]["batteries"][3]
        self.assertEqual(bay["slotState"], "warm")
        self.assertEqual(bay["errorMsg"], "warm")
        self.assertEqual(bay["timeRemainingSeconds"], 0)

    def test_insert_accepts_only_spec_battery_types(self):
        """insert type enum is KLVR-AA / KLVR-AAA (Battery.batteryDetected)."""
        self.addCleanup(self._eject, 4)
        self.assertEqual(self.call("/charger/insert/4?type=KLVR-AA", "POST")[0], 200)
        self.assertEqual(self.call("/charger/status")[1]["batteries"][4]["batteryDetected"], "KLVR-AA")
        self.assertEqual(self.call("/charger/insert/4?type=NiMH", "POST")[0], 400)
        self.assertEqual(self.call("/charger/insert/4?type=", "POST")[0], 400)
        self.assertEqual(self.call("/charger/status")[1]["batteries"][4]["batteryDetected"], "KLVR-AA")

    def test_insert_and_eject_reject_out_of_range_slots(self):
        """insert/eject 400 on slot >= 48 or negative, like set_error (#4 comment)."""
        self.assertEqual(self.call("/charger/insert/48?type=KLVR-AA", "POST")[0], 400)
        self.assertEqual(self.call("/charger/eject/48", "POST")[0], 400)
        self.assertEqual(self.call("/charger/insert/-1?type=KLVR-AA", "POST")[0], 400)
        self.assertEqual(self.call("/charger/eject/-1", "POST")[0], 400)

    def test_mdns_txt_carries_ws_and_api(self):
        """network.c klvr_dns_txt is only ws=1 and api=v2."""
        self.assertEqual(main.MDNS_TXT, {"ws": "1", "api": "v2"})
        info = ServiceInfo(
            type_="_klvrcharger._tcp.local.",
            name="klvr._klvrcharger._tcp.local.",
            addresses=[socket.inet_aton("127.0.0.1")],
            port=8000,
            properties=main.MDNS_TXT,
        )
        decoded = {
            (k.decode() if isinstance(k, bytes) else k): (
                v.decode() if isinstance(v, bytes) else v
            )
            for k, v in info.properties.items()
        }
        self.assertEqual(decoded, {"ws": "1", "api": "v2"})

    def test_web_ui_reads_bay_index(self):
        """Wells are keyed by firmware bay index, not a renamed slot field."""
        html = Path("templates/index.html").read_text()
        self.assertIn("dataset.bay", html)
        self.assertIn("s.batteries[i]", html)
        self.assertNotIn("const s = b.slot", html)

    def test_device_name_multibyte_fitting_at_byte_19_is_kept(self):
        """device_name_from_json: a multibyte char ending at byte 19 is kept."""
        saved = main.charger_state["name"]
        self.addCleanup(self._restore_name, saved)
        status, _, _ = self.raw("/device/name", "POST", {"name": "A" * 17 + "øZ"})
        self.assertEqual(status, 200)
        self.assertEqual(self.raw("/device/name")[1], ("A" * 17 + "ø").encode())

    def test_device_name_empty_is_invalid(self):
        """firmware #180 (f5b9272): empty name is 400 Invalid name."""
        saved = main.charger_state["name"]
        self.addCleanup(self._restore_name, saved)
        status, body, _ = self.raw("/device/name", "POST", {"name": ""})
        self.assertEqual(status, 400)
        self.assertEqual(body, b"Invalid name")
        self.assertEqual(self.raw("/device/name")[1], saved.encode())

    def test_non_utf8_json_body_returns_400_not_500(self):
        """http_server.c: non-UTF-8 body is 400 Invalid JSON object given, not 500."""
        for path in ("/device/name", "/device/network"):
            status, body, _ = self.raw(path, "POST", b'{"name":"\xff"}', "application/json")
            self.assertEqual(status, 400, path)
            self.assertEqual(body, b"Invalid JSON object given", path)

    def test_network_non_string_fields_and_sscanf_ipv4(self):
        """network_settings_body_descr is JSON_TOK_STRING; parse_ip_address is %u then %d."""
        saved = dict(main.charger_state["network"])
        self.addCleanup(self._restore_network, saved)
        for payload in (
            {"type": "dhcp", "ipAddress": 123},
            {"type": "static", "ipAddress": 123, "gatewayAddress": "10.0.0.1", "mask": "255.255.255.0"},
            {"type": 123, "ipAddress": "10.0.0.2", "gatewayAddress": "10.0.0.1", "mask": "255.255.255.0"},
        ):
            status, body, _ = self.raw("/device/network", "POST", payload)
            self.assertEqual(status, 400, payload)
            self.assertEqual(body, b"Invalid JSON object given", payload)

        base_static = {
            "type": "static",
            "ipAddress": "192.168.1.10",
            "gatewayAddress": "192.168.1.1",
            "mask": "255.255.255.0",
        }
        for bad_ip in ("192.168.1.-0", "192.168.1.+1", "192.168.1.1 ", "192.168.1. 1"):
            status, body, _ = self.raw("/device/network", "POST", {**base_static, "ipAddress": bad_ip})
            self.assertEqual(status, 400, bad_ip)
            self.assertEqual(body, b"Invalid address given", bad_ip)

        status, _, _ = self.raw("/device/network", "POST", {**base_static, "ipAddress": "192.168.001.002"})
        self.assertEqual(status, 200)
        self.assertEqual(self.call("/device/network")[1]["ipAddress"], "192.168.1.2")

    def test_options_on_unknown_path_is_404(self):
        """Firmware OPTIONS is per-resource; unknown /api/v2/* is Zephyr 404."""
        status, _, _ = self.raw("/does_not_exist", "OPTIONS")
        self.assertEqual(status, 404)

    def test_set_charge_100_clears_error_msg(self):
        """done has empty errorMsg (openapi Battery.errorMsg)."""
        self.addCleanup(self._eject, 5)
        self.call("/charger/insert/5?type=KLVR-AA", "POST")
        self.call("/charger/set_cold/5", "POST")
        self.raw("/charger/set_charge/5", "POST", {"percentage": 100})
        bay = self.call("/charger/status")[1]["batteries"][5]
        self.assertEqual(bay["slotState"], "done")
        self.assertEqual(bay["errorMsg"], "")

    def test_set_temp_or_error_on_empty_slot_is_400(self):
        """set_cold / set_warm / set_error on an empty bay is invalid, like a bad slot."""
        self.call("/charger/eject/6", "POST")
        self.assertEqual(self.call("/charger/set_cold/6", "POST")[0], 400)
        self.assertEqual(self.call("/charger/set_warm/6", "POST")[0], 400)
        self.assertEqual(self.call("/charger/set_error/6?type=overtemp", "POST")[0], 400)
        self.assertEqual(self.call("/charger/status")[1]["batteries"][6]["slotState"], "empty")

    def test_reboot_invalid_board_and_firmware_abort_clears_target(self):
        """handle_reboot 400 Expected 'rear' or 'main'; abort clears target_version."""
        status, body, _ = self.raw("/device/reboot", "POST", "invalid", "text/plain")
        self.assertEqual(status, 400)
        self.assertEqual(body, b"Expected 'rear' or 'main'")

        saved = dict(main.firmware_state)
        self.addCleanup(main.firmware_state.update, saved)
        main.firmware_state["target_version"] = "9.9.9"
        main.firmware_state["main_firmware_pending"] = True
        self.raw("/device/firmware_abort", "POST")
        self.assertIsNone(main.firmware_state["target_version"])
        self.assertFalse(main.firmware_state["main_firmware_pending"])

    def test_set_charge_on_empty_slot_is_400(self):
        """Empty bay cannot become done; firmware done is a seated cell (charger_map_api_slot)."""
        self.call("/charger/eject/7", "POST")
        status, _, _ = self.raw("/charger/set_charge/7", "POST", {"percentage": 100})
        self.assertEqual(status, 400)
        bay = self.call("/charger/status")[1]["batteries"][7]
        self.assertEqual(bay["slotState"], "empty")
        self.assertEqual(bay["batteryDetected"], "")
        self.assertEqual(self.call("/charger/set_cold/7", "POST")[0], 400)

    def test_firmware_abort_clears_uploaded_sizes(self):
        """ota_cancel_in_progress zeros the transfer counters so a later reboot cannot finish OTA."""
        saved = dict(main.firmware_state)
        self.addCleanup(main.firmware_state.update, saved)
        main.firmware_state.update(
            main_firmware_pending=True,
            rear_firmware_pending=True,
            main_firmware_size=100,
            rear_firmware_size=200,
            target_version="9.9.9",
        )
        self.raw("/device/firmware_abort", "POST")
        self.assertEqual(main.firmware_state["main_firmware_size"], 0)
        self.assertEqual(main.firmware_state["rear_firmware_size"], 0)
        self.assertFalse(main.firmware_state["main_firmware_pending"])
        self.assertFalse(main.firmware_state["rear_firmware_pending"])
        self.assertIsNone(main.firmware_state["target_version"])

    def test_status_simulates_starting_and_charger_unreachable(self):
        """GET /charger/status uses the firmware rule; bays keep their last value."""
        saved = dict(main.charger_frame)
        self.addCleanup(main.charger_frame.update, saved)
        self.call("/charger/insert/0?type=KLVR-AA", "POST")
        self.addCleanup(self._eject, 0)

        self.assertEqual(self.call("/charger/set_frame?seen=false", "POST")[0], 200)
        starting = self.call("/charger/status")[1]
        self.assertEqual(starting["deviceStatus"], "starting")

        timeout = main.COMM_TIMEOUT_MS
        self.assertEqual(self.call(f"/charger/set_frame?seen=true&age_ms={timeout}", "POST")[0], 200)
        self.assertEqual(self.call("/charger/status")[1]["deviceStatus"], "ok")

        self.assertEqual(self.call(f"/charger/set_frame?seen=true&age_ms={timeout + 1}", "POST")[0], 200)
        lost = self.call("/charger/status")[1]
        self.assertEqual(lost["deviceStatus"], "charger_unreachable")
        self.assertEqual(lost["batteries"][0]["batteryDetected"], "KLVR-AA")
        self.assertEqual(lost["batteries"][0]["slotState"], "charging")

    def test_status_and_ws_totals_from_verdict_mixed_rack(self):
        """C7: HTTP and WS totals come from the firmware snapshot tick."""
        self.call("/charger/bulk_clear", "POST")
        self.call("/charger/insert/0?type=KLVR-AA", "POST")
        self.addCleanup(self._eject, 0)
        self.call("/charger/insert/1?type=KLVR-AA", "POST")
        self.addCleanup(self._eject, 1)
        self.raw("/charger/set_charge/1", "POST", {"percentage": 100})

        status = self.call("/charger/status")[1]
        self.assertIn("totals", status)
        self.assertIn("etaSeconds", status)
        self.assertEqual(status["totals"]["AA"]["occupied"], 2)
        self.assertEqual(status["totals"]["AA"]["done"], 1)
        self.assertNotIn("ready", status["totals"]["AA"])

        with connect(self.base.replace("http", "ws") + "/api/v2/ws") as ws:
            msg = json.loads(ws.recv(timeout=5))
        self.assertEqual(msg["data"]["totals"], status["totals"])
        self.assertEqual(msg["data"]["etaSeconds"], status["etaSeconds"])


class DeviceStatus(unittest.TestCase):
    """C16, C18, Q2: same rule as charger_map_device_status() (firmware #205)."""

    def test_device_status(self):
        timeout = main.COMM_TIMEOUT_MS
        self.assertEqual(timeout, 5000)

        self.assertEqual(main.map_device_status(False, 0), "starting")
        self.assertEqual(main.map_device_status(False, 1), "starting")
        self.assertEqual(main.map_device_status(False, timeout), "starting")
        self.assertEqual(main.map_device_status(False, timeout + 1), "starting")

        self.assertEqual(main.map_device_status(True, 0), "ok")
        self.assertEqual(main.map_device_status(True, timeout - 1), "ok")
        self.assertEqual(main.map_device_status(True, timeout), "ok")
        self.assertEqual(main.map_device_status(True, timeout + 1), "charger_unreachable")
        self.assertEqual(main.map_device_status(True, 60 * 1000), "charger_unreachable")


class VerdictTotals(unittest.TestCase):
    """C7: totals come from the firmware snapshot, not a Python encoder."""

    def test_snapshot_totals_have_done_not_ready(self):
        self.assertTrue(main.twin.available)
        snap = main.twin.clear_all()
        main.twin.seat(0, 2, 40, 24, 3600)
        main.twin.seat(1, 2, 100, 24, 0)
        snap = main.twin.tick()
        self.assertEqual(snap["totals"]["AA"]["occupied"], 2)
        self.assertEqual(snap["totals"]["AA"]["done"], 1)
        self.assertNotIn("ready", snap["totals"]["AA"])
        self.assertIn("etaSeconds", snap)

    def test_status_without_c7_keys_is_still_valid(self):
        """1.9.0 chargers omit totals / etaSeconds; those keys are not required."""
        payload = {"deviceStatus": "ok", "batteries": []}
        self.assertTrue(main.STATUS_REQUIRED_KEYS <= payload.keys())
        self.assertFalse(main.STATUS_REQUIRED_KEYS & {"totals", "etaSeconds"})


if __name__ == "__main__":
    unittest.main()
