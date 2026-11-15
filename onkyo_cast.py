#!/usr/bin/env python3
"""
onkyo_cast.py - "cast" audio to an Onkyo TX-NR616 (DLNA Digital Media Renderer)
just like you would to a Chromecast, but using the receiver's built-in UPnP/DLNA
DMR interface + eISCP control protocol.

No hacking, no firmware modification: the receiver already accepts pushed streams.
This script serves a local file (or a URL) and pushes it to the DMR.

Usage examples:
  python onkyo_cast.py status
  python onkyo_cast.py play "C:\\Music\\track.flac"
  python onkyo_cast.py play "C:\\Music\\track.mp3" --duration 30
  python onkyo_cast.py play-url "http://icecast.example/stream.mp3"
  python onkyo_cast.py ui --media "C:\\Music"
  python onkyo_cast.py volume 35
  python onkyo_cast.py mute on
  python onkyo_cast.py input NET

Verified on TX-NR616 (firmware 2012):
  SetAVTransportURI + Play + Stop work; Pause and Seek are NOT implemented by
  this DMR (UPnP returns an error).

Requires Python 3.8+ (standard library only).
"""

import argparse
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
from xml.sax.saxutils import escape

DEFAULT_HOST = "192.168.1.34"
DEFAULT_HTTP_PORT = 8765
UPNP_PORT = 8888
EISCP_PORT = 60128

SERVICES = {
    "AVTransport": "urn:schemas-upnp-org:service:AVTransport:1",
    "RenderingControl": "urn:schemas-upnp-org:service:RenderingControl:1",
    "ConnectionManager": "urn:schemas-upnp-org:service:ConnectionManager:1",
}

INPUTS = {
    "BD/DVD": "10", "BD": "10", "DVD": "10",
    "CBL/SAT": "01", "SAT": "01", "VIDEO2": "01",
    "GAME": "02", "VIDEO3": "02",
    "AUX": "03",
    "PC": "05",
    "STRM BOX": "11",
    "TV": "12",
    "TAPE": "20",
    "PHONO": "22",
    "CD": "23", "TV/CD": "23",
    "FM": "24",
    "AM": "25",
    "MUSIC SERVER": "27", "DLNA": "27",
    "INTERNET RADIO": "28", "IRADIO": "28",
    "USB": "29",
    "NET": "2B", "NETWORK": "2B",
    "AIRPLAY": "2D",
    "BLUETOOTH": "2E",
    "MULTI CH": "30",
}

MIME = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".wma": "audio/x-ms-wma",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".mp4": "audio/mp4",
}


# ---------------------------------------------------------------- eISCP ----

