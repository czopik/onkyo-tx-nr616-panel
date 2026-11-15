#!/usr/bin/env python3
"""
onkyo_web.py - pelny panel WWW do sterowania amplitunerem Onkyo TX-NR616
(eISCP na porcie 60128 + UPnP/DLNA DMR do "castowania" audio).

Uruchomienie:
    python onkyo_web.py --media "C:\\Music"
    # -> http://127.0.0.1:8770/

Wymaga pliku onkyo_cast.py w tym samym folderze (wspolne API eISCP/DLNA).
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import mimetypes
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from onkyo_cast import (  # noqa: E402
    DEFAULT_HOST,
    EISCP_PORT,
    UPNP_PORT,
    Eiscp,
    MIME,
    Renderer,
    extract,
    local_ip_towards,
    push_file,
    start_http_server,
)

HTTP_MEDIA_PORT = 8765          # serwer plikow dla DLNA
HTTP_UI_PORT = 8770             # panel WWW
MEDIA_ROOT = os.getcwd()

# --------------------------------------------------------------- katalogi ---
INPUTS = {
    "10": "BD/DVD",
    "01": "CBL/SAT",
    "00": "STB/DVR",
    "02": "GAME",
    "05": "PC",
    "03": "AUX",
    "25": "AM",
    "24": "FM",
    "23": "TV/CD",
    "2B": "NET",
    "29": "USB",
}

MODES = {
    "00": "Stereo", "01": "Direct", "02": "Surround", "03": "Film", "04": "THX",
    "05": "Action", "06": "Musical", "07": "Mono Movie", "08": "Orchestra",
    "09": "Unplugged", "0A": "Studio-Mix", "0B": "TV Logic", "0C": "All Ch Stereo",
    "0D": "Theater-Dimensional", "0E": "Enhanced 7", "0F": "Mono", "11": "Pure Audio",
    "12": "Multiplex", "13": "Full Mono", "14": "Dolby Virtual", "15": "DTS Surround Sensation",
    "16": "Audyssey DSX", "1F": "Whole House", "40": "Straight Decode", "41": "Dolby EX",
    "42": "THX Cinema", "43": "THX Surround EX", "44": "THX Music", "45": "THX Games",
    "50": "THX Cinema2", "51": "THX MusicMode", "52": "THX Games Mode",
    "80": "PLII/PLIIx Movie", "81": "PLII/PLIIx Music", "82": "Neo:6 Cinema",
    "83": "Neo:6 Music", "84": "PLIIx THX Cinema", "85": "Neo:6 THX Cinema",
    "86": "PLIIx Game", "87": "Neural Surround", "88": "Neural THX",
    "89": "PLIIx THX Games", "8A": "Neo:6 THX Games", "8B": "PLIIx THX Music",
    "8C": "Neo:6 THX Music", "8D": "Neural THX Cinema", "8E": "Neural THX Music",
    "8F": "Neural THX Games", "90": "PLIIz Height", "91": "Neo:6 Cinema DSS",
    "92": "Neo:6 Music DSS", "93": "Neural Digital Music", "94": "PLIIz THX Cinema",
    "95": "PLIIz THX Music", "96": "PLIIz THX Games", "97": "PLIIz THX U2 Cinema",
    "98": "PLIIz THX U2 Music", "99": "PLIIz THX U2 Games", "9A": "Neo:X Game",
    "A0": "PLIIx Movie + DSX", "A1": "PLIIx Music + DSX", "A2": "PLIIx Game + DSX",
    "A3": "Neo:6 Cinema + DSX", "A4": "Neo:6 Music + DSX", "A5": "Neural + DSX",
    "A6": "Neural Digital Music + DSX", "A7": "Dolby EX + DSX",
}

HDMI_OUT = {
    "00": "Brak (analogowo)", "01": "Main", "02": "Sub", "03": "Main+Sub",
    "04": "Both (Main)", "05": "Both (Sub)",
}

DIMMER = {"00": "Bright", "01": "Dim", "02": "Dark", "03": "Shut-off", "08": "Bright + LED off"}

SLEEP_CHOICES = [("OFF", "Wyłączony"), ("1E", "30 min"), ("3C", "60 min"), ("5A", "90 min")]
SLEEP_LABELS = {"OFF": "Wyłączony", "1E": "30 min", "3C": "60 min", "5A": "90 min",
                "00": "Wyłączony", "30": "48 min", "60": "96 min"}

# kody odpytywane cyklicznie (tylko te, ktore ten egzemplarz obsluguje)
QUERY_CODES = [
    "PWR", "AMT", "MVL", "SLI", "LMD", "RAS", "HDO", "DIM", "SLP", "DIF",
    "TFR", "SWL", "CTL", "TUN", "PRS",
    "ZPW", "ZVL", "ZMT", "SLZ", "PRZ", "TUZ",
    "PW3", "VL3", "MT3", "SL3",
]

TONE_SLIDER = range(-10, 11)          # zakres bas/treble w dB


def _signed_format(value, low, high):
    """Format eISCP dla tonow/poziomow: '00', '+4', '-6' (cyfra hex, A=10)."""
    value = max(low, min(high, int(value)))
    if value == 0:
        return "00"
    return ("+" if value > 0 else "-") + f"{abs(value):X}"


def _signed_parse(text):
    if not text:
        return None
    text = text.strip()
    if text == "00":
        return 0
    if len(text) == 2 and text[0] in "+-":
        try:
            value = int(text[1], 16)
        except ValueError:
            return None
        return value if text[0] == "+" else -value
    return None


def tone_format(value):
    """Bas/treble: -10..+10 dB."""
    return _signed_format(value, -10, 10)


def level_format(value):
    """Sub/center: -15..+12 dB."""
    return _signed_format(value, -15, 12)


# ----------------------------------------------------------- config --------
CONFIG_PATH = os.environ.get("ONKYO_CONFIG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(config):
    try:
        parent = os.path.dirname(os.path.abspath(CONFIG_PATH))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=1)
    except OSError:
        pass


# --------------------------------------------------------- discovery ------
def _is_onkyo(info):
    manufacturer = (info.get("manufacturer") or "").upper()
    model = (info.get("model") or "").upper()
    return "ONKYO" in manufacturer or model.startswith("TX-")


def _describe(ip):
    info = {"ip": ip}
    try:
        with urllib.request.urlopen(
                f"http://{ip}:{UPNP_PORT}/upnp_descriptor_0", timeout=1.5) as response:
            xml = response.read().decode("utf-8", "replace")
        info.update({
            "model": extract(xml, "modelName"),
            "friendly": extract(xml, "friendlyName"),
            "manufacturer": extract(xml, "manufacturer"),
        })
        if _is_onkyo(info):
            return info
    except Exception:  # noqa: BLE001
        pass
    try:
        with urllib.request.urlopen(f"http://{ip}/info.cgi", timeout=1.5) as response:
            page = response.read().decode("utf-8", "replace")
        match = re.search(r"Machine Name\s*</td>\s*<td[^>]*>([^<]+)<", page)
        if match and "TX-" in match.group(1):
            name = match.group(1).strip()
            info.update({"model": name, "friendly": name, "manufacturer": "ONKYO"})
            return info
    except Exception:  # noqa: BLE001
        pass
    return None


def _ssdp_candidates(timeout=1.6):
    message = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
               'MAN: "ssdp:discover"\r\nMX: 1\r\nST: ssdp:all\r\n\r\n').encode("ascii")
    found = {}
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.4)
        for target in ("239.255.255.250", "255.255.255.255"):
            try:
                sock.sendto(message, (target, 1900))
            except OSError:
                pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("ascii", "replace")
            match = re.search(r"LOCATION:\s*(\S+)", text, re.I)
            if match:
                host = urllib.parse.urlparse(match.group(1)).hostname
                if host:
                    found.setdefault(host, {"ip": host})
    except OSError:
        pass
    finally:
        if sock is not None:
            sock.close()
    return found


def _scan_eiscp(host_hint, timeout=0.25):
    try:
        local = local_ip_towards(host_hint or DEFAULT_HOST)
        network = ipaddress.ip_network(f"{local}/24", strict=False)
    except Exception:  # noqa: BLE001
        return []
    addresses = [str(address) for address in network.hosts()]

    def probe(address):
        try:
            sock = socket.create_connection((address, EISCP_PORT), timeout)
            sock.close()
            return address
        except OSError:
            return None

    found = []
    with ThreadPoolExecutor(max_workers=96) as pool:
        for result in pool.map(probe, addresses):
            if result:
                found.append(result)
    return found


def discover_devices(host_hint):
    devices = []
    seen = set()
    candidates = list(_ssdp_candidates().keys())
    if not candidates:
        candidates = _scan_eiscp(host_hint)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for info in pool.map(_describe, candidates[:40]):
            if info and info["ip"] not in seen:
                seen.add(info["ip"])
                devices.append(info)
    return devices


FAST_CODES = ["PWR", "MVL", "AMT", "SLI", "LMD"]
FULL_CODES = [code for code in QUERY_CODES if code not in FAST_CODES]
PROTECTED_FIELDS = ("power", "volume", "mute", "input", "mode")

def optimistic_fields(command):
    """Pola stanu, ktore mozna ustawic natychmiast po wyslaniu komendy."""
    code, text = command[:3], command[3:]
    fields = {}
    if code == "PWR" and command in ("PWR01", "PWR00"):
        fields["power"] = text
    elif command == "PWRALL":
        fields["power"] = "00"
    elif code == "AMT" and command in ("AMT00", "AMT01"):
        fields["mute"] = text
    elif code == "MVL" and re.fullmatch(r"[0-9A-F]{2}", text):
        fields["volume"] = int(text, 16)
    elif code == "SLI" and re.fullmatch(r"[0-9A-F]{2}", text):
        fields["input"] = text
    elif code == "LMD" and re.fullmatch(r"[0-9A-F]{2}", text):
        fields["mode"] = text
    elif code == "RAS" and text in ("00", "01"):
        fields["cinema_filter"] = text
    elif code == "HDO" and re.fullmatch(r"[0-9A-F]{2}", text):
        fields["hdmi_out"] = text
    elif code == "DIM" and re.fullmatch(r"[0-9A-F]{2}", text):
        fields["dimmer"] = text
    elif code == "SLP" and (text == "OFF" or re.fullmatch(r"[0-9A-F]{2}", text)):
        fields["sleep"] = "00" if text == "OFF" else text
    elif code == "DIF" and re.fullmatch(r"[0-9A-F]{2}", text):
        fields["display_mode"] = text
    elif code == "TFR" and len(text) == 3 and text[0] == "B":
        value = _signed_parse(text[1:3])
        if value is not None:
            fields["bass"] = value
    elif code == "TFR" and len(text) == 3 and text[0] == "T":
        value = _signed_parse(text[1:3])
        if value is not None:
            fields["treble"] = value
    elif code == "SWL":
        value = _signed_parse(text)
        if value is not None:
            fields["sub_level"] = value
    elif code == "CTL":
        value = _signed_parse(text)
        if value is not None:
            fields["center_level"] = value
    return fields


# -------------------------------------------------------- eISCP live link ---
class LiveEiscp:
    """Jedno stale polaczenie eISCP, wspoldzielone przez UI i poller."""

    def __init__(self, host, port=EISCP_PORT, timeout=2.5):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = None
        self.buffer = b""
        self.lock = threading.RLock()

    # -- niskopoziomowe -------------------------------------------------
    def _packet(self, command):
        text = command if command.startswith("!") else "!1" + command
        payload = text.encode("ascii") + b"\r\n"
        return (b"ISCP" + struct.pack(">I", 16) + struct.pack(">I", len(payload))
                + b"\x01\x00\x00\x00" + b"\x00\x00\x00\x1d" + payload)

    def _connect(self):
        if self.sock is not None:
            return
        sock = socket.create_connection((self.host, self.port), 3.0)
        try:
            sock.settimeout(0.35)
        except OSError:
            sock.close()
            raise
        self.sock = sock
        self.buffer = b""

    def close(self):
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
            self.buffer = b""

    def _drain(self):
        messages = []
        while True:
            start = self.buffer.find(b"ISCP")
            if start < 0 or len(self.buffer) < start + 16:
                break
            size = struct.unpack(">I", self.buffer[start + 8:start + 12])[0]
            end = start + 16 + size
            if len(self.buffer) < end:
                break
            payload = self.buffer[start + 16:end].decode("ascii", "replace")
            self.buffer = self.buffer[end:]
            for line in payload.split("\r\n"):
                line = line.strip("\x1a\r\n ")
                if line.startswith("!1"):
                    line = line[2:]
                if line:
                    messages.append(line)
        return messages

    def _read(self, seconds):
        """Czytaj przez 'seconds' i zwroc liste komunikatow (bez prefiksu !1)."""
        messages = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                self.close()
                break
            if not chunk:
                self.close()
                break
            self.buffer += chunk
            messages.extend(self._drain())
        return messages

    def listen(self, seconds=0.5):
        """Pasywnie nasluchuje broadcastu eISCP z amplitunera."""
        with self.lock:
            self._connect()
            if self.sock is None:
                return []
            return self._read(seconds)

    # -- API -------------------------------------------------------------
    def batch(self, codes):
        """Odpytaj wiele kodow QSTN; konczy gdy tylko ma odpowiedzi."""
        with self.lock:
            self._connect()
            if self.sock is None:
                return {}
            result = {}
            pending = list(codes)
            for _attempt in range(2):
                try:
                    for code in pending:
                        self.sock.sendall(self._packet(code + "QSTN"))
                        time.sleep(0.04)
                except OSError:
                    self.close()
                    return result
                deadline = time.time() + 0.7
                while pending and time.time() < deadline:
                    try:
                        chunk = self.sock.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        self.close()
                        return result
                    if not chunk:
                        self.close()
                        return result
                    self.buffer += chunk
                    for message in self._drain():
                        for code in list(pending):
                            if message.startswith(code):
                                result[code] = message[len(code):]
                                pending.remove(code)
                if not pending:
                    break
            return result

    def send(self, command, wait=0.5):
        """Wyslij komende (np. 'MVL32') i zwroc liste odpowiedzi."""
        with self.lock:
            self._connect()
            if self.sock is None:
                raise RuntimeError("brak polaczenia z amplitunerem")
            try:
                self.sock.sendall(self._packet(command))
            except OSError:
                self.close()
                raise
            replies = []
            expected = command[:3]
            started = time.time()
            deadline = started + wait
            while time.time() < deadline:
                try:
                    chunk = self.sock.recv(4096)
                except socket.timeout:
                    if replies:
                        break
                    continue
                except OSError:
                    self.close()
                    break
                if not chunk:
                    self.close()
                    break
                self.buffer += chunk
                batch = self._drain()
                replies.extend(batch)
                if any(item.startswith(expected) for item in batch):
                    break
                if replies and time.time() - started > 0.2:
                    break
            return replies


# ---------------------------------------------------------------- state -----
class StateStore:
    def __init__(self, host, media_root, config_path=None):
        self.host = host
        self.media_root = media_root
        self.config_path = config_path or CONFIG_PATH
        self.link = LiveEiscp(host)
        self.cmd_link = LiveEiscp(host)
        self.lock = threading.RLock()
        self.data = {"ok": False, "online": False, "host": host, "ts": time.time(),
                     "last_seen": 0, "cast": {}, "files": []}
        self.renderer = None
        self.last_upnp = 0
        self.last_seen = 0
        self.failures = 0
        self.recent = {}
        self.cycle = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _renderer(self):
        with self.lock:
            if self.renderer is None:
                self.renderer = Renderer(self.host)
            return self.renderer

    def set_host(self, host):
        host = host.strip()
        if not re.fullmatch(r"[0-9A-Za-z\.\-]{3,64}", host):
            raise RuntimeError("nieprawidlowy adres")
        files_list = self._files()
        with self.lock:
            self.host = host
            self.link.close()
            self.link = LiveEiscp(host)
            self.cmd_link.close()
            self.cmd_link = LiveEiscp(host)
            self.renderer = None
            self.recent.clear()
            self.last_seen = 0
            self.failures = 0
            self.last_upnp = 0
            self.cycle = 0
            self.data = {"ok": False, "online": False, "host": host, "ts": time.time(),
                         "last_seen": 0, "cast": {}, "files": files_list}
        save_config({"host": host})

    def _apply(self, state, values):
        mapping = {"PWR": "power", "AMT": "mute", "SLI": "input", "LMD": "mode",
                   "RAS": "cinema_filter", "HDO": "hdmi_out", "DIM": "dimmer",
                   "SLP": "sleep", "DIF": "display_mode", "TUN": "tuner", "PRS": "preset"}
        for code, field in mapping.items():
            if code in values:
                value = values[code]
                if code == "LMD" and value == "N/A":
                    value = None
                state[field] = value
        if "MVL" in values and re.fullmatch(r"[0-9A-F]{2}", values["MVL"] or ""):
            state["volume"] = int(values["MVL"], 16)
        if "TFR" in values and len(values["TFR"] or "") == 6:
            state["bass"] = _signed_parse(values["TFR"][1:3])
            state["treble"] = _signed_parse(values["TFR"][4:6])
        if "SWL" in values:
            state["sub_level"] = _signed_parse(values["SWL"])
        if "CTL" in values:
            state["center_level"] = _signed_parse(values["CTL"])
        z2 = dict(state.get("z2") or {})
        for code, key in (("ZPW", "power"), ("ZVL", "volume"), ("ZMT", "mute"),
                          ("SLZ", "input"), ("TUZ", "tuner"), ("PRZ", "preset")):
            if code in values:
                z2[key] = values[code]
        state["z2"] = z2
        z3 = dict(state.get("z3") or {})
        for code, key in (("PW3", "power"), ("VL3", "volume"),
                          ("MT3", "mute"), ("SL3", "input")):
            if code in values:
                z3[key] = values[code]
        state["z3"] = z3

    def _files(self):
        try:
            found = []
            for root_dir, _, filenames in os.walk(self.media_root):
                for fname in filenames:
                    if os.path.splitext(fname)[1].lower() in MIME:
                        rel = os.path.relpath(os.path.join(root_dir, fname), self.media_root)
                        found.append(rel.replace("\\", "/"))
            return sorted(found)
        except OSError:
            return []

    def _build(self):
        start = time.time()
        self.cycle += 1
        values = {}
        if self.cycle == 1 or self.failures > 0 or self.cycle % 15 == 0:
            values.update(self.link.batch(FAST_CODES))
            values.update(self.link.batch(FULL_CODES))
        elif self.cycle % 4 == 0:
            values.update(self.link.batch(FAST_CODES))
        else:
            for msg in self.link.listen(0.5):
                code = msg[:3]
                values[code] = msg[3:]

        online = bool(values) or (self.link.sock is not None and self.failures == 0)
        if online:
            self.failures = 0
        else:
            self.failures = getattr(self, "failures", 0) + 1

        # Operacje I/O (dysk oraz UPnP SOAP) wykonujemy POZA self.lock
        files_list = self._files()
        now = time.time()
        cast_update = None
        if now - self.last_upnp > 5:
            self.last_upnp = now
            if online:
                try:
                    renderer = self._renderer()
                    transport, _ = renderer.transport_info()
                    position = renderer.position()
                    cast_update = {
                        "transport": transport,
                        "rel": position["rel"],
                        "duration": position["duration"],
                        "uri": position["uri"],
                    }
                except Exception:  # noqa: BLE001
                    cast_update = {"transport": "?"}
            else:
                cast_update = {"transport": "OFFLINE"}

        with self.lock:
            state = dict(self.data)
            state["host"] = self.host
            state["ts"] = now
            # pokazujemy OFFLINE dopiero po 3 nieudanych cyklach (~2 s), zeby nie migalo
            state["online"] = online or self.failures < 3
            state["ok"] = state["online"]
            if online:
                self.last_seen = now
            state["last_seen"] = self.last_seen
            self._apply(state, values)
            if "IFA" in values:
                state["audio"] = None if values["IFA"] == "N/A" else values["IFA"]
            for field, (value, stamp) in list(self.recent.items()):
                if stamp > start:
                    state[field] = value
                if now - stamp > 6:
                    self.recent.pop(field, None)
            state["files"] = files_list
            if cast_update is not None:
                state["cast"] = cast_update
            else:
                state["cast"] = state.get("cast") or {}
            return state

    def update_cast(self, info):
        with self.lock:
            state = dict(self.data)
            state["cast"] = info
            self.data = state

    def patch_fields(self, fields):
        """Natychmiast ustawia pola stanu na podstawie wyslanej komendy."""
        if not fields:
            return
        with self.lock:
            state = dict(self.data)
            stamp = time.time()
            for field, value in fields.items():
                state[field] = value
                if field in PROTECTED_FIELDS:
                    self.recent[field] = (value, stamp)
            state["online"] = True
            state["ok"] = True
            state["ts"] = stamp
            state["last_seen"] = stamp
            self.last_seen = stamp
            self.data = state

    def patch(self, messages):
        """Natychmiast aktualizuje cache na podstawie echo odpowiedzi urzadzenia."""
        if not messages:
            return
        values = {}
        for message in messages:
            code = message[:3]
            if code not in values:
                values[code] = message[3:]
        if not values:
            return
        with self.lock:
            state = dict(self.data)
            before = {field: state.get(field) for field in PROTECTED_FIELDS}
            self._apply(state, values)
            if "IFA" in values:
                state["audio"] = None if values["IFA"] == "N/A" else values["IFA"]
            stamp = time.time()
            for field in PROTECTED_FIELDS:
                if state.get(field) != before.get(field):
                    self.recent[field] = (state[field], stamp)
            state["online"] = True
            state["ok"] = True
            state["ts"] = stamp
            state["last_seen"] = stamp
            self.last_seen = stamp
            self.data = state

    def _loop(self):
        while True:
            try:
                state = self._build()
                with self.lock:
                    self.data = state
            except Exception:  # noqa: BLE001
                files_list = self._files()
                with self.lock:
                    self.data = {"ok": False, "online": False, "host": self.host,
                                 "ts": time.time(), "last_seen": self.last_seen,
                                 "cast": {}, "files": files_list}
            time.sleep(0.4)

    def snapshot(self):
        with self.lock:
            return dict(self.data)


STATE = None


# ----------------------------------------------------------------- HTTP -----
class Handler(BaseHTTPRequestHandler):
    server_version = "onkyo-web/1.1"

    def _send(self, body, content_type="application/json; charset=utf-8", code=200):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, payload, code=200):
        self._send(json.dumps(payload, ensure_ascii=False),
                   "application/json; charset=utf-8", code)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(HTML, "text/html; charset=utf-8")
        elif parsed.path == "/api/state":
            self._json(STATE.snapshot())
        else:
            self._json({"ok": False, "error": "unknown endpoint"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else "{}"
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            payload = {}
        try:
            result = self._dispatch(parsed.path, payload)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": str(exc)}, 500)
            return
        self._json(result)

    def _dispatch(self, path, payload):
        if path == "/api/command":
            command = str(payload.get("cmd", "")).strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{3}[0-9A-Za-z+\-]{0,10}", command):
                raise RuntimeError("nieprawidlowa komenda")
            try:
                replies = STATE.cmd_link.send(command)
            except OSError as exc:
                hint = ""
                if command.startswith("PWR") and command != "PWRQSTN":
                    hint = (" Amplituner jest w standby bez aktywnej sieci — włącz go pilotem i ustaw "
                            "Setup > Network > Network Standby > On, żeby panel mógł go obudzić.")
                raise RuntimeError(f"brak łączności z amplitunerem.{hint}") from exc
            STATE.patch(replies)
            if not replies:
                raise RuntimeError("amplituner nie odpowiedział (offline?)")
            fields = optimistic_fields(command)
            if fields:
                STATE.patch_fields(fields)
            else:
                echo = [item for item in replies if item == command]
                if echo:
                    STATE.patch(echo)
            return {"ok": True, "command": command, "replies": replies[-4:]}
        if path == "/api/host":
            host = str(payload.get("host", "")).strip()
            if not host:
                raise RuntimeError("podaj adres IP")
            STATE.set_host(host)
            return {"ok": True, "host": host}
        if path == "/api/discover":
            devices = discover_devices(STATE.host)
            return {"ok": True, "devices": devices, "current": STATE.host}
        if path == "/api/cast":
            name = payload.get("file")
            url = payload.get("url")
            try:
                renderer = STATE._renderer()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"amplituner nieosiągalny ({exc})") from exc
            if url:
                renderer.set_uri(url, url.rsplit("/", 1)[-1] or "stream", "audio/mpeg")
                renderer.play(1)
                target = url
            elif name:
                push_file(STATE.host, STATE.media_root, name, HTTP_MEDIA_PORT)
                target = name
            else:
                raise RuntimeError("brak pliku")
            try:
                transport, _ = renderer.transport_info()
                position = renderer.position()
                STATE.update_cast({"transport": transport, "rel": position["rel"],
                                   "duration": position["duration"], "uri": position["uri"]})
            except Exception:  # noqa: BLE001
                pass
            return {"ok": True, "cast": target}
        if path == "/api/cast/stop":
            try:
                STATE._renderer().stop()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"amplituner nieosiągalny ({exc})") from exc
            STATE.update_cast({"transport": "STOPPED", "rel": "00:00:00", "duration": "", "uri": ""})
            return {"ok": True}
        if path == "/api/cast/refresh":
            state = STATE.snapshot()
            try:
                renderer = STATE._renderer()
                transport, _ = renderer.transport_info()
                position = renderer.position()
                state["cast"] = {"transport": transport, "rel": position["rel"],
                                 "duration": position["duration"], "uri": position["uri"]}
            except Exception:  # noqa: BLE001
                pass
            return state
        raise RuntimeError("unknown endpoint")

    def log_message(self, fmt, *args):
        pass


# ------------------------------------------------------------------ HTML ----
HTML = r"""<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Onkyo TX-NR616 - panel sterowania</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--card2:#1c2430;--line:#2a3441;--tx:#e6edf3;--mut:#8b98a8;--acc:#3fb6f0;--ok:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.45 system-ui,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:5;background:linear-gradient(#0d1117,#0d1117f2);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:14px;flex-wrap:wrap;align-items:center}
h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px}
.chips{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-left:auto}
.chip{background:var(--card2);border:1px solid var(--line);border-radius:20px;padding:3px 11px;font-size:12px;color:var(--mut)}
.chip b{color:var(--tx);font-weight:600}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--ok);margin-right:6px;box-shadow:0 0 6px #3fb950aa}
.dot.off{background:var(--bad);box-shadow:0 0 6px #f85149aa}
#tabs{display:flex;gap:8px;padding:12px 16px 0;max-width:1600px;margin:0 auto}
#tabs button{font-size:14px;padding:9px 18px}
#tabs button.on{background:#123a4d;border-color:var(--acc);color:#c9ecff}
main{padding:14px 16px 24px;display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;max-width:1600px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;display:flex;flex-direction:column;gap:11px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);margin:0;font-weight:650}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row>label{font-size:12px;color:var(--mut);min-width:66px}
button{background:#212b37;color:var(--tx);border:1px solid #334153;border-radius:8px;padding:7px 12px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--acc)}
button.on{background:#123a4d;border-color:var(--acc);color:#c9ecff}
button.ghost{background:transparent}
select,input[type=text],input[type=number]{background:#0f141b;color:var(--tx);border:1px solid #334153;border-radius:8px;padding:7px 9px;font-size:13px;max-width:100%}
input[type=range]{width:100%;accent-color:var(--acc)}
.val{font-variant-numeric:tabular-nums;color:var(--acc);font-weight:600;min-width:64px;text-align:right}
.big{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
.mut{color:var(--mut);font-size:12px}
.ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)}
#sources{display:flex;flex-wrap:wrap;gap:8px}
#sources button{min-width:92px;text-align:center}
#files{display:flex;flex-direction:column;gap:6px;max-height:190px;overflow:auto}
#files .f{display:flex;gap:8px;align-items:center;justify-content:space-between;background:#0f141b;border:1px solid #222c38;border-radius:8px;padding:6px 9px;font-size:12px}
#files .f span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
pre{background:#0a0e13;border:1px solid #222c38;border-radius:8px;padding:10px;font-size:12px;color:#9fd7a8;white-space:pre-wrap;word-break:break-all;margin:0;max-height:160px;overflow:auto}
#devices .d{display:flex;gap:8px;align-items:center;justify-content:space-between;background:#0f141b;border:1px solid #222c38;border-radius:8px;padding:7px 9px;font-size:12px}
#toast{position:fixed;right:16px;bottom:16px;display:flex;flex-direction:column;gap:8px;z-index:50;max-width:420px}
#toast div{background:#2a1b1b;border:1px solid #6b2b2b;color:#ffd7d7;border-radius:8px;padding:9px 12px;font-size:13px;box-shadow:0 4px 16px #0008}
#toast div.info{background:#16242e;border-color:#2b4a5c;color:#cfe9ff}
.tabpage{display:contents}
.tabpage[hidden]{display:none}
.hint{background:#101821;border:1px dashed #2e3c4d;border-radius:8px;padding:9px 11px;font-size:12px;color:#a9bdd3;line-height:1.5}
.hint b{color:#d8e7f7}
</style>
</head>
<body>
<header>
  <h1>Onkyo TX-NR616</h1>
  <div class="chips">
    <span class="chip"><span class="dot off" id="onDot"></span><b id="onTxt">-</b></span>
    <span class="chip">zasilanie <b id="pwrTxt">-</b></span>
    <span class="chip">host <b id="hostChip">-</b></span>
    <span class="chip">wejście <b id="inTxt">-</b></span>
    <span class="chip">tryb <b id="modeTxt">-</b></span>
    <span class="chip">vol <b id="volTxt">-</b></span>
    <span class="chip">cast <b id="castTxt">-</b></span>
  </div>
</header>

<div id="tabs">
  <button data-tab="main" id="tabBtnMain">Główne</button>
  <button data-tab="settings" id="tabBtnSettings">Ustawienia</button>
</div>

<main>
  <!-- ====================== GŁÓWNE ====================== -->
  <div class="tabpage" id="tab-main">
    <section class="card">
      <h2>Strefa główna</h2>
      <div class="row">
        <button id="btnOn">ON</button>
        <button id="btnStandby">STANDBY</button>
        <button id="btnAllOff" class="ghost">All OFF</button>
        <button id="btnMute">Mute</button>
      </div>
      <div class="row"><label>Głośność</label><span class="big" id="volBig">-</span>
        <button id="volDown" class="ghost">−</button><button id="volUp" class="ghost">+</button></div>
      <input type="range" id="volRange" min="0" max="100" step="1">
      <div class="mut" id="displayInfo">-</div>
    </section>

    <section class="card">
      <h2>Źródło</h2>
      <div id="sources"></div>
      <div class="mut">Pokazuję tylko źródła, które ten model ma na panelu przednim.</div>
    </section>

    <section class="card">
      <h2>Teraz gra</h2>
      <div class="mut" id="audioInfo">-</div>
      <div class="row"><label>Tryb</label><select id="selMode"></select></div>
      <div class="row"><button id="btnCinema">Cinema Filter</button></div>
      <div class="row"><span class="mut" id="castInfo">-</span></div>
    </section>

    <section class="card">
      <h2>Cast (DLNA)</h2>
      <div id="files"></div>
      <div class="row"><input type="text" id="castUrl" placeholder="URL strumienia (http...)" style="flex:1">
        <button id="btnCastUrl">Cast URL</button></div>
      <div class="row"><button id="btnCastStop">Stop</button></div>
    </section>
  </div>

  <!-- ====================== USTAWIENIA ====================== -->
  <div class="tabpage" id="tab-settings" hidden>
    <section class="card">
      <h2>Połączenie</h2>
      <div class="row"><span class="dot off" id="connDot"></span><span class="big" id="connStatus">łączenie...</span></div>
      <div class="mut" id="lastSeen">-</div>
      <div class="row">
        <input type="text" id="hostInput" placeholder="adres IP, np. 192.168.1.34" style="flex:1">
        <button id="btnConnect">Połącz</button>
      </div>
      <div class="row">
        <button id="btnDiscover">Szukaj amplitunera w sieci</button>
        <span class="mut" id="discoverInfo"></span>
      </div>
      <div id="devices"></div>
      <div class="mut" id="connHint">Sprawdzam...</div>
    </section>

    <section class="card">
      <h2>Włączanie ze standby</h2>
      <div class="hint">
        Ten model po wejściu w standby <b>wyłącza moduł sieciowy</b>, więc bez tej opcji nie da się
        go obudzić z sieci. Włącz ją raz na amplitunerze (menu na TV):<br>
        <b>SETUP → Network → Network Standby → On</b><br>
        Wtedy panel pokaże <b>STANDBY</b> + <b>ONLINE</b> i przycisk ON zadziała.
        Bez tego pokaże <b>OFFLINE</b> (i trzeba włączyć pilotem).
      </div>
    </section>

    <section class="card">
      <h2>Poziomy głośników</h2>
      <div class="row"><label>Subwoofer</label><input type="range" id="sub" min="-15" max="12" step="1"><span class="val" id="subTxt">-</span></div>
      <div class="row"><label>Center</label><input type="range" id="center" min="-15" max="12" step="1"><span class="val" id="centerTxt">-</span></div>
      <div class="hint">
        <b>Front L/R i surround L/R nie są dostępne przez sieć</b> w tym modelu
        (komendy TCL/FWL/SLL nie odpowiadają — sprawdzone). Ustaw je na amplitunerze:
        <b>SETUP → Speaker Setup → Level Calibration</b> i przełączaj kanał pilotem, albo
        ustaw ręcznie w menu. Subwoofer i center możesz regulować tutaj.
      </div>
    </section>

    <section class="card">
      <h2>Tony</h2>
      <div class="row"><label>Bas</label><input type="range" id="bass" min="-10" max="10" step="1"><span class="val" id="bassTxt">-</span></div>
      <div class="row"><label>Treble</label><input type="range" id="treble" min="-10" max="10" step="1"><span class="val" id="trebleTxt">-</span></div>
      <div class="mut">Bas/treble −10…+10 dB (działa na front).</div>
    </section>

    <section class="card">
      <h2>Timer, wyświetlacz, wyjście HDMI</h2>
      <div class="row"><label>Sleep</label><select id="selSleep"></select></div>
      <div class="row"><label>Display</label><select id="selDisplay"></select></div>
      <div class="row"><label>HDMI out</label><select id="selHdmi"></select></div>
      <div class="row"><button id="btnDimmer">Dimmer</button></div>
    </section>

    <section class="card">
      <h2>Wejścia (pełna lista techniczna)</h2>
      <div class="row"><label>Wejście</label><select id="selInput"></select></div>
      <div class="mut">Do codziennego używania wystarczy kafel „Źródło” na stronie głównej.</div>
    </section>

    <section class="card">
      <h2>Strefa 2</h2>
      <div class="row"><button id="z2On">ON</button><button id="z2Off">OFF</button><button id="z2Mute">Mute</button></div>
      <div class="row"><label>Wejście</label><select id="z2Input"></select></div>
      <div class="row"><label>Głośność</label><span class="val" id="z2Vol">-</span>
        <button id="z2VolDown" class="ghost">−</button><button id="z2VolUp" class="ghost">+</button></div>
    </section>

    <section class="card">
      <h2>Strefa 3</h2>
      <div class="row"><button id="z3On">ON</button><button id="z3Off">OFF</button><button id="z3Mute">Mute</button></div>
      <div class="row"><label>Wejście</label><select id="z3Input"></select></div>
      <div class="row"><label>Głośność</label><span class="val" id="z3Vol">-</span>
        <button id="z3VolDown" class="ghost">−</button><button id="z3VolUp" class="ghost">+</button></div>
    </section>

    <section class="card">
      <h2>Tuner</h2>
      <div class="row"><label>Częstotliwość</label><span class="big" id="tunFreq">-</span></div>
      <div class="row"><button id="tunUp">▲</button><button id="tunDown">▼</button><button id="tunBand">FM/AM</button></div>
      <div class="row"><label>Preset</label><button id="presetDown" class="ghost">−</button><span class="val" id="presetTxt">-</span><button id="presetUp" class="ghost">+</button></div>
      <div class="row"><input type="text" id="directFreq" placeholder="np. 08750 (FM) / 00522 (AM)" style="flex:1">
        <button id="btnDirect">Nastrój</button></div>
    </section>

    <section class="card">
      <h2>Informacje</h2>
      <div class="mut" id="netInfo">-</div>
    </section>

    <section class="card">
      <h2>Konsola eISCP</h2>
      <div class="row"><input type="text" id="rawCmd" placeholder="np. PWRQSTN, MVL32, TCL000..." style="flex:1">
        <button id="btnRaw">Wyślij</button></div>
      <pre id="rawOut">gotowe</pre>
    </section>
  </div>
</main>

<div id="toast"></div>

<script>
const $ = (id) => document.getElementById(id);
const HEX = (n) => Math.max(0, Math.min(255, n)).toString(16).toUpperCase().padStart(2, "0");

let state = {};
let volumeDragging = false;
let volumeTimer = null;
let volumePending = null;
let hostTyping = false;

function toast(msg, info) {
  const box = $("toast");
  const el = document.createElement("div");
  if (info) el.className = "info";
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .4s"; }, 4200);
  setTimeout(() => el.remove(), 4800);
}

async function api(path, payload) {
  const opts = payload === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  };
  let res;
  try { res = await fetch(path, opts); }
  catch (e) { toast("Brak połączenia z panelem", true); return { ok: false }; }
  let data = null;
  try { data = await res.json(); } catch (e) { data = { ok: false, error: "zła odpowiedź serwera" }; }
  if (data && data.ok === false && data.error) toast(data.error, true);
  return data || { ok: false };
}

async function cmd(c) {
  const r = await api("/api/command", { cmd: c });
  if (r.ok) setTimeout(load, 80);
  return r;
}

/* --------------------------- zakładki --------------------------- */
function showTab(name) {
  document.querySelectorAll(".tabpage").forEach((page) => {
    page.hidden = page.id !== "tab-" + name;
  });
  document.querySelectorAll("#tabs button").forEach((btn) => {
    btn.classList.toggle("on", btn.dataset.tab === name);
  });
  localStorage.setItem("onkyo-tab", name);
  try { history.replaceState(null, "", "#" + name); } catch (e) {}
}
document.querySelectorAll("#tabs button").forEach((btn) => {
  btn.onclick = () => showTab(btn.dataset.tab);
});
const desiredTab = (location.hash || "").replace("#", "") || localStorage.getItem("onkyo-tab");
showTab(desiredTab === "settings" ? "settings" : "main");
window.addEventListener("hashchange", () => {
  const tab = (location.hash || "").replace("#", "");
  if (tab === "main" || tab === "settings") showTab(tab);
});

/* --------------------------- źródła --------------------------- */
let sourceButtons = [];
function buildSources() {
  const box = $("sources");
  box.innerHTML = "";
  sourceButtons = [];
  Object.entries(inputs).forEach(([code, label]) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.dataset.code = code;
    b.onclick = () => cmd("SLI" + code);
    box.appendChild(b);
    sourceButtons.push(b);
  });
}

function fillSelect(el, items, current) {
  const want = current === undefined ? el.value : current;
  if (el.dataset.built !== "1") {
    el.innerHTML = "";
    for (const [value, label] of items) {
      const o = document.createElement("option");
      o.value = value; o.textContent = label; el.appendChild(o);
    }
    el.dataset.built = "1";
  }
  if (want !== undefined && want !== null) { el.value = String(want).toUpperCase(); }
}

function ago(ts) {
  if (!ts) return "brak danych";
  const sec = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (sec < 3) return "przed chwilą";
  if (sec < 60) return sec + " s temu";
  return Math.round(sec / 60) + " min temu";
}

function render(s) {
  state = s;
  const online = !!s.online;
  $("onDot").className = "dot" + (online ? "" : " off");
  $("onTxt").textContent = online ? "ONLINE" : "OFFLINE";
  $("connDot").className = "dot" + (online ? "" : " off");
  $("connStatus").textContent = online ? "ONLINE" : "BRAK POŁĄCZENIA";
  $("connStatus").className = "big " + (online ? "ok" : "bad");
  $("lastSeen").textContent = "Ostatnia odpowiedź: " + ago(s.last_seen) + "   (host " + (s.host || "-") + ")";
  $("pwrTxt").textContent = online
    ? (s.power === "01" ? "ON" : (s.power === "00" ? "STANDBY" : "?"))
    : "OFFLINE";
  $("hostChip").textContent = s.host || "-";
  if (!hostTyping) $("hostInput").value = s.host || "";
  $("inTxt").textContent = s.input ? (inputs[s.input] || s.input) : "-";
  $("modeTxt").textContent = s.mode ? (modes[s.mode] || s.mode) : "-";
  $("volTxt").textContent = s.volume != null ? s.volume : "-";
  if (!volumeDragging) {
    $("volBig").textContent = s.volume != null ? s.volume : "-";
    $("volRange").value = s.volume != null ? s.volume : 0;
  }
  if (s.bass != null) { $("bass").value = s.bass; $("bassTxt").textContent = (s.bass > 0 ? "+" : "") + s.bass + " dB"; }
  if (s.treble != null) { $("treble").value = s.treble; $("trebleTxt").textContent = (s.treble > 0 ? "+" : "") + s.treble + " dB"; }
  if (s.sub_level != null) { $("sub").value = s.sub_level; $("subTxt").textContent = (s.sub_level > 0 ? "+" : "") + s.sub_level + " dB"; }
  if (s.center_level != null) { $("center").value = s.center_level; $("centerTxt").textContent = (s.center_level > 0 ? "+" : "") + s.center_level + " dB"; }
  $("btnMute").className = s.mute === "01" ? "on" : "";
  $("btnCinema").className = s.cinema_filter === "01" ? "on" : "";
  sourceButtons.forEach((b) => b.classList.toggle("on", b.dataset.code === s.input));
  fillSelect($("selInput"), inputItems, s.input);
  fillSelect($("selMode"), modeItems, s.mode);
  fillSelect($("selHdmi"), hdmiItems, s.hdmi_out);
  fillSelect($("selSleep"), sleepItems, sleepKey(s.sleep));
  fillSelect($("selDisplay"), displayItems, s.display_mode);
  $("audioInfo").textContent = "Audio: " + (s.audio || "brak sygnału");
  $("displayInfo").textContent = "Audio: " + (s.audio || "brak sygnału");
  $("z2Vol").textContent = s.z2 && s.z2.volume && s.z2.volume !== "N/A" ? parseInt(s.z2.volume, 16) : "n/a";
  $("z3Vol").textContent = s.z3 && s.z3.volume && s.z3.volume !== "N/A" ? parseInt(s.z3.volume, 16) : "n/a";
  fillSelect($("z2Input"), inputItems, s.z2 && s.z2.input);
  fillSelect($("z3Input"), inputItems, s.z3 && s.z3.input);
  $("tunFreq").textContent = fmtFreq(s.tuner);
  $("presetTxt").textContent = s.preset && s.preset !== "00" ? parseInt(s.preset, 16) : "-";
  if (s.cast) {
    $("castTxt").textContent = s.cast.transport || "-";
    $("castInfo").textContent = s.cast.rel ? (s.cast.transport + " " + s.cast.rel + " / " + s.cast.duration) : "-";
  }
  $("netInfo").textContent = "Host: " + (s.host || "-") + "   ·   status: " + (online ? "online" : "offline")
    + "   ·   eISCP 60128, UPnP 8888";
  if (!online) {
    $("connHint").className = "mut warn";
    $("connHint").textContent =
      "Amplituner nie odpowiada. Sprawdź zasilanie lub włącz Network Standby (SETUP > Network > Network Standby > On). " +
      "Jeśli zmienił IP — kliknij „Szukaj amplitunera w sieci”.";
  } else {
    $("connHint").className = "mut ok";
    $("connHint").textContent = "Połączenie aktywne.";
  }
  const filesEl = $("files");
  const files = s.files || [];
  if (filesEl.dataset.sig !== String(files.length) + files.join("|")) {
    filesEl.dataset.sig = String(files.length) + files.join("|");
    filesEl.innerHTML = "";
    if (!files.length) filesEl.innerHTML = '<div class="mut">brak plików w folderze</div>';
    files.forEach((f) => {
      const d = document.createElement("div"); d.className = "f";
      const sp = document.createElement("span"); sp.textContent = f;
      const b = document.createElement("button"); b.textContent = "Cast"; b.onclick = () => cast(f);
      d.appendChild(sp); d.appendChild(b); filesEl.appendChild(d);
    });
  }
}

function sleepKey(v) {
  if (!v || v === "00" || v === "OFF") return "OFF";
  return ["1E", "3C", "5A"].includes(v) ? v : "OFF";
}
function toneFmt(v) {
  v = Math.trunc(Number(v) || 0);
  if (v === 0) return "00";
  return (v > 0 ? "+" : "-") + Math.abs(v).toString(16).toUpperCase();
}
function fmtFreq(f) {
  if (!f || f === "N/A") return "-";
  const n = parseInt(f, 10);
  if (!isFinite(n)) return f;
  if (n >= 8750) return (n / 100).toFixed(2).replace(".", ",") + " MHz FM";
  return n + " kHz AM";
}

async function load() {
  try { render(await api("/api/state")); } catch (e) {}
}
async function cast(f) {
  const r = await api("/api/cast", { file: f });
  if (r.ok) { toast("Cast: " + f, true); load(); }
}

/* ---- głośność ---- */
$("volRange").addEventListener("pointerdown", () => { volumeDragging = true; });
$("volRange").addEventListener("input", (e) => {
  volumeDragging = true;
  $("volBig").textContent = e.target.value;
  $("volTxt").textContent = e.target.value;
  volumePending = e.target.value;
  if (!volumeTimer) volumeTimer = setTimeout(sendVolume, 110);
});
$("volRange").addEventListener("change", (e) => {
  volumePending = e.target.value;
  sendVolume();
  setTimeout(() => { volumeDragging = false; }, 250);
});
function sendVolume() {
  volumeTimer = null;
  if (volumePending == null) return;
  const value = volumePending; volumePending = null;
  cmd("MVL" + HEX(Number(value)));
}

$("btnOn").onclick = () => cmd("PWR01");
$("btnStandby").onclick = () => cmd("PWR00");
$("btnAllOff").onclick = () => cmd("PWRALL");
$("volUp").onclick = () => cmd("MVLUP");
$("volDown").onclick = () => cmd("MVLDOWN");
$("btnMute").onclick = () => cmd(state.mute === "01" ? "AMT00" : "AMT01");
$("selInput").onchange = (e) => cmd("SLI" + e.target.value);
$("selMode").onchange = (e) => cmd("LMD" + e.target.value);
$("selHdmi").onchange = (e) => cmd("HDO" + e.target.value);
$("btnCinema").onclick = () => cmd(state.cinema_filter === "01" ? "RAS00" : "RAS01");
$("btnDimmer").onclick = () => cmd("DIM" + (state.dimmer === "00" ? "01" : "00"));
$("bass").onchange = (e) => cmd("TFRB" + toneFmt(e.target.value));
$("treble").onchange = (e) => cmd("TFRT" + toneFmt(e.target.value));
$("sub").onchange = (e) => cmd("SWL" + toneFmt(e.target.value));
$("center").onchange = (e) => cmd("CTL" + toneFmt(e.target.value));
$("selSleep").onchange = (e) => cmd("SLP" + e.target.value);
$("selDisplay").onchange = (e) => cmd("DIF" + e.target.value);
$("z2On").onclick = () => cmd("ZPW01");
$("z2Off").onclick = () => cmd("ZPW00");
$("z2Mute").onclick = () => cmd("ZMTTG");
$("z2Input").onchange = (e) => cmd("SLZ" + e.target.value);
$("z2VolUp").onclick = () => cmd("ZVLUP");
$("z2VolDown").onclick = () => cmd("ZVLDOWN");
$("z3On").onclick = () => cmd("PW301");
$("z3Off").onclick = () => cmd("PW300");
$("z3Mute").onclick = () => cmd("MT3TG");
$("z3Input").onchange = (e) => cmd("SL3" + e.target.value);
$("z3VolUp").onclick = () => cmd("VL3UP");
$("z3VolDown").onclick = () => cmd("VL3DOWN");
$("tunUp").onclick = () => cmd("TUNUP");
$("tunDown").onclick = () => cmd("TUNDOWN");
$("tunBand").onclick = () => cmd("TUNBAND");
$("presetUp").onclick = () => cmd("PRSUP");
$("presetDown").onclick = () => cmd("PRSDOWN");
$("btnDirect").onclick = async () => {
  const v = ($("directFreq").value || "").replace(/[^0-9]/g, "").padStart(5, "0").slice(0, 5);
  await cmd("TUNDIRECT");
  for (const ch of v) await cmd("TUN" + ch);
};
$("btnCastUrl").onclick = async () => {
  const u = $("castUrl").value.trim();
  if (!u) return;
  const r = await api("/api/cast", { url: u });
  if (r.ok) { toast("Cast URL uruchomiony", true); load(); }
};
$("btnCastStop").onclick = async () => { await api("/api/cast/stop"); load(); };
$("btnRaw").onclick = async () => {
  const r = await api("/api/command", { cmd: $("rawCmd").value.trim() });
  $("rawOut").textContent = JSON.stringify(r, null, 1);
  load();
};
$("rawCmd").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btnRaw").click(); });

/* ---- połączenie / wyszukiwanie ---- */
$("hostInput").addEventListener("input", () => { hostTyping = true; });
$("hostInput").addEventListener("blur", () => { hostTyping = false; });
$("btnConnect").onclick = async () => {
  const host = $("hostInput").value.trim();
  if (!host) return;
  const r = await api("/api/host", { host: host });
  if (r.ok) { hostTyping = false; toast("Ustawiono host: " + r.host, true); load(); }
};
$("btnDiscover").onclick = async () => {
  $("discoverInfo").textContent = "szukam…";
  const box = $("devices"); box.innerHTML = "";
  const r = await api("/api/discover");
  $("discoverInfo").textContent = "";
  if (!r.ok) return;
  const devices = r.devices || [];
  if (!devices.length) { toast("Nie znaleziono amplitunera w sieci", true); return; }
  devices.forEach((d) => {
    const row = document.createElement("div"); row.className = "d";
    const left = document.createElement("span");
    left.innerHTML = "<b>" + (d.friendly || d.model || "urządzenie") + "</b><br>" + (d.ip || "");
    const b = document.createElement("button");
    b.textContent = d.ip === r.current ? "aktualny" : "Użyj";
    b.disabled = d.ip === r.current;
    b.onclick = async () => {
      const h = await api("/api/host", { host: d.ip });
      if (h.ok) { toast("Przełączono na " + d.ip, true); load(); }
    };
    row.appendChild(left); row.appendChild(b); box.appendChild(row);
  });
  toast("Znaleziono: " + devices.length, true);
};

const inputs = "__INPUTS__";
const modes = "__MODES__";
const inputItems = Object.entries(inputs);
const modeItems = Object.entries(modes);
const hdmiItems = __HDMI__;
const sleepItems = __SLEEP__;
const displayItems = [["00","1 linia"],["01","2 linie"],["02","Format cyfrowy"],["03","Format wideo"]];

buildSources();
load(); setInterval(load, 1000);
</script>
</body>
</html>
"""


def build_html():
    sleep_items = [[code, label] for code, label in SLEEP_CHOICES]
    html = HTML
    html = html.replace('"__INPUTS__"', json.dumps(INPUTS, ensure_ascii=False))
    html = html.replace('"__MODES__"', json.dumps(MODES, ensure_ascii=False))
    html = html.replace("__HDMI__", json.dumps([[k, v] for k, v in HDMI_OUT.items()], ensure_ascii=False))
    html = html.replace("__SLEEP__", json.dumps(sleep_items, ensure_ascii=False))
    return html


def main():
    global STATE, MEDIA_ROOT, HTTP_MEDIA_PORT, HTML
    parser = argparse.ArgumentParser(description="Panel WWW do Onkyo TX-NR616")
    parser.add_argument("--host", default=load_config().get("host", DEFAULT_HOST))
    parser.add_argument("--media", default=os.getcwd())
    parser.add_argument("--ui-port", type=int, default=HTTP_UI_PORT)
    parser.add_argument("--media-port", type=int, default=HTTP_MEDIA_PORT)
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args()

    HTTP_MEDIA_PORT = args.media_port
    MEDIA_ROOT = os.path.abspath(args.media)
    if not os.path.isdir(MEDIA_ROOT):
        sys.exit(f"Nie ma folderu: {MEDIA_ROOT}")

    start_http_server(MEDIA_ROOT, args.bind, args.media_port)
    STATE = StateStore(args.host, MEDIA_ROOT)
    HTML = build_html()

    server = ThreadingHTTPServer((args.bind, args.ui_port), Handler)
    server.daemon_threads = True
    ip = local_ip_towards(args.host)
    print(f"Amplituner: {args.host}")
    print(f"Panel: http://127.0.0.1:{args.ui_port}/ (w sieci: http://{ip}:{args.ui_port}/)")
    print(f"Pliki (DLNA): http://{ip}:{args.media_port}/ folder: {MEDIA_ROOT}")
    print("Ctrl+C aby zatrzymac.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nKoniec.")
    finally:
        server.server_close()
        STATE.link.close()
        STATE.cmd_link.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    main()