class Eiscp:
    """Minimal eISCP client (send '!1<CMD>\r\n', parse replies)."""

    def __init__(self, host, port=EISCP_PORT, timeout=3.0):
        self.host, self.port, self.timeout = host, port, timeout

    def _packet(self, command):
        # The receiver only answers commands prefixed with "!1" (unit 1).
        text = command if command.startswith("!") else "!1" + command
        payload = text.encode("ascii") + b"\r\n"
        return (
            b"ISCP"
            + struct.pack(">I", 16)
            + struct.pack(">I", len(payload))
            + b"\x01\x00\x00\x00"
            + b"\x00\x00\x00\x1d"
            + payload
        )

    def command(self, command):
        """Send one command, return list of reply strings (without the !1 prefix)."""
        sock = socket.create_connection((self.host, self.port), self.timeout)
        sock.settimeout(self.timeout)
        replies = []
        try:
            sock.sendall(self._packet(command))
            buffer = b""
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buffer += chunk
                while True:
                    start = buffer.find(b"ISCP")
                    if start < 0 or len(buffer) < start + 16:
                        break
                    size = struct.unpack(">I", buffer[start + 8:start + 12])[0]
                    end = start + 16 + size
                    if len(buffer) < end:
                        break
                    payload = buffer[start + 16:end].decode("ascii", "replace")
                    buffer = buffer[end:]
                    for line in payload.split("\r\n"):
                        line = line.strip("\x1a\r\n ")
                        if line.startswith("!1"):
                            line = line[2:]
                        if line:
                            replies.append(line)
            return replies
        finally:
            sock.close()

    def query(self, code):
        replies = self.command(code + "QSTN")
        for reply in replies:
            if reply.startswith(code):
                return reply[len(code):]
        return None

    def query_many(self, codes):
        """Query several values on ONE connection (receiver dislikes rapid reconnects)."""
        sock = socket.create_connection((self.host, self.port), self.timeout)
        sock.settimeout(self.timeout)
        results = {}
        try:
            for code in codes:
                sock.sendall(self._packet(code + "QSTN"))
                time.sleep(0.12)
            buffer = b""
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buffer += chunk
                while True:
                    start = buffer.find(b"ISCP")
                    if start < 0 or len(buffer) < start + 16:
                        break
                    size = struct.unpack(">I", buffer[start + 8:start + 12])[0]
                    end = start + 16 + size
                    if len(buffer) < end:
                        break
                    payload = buffer[start + 16:end].decode("ascii", "replace")
                    buffer = buffer[end:]
                    for line in payload.split("\r\n"):
                        line = line.strip("\x1a\r\n ")
                        if line.startswith("!1"):
                            line = line[2:]
                        for code in codes:
                            if line.startswith(code):
                                results[code] = line[len(code):]
            return results
        finally:
            sock.close()


# ------------------------------------------------------------------ UPnP ----

def soap(host, control_url, service, action, args_xml="", timeout=3.0):
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service}">{args_xml}</u:{action}></s:Body>'
        "</s:Envelope>"
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://{host}:{UPNP_PORT}{control_url}",
        data=body,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service}#{action}"',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        code = re.search(r"<errorCode>(.*?)</errorCode>", detail)
        text = re.search(r"<errorDescription>(.*?)</errorDescription>", detail)
        raise RuntimeError(
            "UPnP error "
            f"{code.group(1) if code else '?'}: "
            f"{text.group(1) if text else error.reason}"
        ) from None


def extract(xml, tag):
    match = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
    return match.group(1) if match else None


class Renderer:
    def __init__(self, host):
        self.host = host
        self.controls = {}
        self._discover()

    def _discover(self):
        try:
            with urllib.request.urlopen(
                f"http://{self.host}:{UPNP_PORT}/upnp_descriptor_0", timeout=4
            ) as response:
                xml = response.read().decode("utf-8", "replace")
            for block in re.findall(r"<service>(.*?)</service>", xml, re.S):
                service_type = extract(block, "serviceType")
                control_url = extract(block, "controlURL")
                if service_type and control_url:
                    for name, urn in SERVICES.items():
                        if service_type == urn:
                            self.controls[name] = control_url
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] UPnP discovery failed ({exc}); using default control URLs")
            self.controls = {
                "AVTransport": "/upnp_control_2",
                "ConnectionManager": "/upnp_control_1",
                "RenderingControl": "/upnp_control_0",
            }
        if "AVTransport" not in self.controls:
            self.controls["AVTransport"] = "/upnp_control_2"

    def call(self, service, action, args_xml="", timeout=3.0):
        urn = SERVICES[service]
        url = self.controls.get(service)
        if not url:
            raise RuntimeError(f"service {service} not found on renderer")
        return soap(self.host, url, urn, action, args_xml, timeout=timeout)

    # --- convenience wrappers -------------------------------------------
    def set_uri(self, uri, title, mime):
        didl = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="0" parentID="-1" restricted="1">'
            f"<dc:title>{escape(title)}</dc:title>"
            "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
            f'<res protocolInfo="http-get:*:{mime}:*">{escape(uri)}</res>'
            "</item></DIDL-Lite>"
        )
        return self.call(
            "AVTransport",
            "SetAVTransportURI",
            f"<InstanceID>0</InstanceID><CurrentURI>{escape(uri)}</CurrentURI>"
            f"<CurrentURIMetaData>{escape(didl)}</CurrentURIMetaData>",
        )

    def set_next_uri(self, uri, title, mime):
        didl = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="0" parentID="-1" restricted="1">'
            f"<dc:title>{escape(title)}</dc:title>"
            "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
            f'<res protocolInfo="http-get:*:{mime}:*">{escape(uri)}</res>'
            "</item></DIDL-Lite>"
        )
        return self.call(
            "AVTransport",
            "SetNextAVTransportURI",
            f"<InstanceID>0</InstanceID><NextURI>{escape(uri)}</NextURI>"
            f"<NextURIMetaData>{escape(didl)}</NextURIMetaData>",
        )

    def play(self, speed=1):
        return self.call(
            "AVTransport", "Play", f"<InstanceID>0</InstanceID><Speed>{speed}</Speed>"
        )

    def pause(self):
        return self.call("AVTransport", "Pause", "<InstanceID>0</InstanceID>")

    def stop(self):
        return self.call("AVTransport", "Stop", "<InstanceID>0</InstanceID>")

    def seek(self, seconds):
        target = time.strftime("%H:%M:%S", time.gmtime(max(0, int(seconds))))
        return self.call(
            "AVTransport",
            "Seek",
            "<InstanceID>0</InstanceID><Unit>REL_TIME</Unit>"
            f"<Target>{target}</Target>",
        )

    def transport_info(self):
        xml = self.call("AVTransport", "GetTransportInfo", "<InstanceID>0</InstanceID>")
        return (
            extract(xml, "CurrentTransportState"),
            extract(xml, "CurrentTransportStatus"),
        )

    def position(self):
        xml = self.call("AVTransport", "GetPositionInfo", "<InstanceID>0</InstanceID>")
        return {
            "track": extract(xml, "Track"),
            "duration": extract(xml, "TrackDuration"),
            "rel": extract(xml, "RelTime"),
            "uri": extract(xml, "TrackURI"),
        }

    def volume(self):
        xml = self.call(
            "RenderingControl",
            "GetVolume",
            "<InstanceID>0</InstanceID><Channel>Master</Channel>",
        )
        return extract(xml, "CurrentVolume")


# ----------------------------------------------------------- HTTP server ----

class MediaHandler(BaseHTTPRequestHandler):
    server_version = "onkyo-cast/1.1.1"
    media_root = os.getcwd()

    def _serve(self, with_body=True):
        raw_path = urllib.parse.urlparse(self.path).path
        rel = urllib.parse.unquote(raw_path).lstrip("/")
        root = os.path.realpath(self.media_root)
        full = os.path.realpath(os.path.join(root, rel))
        try:
            if os.path.commonpath([root, full]) != root:
                self.send_error(403)
                return
        except ValueError:
            self.send_error(403)
            return
        if not os.path.isfile(full):
            print(f"[http] {self.client_address[0]} 404 {raw_path}")
            self.send_error(404)
            return
        size = os.path.getsize(full)
        mime = MIME.get(os.path.splitext(full)[1].lower()) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            try:
                spec = rng[6:].split(",")[0].strip()
                first, _, last = spec.partition("-")
                if first:
                    start = int(first)
                if last:
                    end = min(int(last), size - 1)
                if start > end or start < 0 or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if with_body:
            with open(full, "rb") as handle:
                handle.seek(start)
                remaining = length
                try:
                    while remaining > 0:
                        chunk = handle.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    print(f"[http] {self.client_address[0]} client closed early "
                          f"({length - remaining}/{length} bytes sent)")
        print(f"[http] {self.client_address[0]} {self.command} {raw_path}"
              f" ({mime}, {length}B{' range ' + rng if rng else ''})")

    def do_GET(self):
        self._serve(True)

    def do_HEAD(self):
        self._serve(False)


def start_http_server(media_root, bind, port):
    global _server
    if _server is not None:
        return _server
    handler = type("Handler", (MediaHandler,), {"media_root": media_root})
    _server = ThreadingHTTPServer((bind, port), handler)
    _server.daemon_threads = True
    thread = threading.Thread(target=_server.serve_forever, daemon=True)
    thread.start()
    return _server


def local_ip_towards(host):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, EISCP_PORT))
        return sock.getsockname()[0]
    finally:
        sock.close()


_server = None


# --------------------------------------------------------------- commands ----

def cmd_status(args):
    rx = Eiscp(args.host)
    print(f"Host: {args.host}")
    values = rx.query_many(["PWR", "MVL", "AMT", "SLI", "LMD", "IFA", "HDO", "TUN"])
    power = values.get("PWR")
    volume = values.get("MVL")
    mute = values.get("AMT")
    source = values.get("SLI")
    listening = values.get("LMD")
    audio = values.get("IFA")
    hdmi_out = values.get("HDO")
    print(f"  power      : {'ON' if power and power.endswith('1') else power}")
    if volume is not None and re.fullmatch(r"[0-9A-F]{2}", volume):
        print(f"  volume     : {int(volume, 16)}/100 (raw MVL {volume})")
    else:
        print(f"  volume     : {volume}")
    print(f"  mute       : {mute}")
    print(f"  input (SLI): {source}")
    print(f"  listening  : {listening}")
    print(f"  hdmi out   : {hdmi_out}")
    print(f"  audio info : {audio}")
    try:
        renderer = Renderer(args.host)
        state, status = renderer.transport_info()
        position = renderer.position()
        print(f"  transport  : {state} / {status}")
        print(f"  position   : {position['rel']} / {position['duration']}")
        if position["uri"]:
            print(f"  current uri: {position['uri']}")
        print(f"  upnp volume: {renderer.volume()}")
    except Exception as exc:  # noqa: BLE001
        print(f"  transport  : (UPnP error: {exc})")


def _push(args, uri, title, mime):
    renderer = Renderer(args.host)
    print(f"Casting: {title}")
    print(f"  {uri}")
    renderer.set_uri(uri, title, mime)
    renderer.play(1)
    time.sleep(0.8)
    state, status = renderer.transport_info()
    print(f"Transport: {state} / {status}")
    return renderer


def cmd_play(args):
    target = args.target
    is_url = bool(re.match(r"^https?://", target, re.I))
    previous_input = None
    if args.restore and not is_url:
        # must be captured BEFORE casting, the renderer switches input by itself
        previous_input = Eiscp(args.host).query("SLI")
    if is_url:
        mime = MIME.get(os.path.splitext(urllib.parse.urlparse(target).path)[1].lower(), "audio/mpeg")
        renderer = _push(args, target, os.path.basename(urllib.parse.urlparse(target).path) or "stream", mime)
    else:
        full = os.path.abspath(target)
        if not os.path.isfile(full):
            sys.exit(f"File not found: {full}")
        media_root = os.path.dirname(full)
        start_http_server(media_root, args.bind, args.port)
        ip = local_ip_towards(args.host)
        name = os.path.basename(full)
        uri = f"http://{ip}:{args.port}/{urllib.parse.quote(name)}"
        mime = MIME.get(os.path.splitext(name)[1].lower(), mimetypes.guess_type(name)[0] or "audio/mpeg")
        print(f"Serving {media_root} at http://{ip}:{args.port}/")
        renderer = _push(args, uri, name, mime)
    started = time.time()
    try:
        while True:
            time.sleep(1.5)
            state, _ = renderer.transport_info()
            position = renderer.position()
            elapsed = int(time.time() - started)
            print(f"  [{elapsed:>4}s] {state:<12} {position['rel']} / {position['duration']}")
            if args.duration and elapsed >= args.duration:
                break
            if state in ("STOPPED", "NO_MEDIA_PRESENT") and elapsed > 4:
                print("Playback finished / dropped.")
                break
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        try:
            renderer.stop()
        except Exception:  # noqa: BLE001
            pass
        if previous_input and re.fullmatch(r"[0-9A-F]{2}", previous_input):
            try:
                Eiscp(args.host).command("SLI" + previous_input)
                print(f"Restored input to SLI{previous_input}")
            except Exception:  # noqa: BLE001
                pass
        if _server:
            _server.shutdown()


def cmd_stop(args):
    renderer = Renderer(args.host)
    renderer.stop()
    print("Stopped.")


def cmd_pause(args):
    Renderer(args.host).pause()
    print("Paused.")


def cmd_resume(args):
    Renderer(args.host).play(1)
    print("Resumed.")


def cmd_seek(args):
    Renderer(args.host).seek(args.seconds)
    print(f"Seek to {args.seconds}s.")


def _fmt_volume(value):
    return f"{int(value, 16)}/100"


def cmd_volume(args):
    rx = Eiscp(args.host)
    value = max(0, min(100, args.level))
    rx.command(f"MVL{value:02X}")
    time.sleep(0.4)
    print(f"Volume -> {value}/100 (read back {rx.query('MVL')})")


def cmd_mute(args):
    rx = Eiscp(args.host)
    rx.command("AMT01" if args.state == "on" else "AMT00")
    time.sleep(0.3)
    print(f"Mute -> {args.state} (read back {rx.query('AMT')})")


def cmd_input(args):
    key = args.name.strip().upper()
    code = INPUTS.get(key)
    if code is None:
        sys.exit(f"Unknown input '{args.name}'. Known: {', '.join(sorted(INPUTS))}")
    rx = Eiscp(args.host)
    rx.command("SLI" + code)
    time.sleep(0.5)
    print(f"Input -> {args.name} (SLI{code}), read back SLI{rx.query('SLI')}")


def cmd_power(args):
    Eiscp(args.host).command("PWR01" if args.state == "on" else "PWR00")
    print(f"Power -> {args.state}")


# ------------------------------------------------------------- web UI ----

UI_HTML = """<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<title>Onkyo TX-NR616 - cast</title>
<style>
  body { font-family: system-ui, sans-serif; background: #121418; color: #e2e8f0; max-width: 720px; margin: 24px auto; padding: 0 16px; }
  .card { background: #1e2229; border-radius: 10px; padding: 16px; margin-bottom: 16px; }
  button { background: #3b82f6; color: #fff; border: 0; border-radius: 6px; padding: 8px 14px; cursor: pointer; margin-right: 6px; }
  button.sec { background: #334155; }
  ul { list-style: none; padding: 0; }
  li { padding: 6px 0; border-bottom: 1px solid #2d333f; display: flex; justify-content: space-between; align-items: center; }
</style>
</head>
<body>
  <h1>Onkyo TX-NR616 — cast</h1>
  <div class="card">
    <div id="info">łączenie...</div>
    <p>
      <button onclick="api('/api/resume')">Play</button>
      <button class="sec" onclick="api('/api/stop')">Stop</button>
      Głośność: <input type="range" id="vol" min="0" max="100" onchange="api('/api/volume?level='+this.value)">
    </p>
    <small>Ten DMR nie obsługuje pauzy ani przewijania - jest Play i Stop.</small>
  </div>
  <div class="card">
    <h2>Pliki w folderze</h2>
    <ul id="files"></ul>
    <small>Pliki serwuje ten komputer (port %%MEDIA_PORT%%). CLI: python onkyo_cast.py play plik.flac</small>
  </div>
<script>
async function api(url) {
  const r = await fetch(url);
  const d = await r.json();
  render(d);
}
function render(d) {
  if (!d || d.ok === false) return;
  document.getElementById('info').textContent =
    `Host: ${d.host} | PWR: ${d.power} | Vol: ${d.volume ?? '?'} | Transport: ${d.transport ?? '?'} (${d.rel ?? ''}/${d.duration ?? ''})`;
  if (d.volume != null) document.getElementById('vol').value = d.volume;
  const ul = document.getElementById('files');
  ul.innerHTML = '';
  (d.files || []).forEach(f => {
    const li = document.createElement('li');
    const span = document.createElement('span');
    span.textContent = f;
    const btn = document.createElement('button');
    btn.textContent = 'Cast';
    btn.onclick = () => api('/api/play?file=' + encodeURIComponent(f));
    li.append(span, btn);
    ul.appendChild(li);
  });
}
setInterval(() => api('/api/status'), 2500);
api('/api/status');
</script>
</body>
</html>"""


def build_status(host, media_root):
    values = Eiscp(host).query_many(["PWR", "MVL", "AMT", "SLI", "LMD", "IFA"])
    volume = values.get("MVL")
    status = {
        "host": host,
        "power": values.get("PWR"),
        "mute": values.get("AMT"),
        "volume": int(volume, 16) if volume and re.fullmatch(r"[0-9A-F]{2}", volume) else None,
        "input": values.get("SLI"),
        "listening": values.get("LMD"),
        "audio": values.get("IFA"),
        "files": [],
    }
    try:
        renderer = Renderer(host)
        state, _ = renderer.transport_info()
        position = renderer.position()
        status.update({
            "transport": state,
            "rel": position["rel"],
            "duration": position["duration"],
            "uri": position["uri"],
        })
    except Exception:  # noqa: BLE001
        pass
    try:
        extensions = tuple(MIME)
        found = []
        for root_dir, _, filenames in os.walk(media_root):
            for fname in filenames:
                if os.path.splitext(fname)[1].lower() in extensions:
                    rel_path = os.path.relpath(os.path.join(root_dir, fname), media_root)
                    found.append(rel_path.replace("\\", "/"))
        status["files"] = sorted(found)
    except OSError:
        pass
    return status


def push_file(host, media_root, name, http_port):
    root = os.path.realpath(media_root)
    full = os.path.realpath(os.path.join(root, name))
    try:
        if os.path.commonpath([root, full]) != root or not os.path.isfile(full):
            raise RuntimeError(f"file not found: {name}")
    except ValueError as exc:
        raise RuntimeError(f"invalid file path: {name}") from exc
    start_http_server(root, "0.0.0.0", http_port)
    ip = local_ip_towards(host)
    rel_posix = os.path.relpath(full, root).replace("\\", "/")
    uri = f"http://{ip}:{http_port}/{urllib.parse.quote(rel_posix, safe='/')}"
    mime = MIME.get(os.path.splitext(full)[1].lower(), mimetypes.guess_type(full)[0] or "audio/mpeg")
    renderer = Renderer(host)
    renderer.set_uri(uri, os.path.basename(full), mime)
    renderer.play(1)


class UIHandler(BaseHTTPRequestHandler):
    server_version = "onkyo-cast-ui/1.1.1"
    host = DEFAULT_HOST
    media_root = os.getcwd()
    http_port = DEFAULT_HTTP_PORT

    def _send(self, body, content_type):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, payload):
        self._send(json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path in ("/", "/index.html"):
                self._send(UI_HTML.replace("%%MEDIA_PORT%%", str(self.http_port)), "text/html; charset=utf-8")
            elif parsed.path == "/api/status":
                self._json(build_status(self.host, self.media_root))
            elif parsed.path == "/api/play":
                name = query.get("file", [""])[0]
                push_file(self.host, self.media_root, name, self.http_port)
                self._json(build_status(self.host, self.media_root))
            elif parsed.path == "/api/pause":
                Renderer(self.host).pause()
                self._json(build_status(self.host, self.media_root))
            elif parsed.path == "/api/resume":
                Renderer(self.host).play(1)
                self._json(build_status(self.host, self.media_root))
            elif parsed.path == "/api/stop":
                Renderer(self.host).stop()
                self._json(build_status(self.host, self.media_root))
            elif parsed.path == "/api/volume":
                level = max(0, min(100, int(query.get("level", ["0"])[0])))
                Eiscp(self.host).command(f"MVL{level:02X}")
                self._json(build_status(self.host, self.media_root))
            else:
                self.send_error(404)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": str(exc)})

    def log_message(self, fmt, *args):
        print(f"[ui] {args[0] if args else ''}")


def cmd_ui(args):
    media_root = os.path.abspath(args.media)
    if not os.path.isdir(media_root):
        sys.exit(f"Not a folder: {media_root}")
    start_http_server(media_root, args.bind, args.port)
    handler = type("UIBound", (UIHandler,), {"host": args.host, "media_root": media_root, "http_port": args.port})
    server = ThreadingHTTPServer((args.bind, args.ui_port), handler)
    server.daemon_threads = True
    ip = local_ip_towards(args.host)
    print(f"Media folder : {media_root}")
    print(f"Media server : http://{ip}:{args.port}/")
    print(f"Web UI       : http://127.0.0.1:{args.ui_port}/ (also http://{ip}:{args.ui_port}/)")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye")
    finally:
        server.server_close()
        if _server is not None:
            _server.shutdown()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Cast audio to an Onkyo TX-NR616 (DLNA renderer) like a Chromecast.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="receiver IP")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT,
                        help="local HTTP port used to serve media")
    parser.add_argument("--bind", default="0.0.0.0", help="bind address for the media server")
    sub = parser.add_subparsers(dest="command", required=True)

    play = sub.add_parser("play", help="cast a local file or an http(s) URL")
    play.add_argument("target")
    play.add_argument("--duration", type=int, default=0,
                      help="stop after N seconds (0 = run until Ctrl+C)")
    play.add_argument("--restore", action="store_true", default=True,
                      help="restore previous input after stop (default: on)")
    play.set_defaults(func=cmd_play)

    play_url = sub.add_parser("play-url", help="cast an internet stream URL")
    play_url.add_argument("target")
    play_url.add_argument("--duration", type=int, default=0)
    play_url.set_defaults(func=cmd_play, restore=False)

    sub.add_parser("status", help="show receiver + transport state").set_defaults(func=cmd_status)
    sub.add_parser("stop", help="stop playback").set_defaults(func=cmd_stop)
    sub.add_parser("pause", help="pause playback").set_defaults(func=cmd_pause)
    sub.add_parser("resume", help="resume playback").set_defaults(func=cmd_resume)

    seek = sub.add_parser("seek", help="seek in the current track (seconds)")
    seek.add_argument("seconds", type=float)
    seek.set_defaults(func=cmd_seek)

    vol = sub.add_parser("volume", help="set master volume 0-100")
    vol.add_argument("level", type=int)
    vol.set_defaults(func=cmd_volume)

    mute = sub.add_parser("mute", help="mute on/off")
    mute.add_argument("state", choices=["on", "off"])
    mute.set_defaults(func=cmd_mute)

    inp = sub.add_parser("input", help="select input, e.g. NET, BD/DVD, FM")
    inp.add_argument("name")
    inp.set_defaults(func=cmd_input)

    power = sub.add_parser("power", help="power on/off")
    power.add_argument("state", choices=["on", "off"])
    power.set_defaults(func=cmd_power)

    ui = sub.add_parser("ui", help="start a small web UI for casting")
    ui.add_argument("--ui-port", type=int, default=8766, help="local web UI port")
    ui.add_argument("--media", default=".", help="folder with audio files")
    ui.set_defaults(func=cmd_ui)

    return parser


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    arguments = build_parser().parse_args()
    if not hasattr(arguments, "restore"):
        arguments.restore = False
    try:
        arguments.func(arguments)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except urllib.error.URLError as exc:
        sys.exit(f"Network error: {exc}")
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Error: {exc}")
