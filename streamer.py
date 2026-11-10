from __future__ import annotations

import base64
import json
import os
import random
import select
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "session.json"
LOG_FILE = ROOT / "streamer.log"

ME_URL = "https://discord.com/api/v9/users/@me?with_analytics_token=true"
USERS_URL = "https://discord.com/api/v9/users"
SCIENCE_URL = "https://discord.com/api/v9/science"
PROPERTIES_URL = "https://cordapi.dolfi.es/api/v2/properties/windows"
GATEWAY_URL = "gateway.discord.gg"

CLIENT_VERSION = "1.0.9267"
CLIENT_BUILD_NUMBER = 667580
NATIVE_BUILD_NUMBER = 97793
OS_VERSION = "10.0.26100"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) discord/1.0.9267 Chrome/152.0.7977.169 "
    "Electron/44.10.0 Safari/537.36"
)

AUTH_MAX_AGE = 12 * 3600

STREAM_TIERS = [
    (1, "Newcomer", 1),
    (2, "Fledgling", 5),
    (3, "Breakout", 20),
    (4, "Standout", 75),
    (5, "Trendsetter", 150),
    (6, "Headliner", 300),
    (7, "Star", 500),
    (8, "Sensation", 1000),
    (9, "Visionary", 2000),
    (10, "Phenomenon", 5000),
]

OPUS_SILENCE = b"\xf8\xff\xfe"


def _user_id_from_token(token: str) -> str:
    try:
        first = token.split(".", 1)[0]
        pad = "=" * (-len(first) % 4)
        return base64.b64decode(first + pad).decode("utf-8")
    except Exception:
        return ""


def _default_client_state() -> dict:
    return {
        "guild_versions": {},
        "highest_last_message_id": "0",
        "read_state_version": 0,
        "user_guild_settings_version": -1,
        "user_settings_version": -1,
        "private_channels_version": "0",
        "api_code_version": 0,
    }


class Console:
    RED = "\x1b[91m"
    ORANGE = "\x1b[38;5;208m"
    GREEN = "\x1b[92m"
    YELLOW = "\x1b[93m"
    CYAN = "\x1b[96m"
    WHITE = "\x1b[97m"
    GREY = "\x1b[90m"
    MAGENTA = "\x1b[95m"
    RESET = "\x1b[0m"
    RULE = "─" * 48

    def __init__(self) -> None:
        self._log_lock = threading.Lock()
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        if sys.platform == "win32":
            self._enable_ansi()

    @staticmethod
    def _enable_ansi() -> None:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
            in_handle = kernel32.GetStdHandle(-10)
            in_mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(in_handle, ctypes.byref(in_mode)):
                kernel32.SetConsoleMode(in_handle, (in_mode.value & ~0x0040) | 0x0080)
        except Exception:
            pass

    def _write_log(self, text: str) -> None:
        try:
            import re
            clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with self._log_lock:
                if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
                    try:
                        lines = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-1000:]
                        LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    except Exception:
                        pass
                with open(LOG_FILE, "a", encoding="utf-8") as fp:
                    fp.write(f"[{stamp}] {clean}\n")
        except Exception:
            pass

    def _stamp(self) -> str:
        return f"{self.GREY}{time.strftime('%H:%M:%S')}{self.RESET}"

    def _line(self, glyph: str, target: str, detail: str, color: str) -> None:
        msg = f"  {self._stamp()} {glyph} {self.WHITE}{target}{self.RESET} {self.GREY}·{self.RESET} {color}{detail}{self.RESET}"
        print(msg, flush=True)
        self._write_log(f"{target} · {detail}")

    def ok(self, target: str, detail: str) -> None:
        self._line(f"{self.GREEN}✓{self.RESET}", target, detail, self.GREEN)

    def sent(self, target: str, detail: str) -> None:
        self._line(f"{self.CYAN}↑{self.RESET}", target, detail, self.CYAN)

    def live(self, target: str, detail: str) -> None:
        self._line(f"{self.MAGENTA}●{self.RESET}", target, detail, self.MAGENTA)

    def warn(self, target: str, detail: str) -> None:
        self._line(f"{self.YELLOW}!{self.RESET}", target, detail, self.YELLOW)

    def err(self, target: str, detail: str) -> None:
        self._line(f"{self.RED}✗{self.RESET}", target, detail, self.RED)

    def info(self, detail: str) -> None:
        msg = f"  {self._stamp()} {self.GREY}·{self.RESET} {self.WHITE}{detail}{self.RESET}"
        print(msg, flush=True)
        self._write_log(detail)

    def rule(self) -> None:
        print(f"  {self.GREY}{self.RULE}{self.RESET}", flush=True)

    def clear(self) -> None:
        print("\x1b[2J\x1b[3J\x1b[H", end="", flush=True)

    def banner(self) -> None:
        self.clear()
        print()
        print(f"  {self.CYAN}stream maximizer{self.RESET} {self.GREY}·{self.RESET} {self.WHITE}discord stream time badge{self.RESET}")
        print(f"    {self.GREY}gateway + voice + go live streaming engine (anti-ban v2.0){self.RESET}")
        print(flush=True)



CONFIG_FILE = ROOT / "config.json"



@dataclass
class State:
    token: str = ""
    cookie: str = ""
    fingerprint: str = ""
    analytics_token: str = ""
    fetched_at: int = 0
    total_stream_hours_claimed: float = 0.0
    total_stream_sessions: int = 0
    last_guild_id: str = ""
    last_channel_id: str = ""
    viewer_token: str = ""
    streamer_id: str = ""
    auto_start: bool = False
    auto_mode: int = 1

    @classmethod
    def load(cls) -> "State":
        inst = cls()
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if hasattr(inst, k):
                        setattr(inst, k, v)
            except Exception:
                pass
        if CONFIG_FILE.exists():
            try:
                cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if "streamer_token" in cfg and cfg["streamer_token"] is not None:
                    inst.token = str(cfg["streamer_token"]).strip()
                elif "token" in cfg and cfg["token"] is not None:
                    inst.token = str(cfg["token"]).strip()
                if cfg.get("guild_id"):
                    inst.last_guild_id = str(cfg["guild_id"]).strip()
                if cfg.get("channel_id"):
                    inst.last_channel_id = str(cfg["channel_id"]).strip()
                if "viewer_token" in cfg and cfg["viewer_token"] is not None:
                    inst.viewer_token = str(cfg["viewer_token"]).strip()
                if cfg.get("streamer_id"):
                    inst.streamer_id = str(cfg["streamer_id"]).strip()
                if "auto_start" in cfg:
                    inst.auto_start = bool(cfg["auto_start"])
                if "auto_mode" in cfg:
                    try:
                        inst.auto_mode = int(cfg["auto_mode"])
                    except Exception:
                        pass
            except Exception:
                pass
        return inst

    def save(self) -> None:
        existing = {}
        if STATE_FILE.exists():
            try:
                existing = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing.update(asdict(self))
        STATE_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")

        cfg: dict[str, Any] = {
            "streamer_token": self.token,
            "guild_id": self.last_guild_id,
            "channel_id": self.last_channel_id,
            "viewer_token": self.viewer_token,
        }
        if self.streamer_id:
            cfg["streamer_id"] = self.streamer_id
        if self.auto_start:
            cfg["auto_start"] = self.auto_start
            cfg["auto_mode"] = self.auto_mode
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

    @property
    def is_stale(self) -> bool:
        return not self.analytics_token or (time.time() - self.fetched_at) > AUTH_MAX_AGE

    @property
    def has_cookie(self) -> bool:
        return True

    @property
    def has_token(self) -> bool:
        return bool(self.token.strip())

    @property
    def has_viewer_token(self) -> bool:
        return bool(self.viewer_token.strip())


def _local_props() -> dict:
    return {
        "os": "Windows",
        "browser": "Discord Client",
        "release_channel": "stable",
        "client_version": CLIENT_VERSION,
        "os_version": OS_VERSION,
        "os_arch": "x64",
        "app_arch": "x64",
        "system_locale": "en-US",
        "has_client_mods": False,
        "browser_user_agent": USER_AGENT,
        "browser_version": "44.10.0",
        "os_sdk_version": "26100",
        "client_build_number": CLIENT_BUILD_NUMBER,
        "native_build_number": NATIVE_BUILD_NUMBER,
        "client_event_source": None,
        "client_app_state": "focused",
        "is_fast_connect": False,
        "gateway_connect_reasons": "AppSkeleton",
    }


def build_super_props(launch_signature: str, heartbeat_session: str) -> tuple[str, dict]:
    try:
        req = urllib.request.Request(
            PROPERTIES_URL,
            method="POST",
            data=b"{}",
            headers={"content-type": "application/json", "user-agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            props = json.loads(resp.read().decode("utf-8"))["properties"]
    except Exception:
        props = _local_props()
    props["client_launch_id"] = str(uuid.uuid4())
    props["launch_signature"] = launch_signature
    props["client_heartbeat_session_id"] = heartbeat_session
    props.setdefault("is_fast_connect", False)
    props.setdefault("gateway_connect_reasons", "AppSkeleton")
    raw = json.dumps(props, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii"), props


def _read_analytics_token(auth_token: str, super_props: str) -> str:
    req = urllib.request.Request(
        ME_URL,
        headers={
            "authorization": auth_token,
            "user-agent": USER_AGENT,
            "x-super-properties": super_props,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"token rejected (HTTP {exc.code})") from exc
    token = data.get("analytics_token")
    if not token:
        raise RuntimeError("no analytics_token returned by discord")
    return token


def ensure_token(state: State, super_props: str) -> None:
    if not state.is_stale:
        return
    state.analytics_token = _read_analytics_token(state.token, super_props)
    state.fetched_at = int(time.time())
    state.save()


@dataclass(frozen=True)
class Session:
    heartbeat_session: str
    launch_signature: str
    super_props: str
    props_dict: dict

    @classmethod
    def new(cls) -> "Session":
        hb = str(uuid.uuid4())
        sig = str(uuid.uuid4())
        b64_props, raw_props = build_super_props(sig, hb)
        return cls(hb, sig, b64_props, raw_props)


class MiniWebSocket:
    def __init__(self, host: str, port: int = 443, path: str = "/") -> None:
        host_clean = host.replace("wss://", "").replace("https://", "").rstrip("/")
        if ":" in host_clean:
            h, p = host_clean.split(":", 1)
            self.host = h
            try:
                self.port = int(p)
            except ValueError:
                self.port = port
        else:
            self.host = host_clean
            self.port = port

        self.path = path
        self.sock: ssl.SSLSocket | None = None
        self.closed = True
        self._lock = threading.RLock()

    def connect(self) -> None:
        raw_sock = socket.create_connection((self.host, self.port), timeout=15)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw_sock, server_hostname=self.host)
        sec_key = base64.b64encode(os.urandom(16)).decode("ascii")

        host_hdr = self.host if self.port == 443 else f"{self.host}:{self.port}"
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {host_hdr}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: {USER_AGENT}\r\n\r\n"
        )
        self.sock.sendall(req.encode("utf-8"))

        header_data = b""
        while b"\r\n\r\n" not in header_data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("failed handshake")
            header_data += chunk

        first_line = header_data.split(b"\r\n")[0].decode("utf-8", "ignore")
        if "101" not in first_line:
            raise ConnectionError(f"handshake rejected: {first_line}")
        self.closed = False

    def send(self, data: str) -> None:
        with self._lock:
            if self.closed or not self.sock:
                raise ConnectionError("socket closed")
            try:
                payload = data.encode("utf-8")
                length = len(payload)
                mask_key = os.urandom(4)

                header = bytearray([0x81])
                if length <= 125:
                    header.append(0x80 | length)
                elif length <= 65535:
                    header.append(0x80 | 126)
                    header.extend(length.to_bytes(2, "big"))
                else:
                    header.append(0x80 | 127)
                    header.extend(length.to_bytes(8, "big"))

                header.extend(mask_key)
                masked = bytearray(payload[i] ^ mask_key[i % 4] for i in range(length))
                self.sock.sendall(header + masked)
            except Exception as e:
                self.close()
                raise ConnectionError(f"socket write error: {e}") from e

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed during read")
            buf.extend(chunk)
        return bytes(buf)

    def recv(self, timeout: float = 5.0) -> dict | None:
        if self.closed or not self.sock:
            return None
        try:
            r, _, _ = select.select([self.sock], [], [], timeout)
            if not r:
                return None
            head = self._recv_exact(2)
            opcode = head[0] & 0x0F
            length = head[1] & 0x7F
            if length == 126:
                ext = self._recv_exact(2)
                length = int.from_bytes(ext, "big")
            elif length == 127:
                ext = self._recv_exact(8)
                length = int.from_bytes(ext, "big")
            payload = self._recv_exact(length) if length > 0 else b""
        except Exception:
            self.close()
            return None

        if opcode == 0x8:
            code = 0
            reason = ""
            if len(payload) >= 2:
                code = struct.unpack(">H", payload[:2])[0]
                reason = payload[2:].decode("utf-8", "ignore")
            self.close()
            return {"op": "close", "code": code, "reason": reason}

        if opcode == 0x9:
            try:
                with self._lock:
                    if self.sock and not self.closed:
                        plen = len(payload)
                        mask_key = os.urandom(4)
                        pong = bytearray([0x8A, 0x80 | plen])
                        pong.extend(mask_key)
                        pong.extend(payload[i] ^ mask_key[i % 4] for i in range(plen))
                        self.sock.sendall(pong)
            except Exception:
                self.close()
            return {"op": "ping"}

        if opcode == 0xA:
            return {"op": "pong"}

        if opcode in (0x0, 0x2):
            return {"op": "binary", "len": len(payload)}

        try:
            return json.loads(payload.decode("utf-8"))
        except Exception:
            return {"op": "raw"}

    def close(self) -> None:
        self.closed = True
        with self._lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None


class VoiceConnection:
    def __init__(
        self,
        endpoint: str,
        server_id: str,
        user_id: str,
        session_id: str,
        token: str,
        console: Console,
        label: str = "voice",
        is_stream: bool = False,
        stream_key: str = "",
    ) -> None:
        cleaned = endpoint.replace("wss://", "").replace("https://", "").rstrip("/")
        if ":" in cleaned:
            self.host, port_str = cleaned.split(":", 1)
            try:
                self.port = int(port_str)
            except ValueError:
                self.port = 443
        else:
            self.host = cleaned
            self.port = 443
        self.endpoint = f"{self.host}:{self.port}"
        self.server_id = server_id
        self.user_id = user_id
        self.session_id = session_id
        self.token = token
        self.console = console
        self.label = label
        self.is_stream = is_stream
        self.stream_key = stream_key

        self.ws: MiniWebSocket | None = None
        self.udp_sock: socket.socket | None = None
        self.ssrc: int = 0
        self.voice_ip: str = ""
        self.voice_port: int = 0
        self.external_ip: str = ""
        self.external_port: int = 0
        self.secret_key: bytes = b""
        self.encryption_mode: str = ""
        self.heartbeat_interval: float = 13.75
        self.running: bool = False
        self.nonce_counter: int = 0
        self._hb_thread: threading.Thread | None = None
        self._audio_thread: threading.Thread | None = None

    def connect(self) -> bool:
        C = self.console

        try:
            self.ws = MiniWebSocket(self.host, port=self.port, path="/?v=7")
            self.ws.connect()
        except Exception as exc:
            C.err(self.label, f"ws connect failed: {exc}")
            return False

        msg = self._recv_op(8, timeout=10)
        if not msg:
            C.err(self.label, "no Hello from voice server")
            self.close()
            return False
        self.heartbeat_interval = msg.get("d", {}).get("heartbeat_interval", 13750) / 1000.0

        identify_d: dict[str, Any] = {
            "server_id": self.server_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "token": self.token,
            "max_dave_protocol_version": 1,
        }
        if self.is_stream:
            if self.stream_key:
                identify_d["stream_key"] = self.stream_key
            identify_d["video"] = True
            identify_d["streams"] = [
                {"type": "screen", "rid": "100", "quality": 100}
            ]
        self.ws.send(json.dumps({"op": 0, "d": identify_d}))

        msg = self._recv_op(2, timeout=10)
        if not msg:
            C.err(self.label, "no Ready from voice server")
            self.close()
            return False

        d = msg.get("d", {})
        self.ssrc = d.get("ssrc", 0)
        self.voice_ip = d.get("ip", "")
        self.voice_port = d.get("port", 0)
        modes = d.get("modes", [])

        preferred = [
            "aead_aes256_gcm_rtpsize",
            "aead_xchacha20_poly1305_rtpsize",
            "xsalsa20_poly1305_lite",
            "xsalsa20_poly1305",
        ]
        for mode in preferred:
            if mode in modes:
                self.encryption_mode = mode
                break
        if not self.encryption_mode and modes:
            self.encryption_mode = modes[0]

        if not self._udp_discovery():
            C.warn(self.label, "UDP IP discovery failed — aborting voice handshake to avoid invalid IP leak")
            self.close()
            return False

        self.ws.send(json.dumps({
            "op": 1,
            "d": {
                "protocol": "udp",
                "data": {
                    "address": self.external_ip,
                    "port": self.external_port,
                    "mode": self.encryption_mode,
                },
            },
        }))

        msg = self._recv_op(4, timeout=10)
        if msg:
            d = msg.get("d", {})
            self.secret_key = bytes(d.get("secret_key", []))
            self.encryption_mode = d.get("mode", self.encryption_mode)

        self.running = True
        return True

    def _recv_op(self, target_op: int, timeout: float = 10) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            msg = self.ws.recv(timeout=min(remaining, 2.0))
            if msg is None:
                if self.ws.closed:
                    return None
                continue
            if msg.get("op") == "close":
                C = self.console
                code = msg.get("code")
                reason = msg.get("reason")
                C.err(self.label, f"voice server closed: {code} ({reason})")
                return None
            if msg.get("op") == target_op:
                return msg
            if msg.get("op") == 3:
                self._send_heartbeat()
        return None

    def _send_heartbeat(self) -> None:
        if self.ws and not self.ws.closed:
            try:
                nonce = int(time.time() * 1000)
                self.ws.send(json.dumps({"op": 3, "d": nonce}))
            except Exception:
                pass

    def _udp_discovery(self) -> bool:
        for _ in range(3):
            try:
                if self.udp_sock:
                    try:
                        self.udp_sock.close()
                    except Exception:
                        pass
                self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.udp_sock.settimeout(3.0)

                packet = bytearray(74)
                struct.pack_into(">HHI", packet, 0, 0x0001, 70, self.ssrc)

                self.udp_sock.sendto(bytes(packet), (self.voice_ip, self.voice_port))

                data, _ = self.udp_sock.recvfrom(256)
                if len(data) < 74:
                    continue

                ip_start = 8
                ip_end = data.index(0, ip_start)
                self.external_ip = data[ip_start:ip_end].decode("utf-8")
                self.external_port = struct.unpack_from(">H", data, 72)[0]

                if self.external_ip and self.external_ip != "0.0.0.0":
                    return True
            except Exception:
                pass
            time.sleep(0.4)

        if self.udp_sock:
            try:
                self.udp_sock.close()
            except Exception:
                pass
            self.udp_sock = None
        return False

    def start_heartbeat(self) -> None:
        def _loop():
            last_hb = 0.0
            while self.running and self.ws and not self.ws.closed:
                now = time.time()
                if now - last_hb >= max(2.0, self.heartbeat_interval - 0.5):
                    self._send_heartbeat()
                    last_hb = now
                try:
                    msg = self.ws.recv(timeout=1.0)
                    if msg is None:
                        if self.ws.closed:
                            self.running = False
                            break
                        continue
                    if isinstance(msg, dict):
                        if msg.get("op") == "close":
                            self.console.warn(self.label, f"voice ws closed: {msg.get('code')} ({msg.get('reason')})")
                            self.running = False
                            break
                        if msg.get("op") == 8:
                            self.heartbeat_interval = msg.get("d", {}).get("heartbeat_interval", 13750) / 1000.0
                except Exception:
                    if not self.ws or self.ws.closed:
                        self.running = False
                        break
            self.running = False

        self._hb_thread = threading.Thread(target=_loop, daemon=True)
        self._hb_thread.start()

    def send_speaking(self, speaking: bool = True) -> None:
        if self.ws and not self.ws.closed:
            try:
                self.ws.send(json.dumps({
                    "op": 5,
                    "d": {
                        "speaking": 1 if speaking else 0,
                        "delay": 0,
                        "ssrc": self.ssrc,
                    },
                }))
            except Exception:
                pass

    def send_udp_keepalive_only(self) -> None:
        if not self.udp_sock:
            return

        def _keepalive():
            seq = 0
            while self.running and self.udp_sock:
                try:
                    pkt = struct.pack(">I", seq) + b"\xc9"
                    self.udp_sock.sendto(pkt, (self.voice_ip, self.voice_port))
                    seq = (seq + 1) & 0xFFFF
                except Exception:
                    pass
                time.sleep(random.uniform(4.5, 6.0))

        self._audio_thread = threading.Thread(target=_keepalive, daemon=True)
        self._audio_thread.start()

    def start_silence_pump(self) -> None:
        if not self.udp_sock or not self.secret_key:
            return

        def _pump():
            seq = 0
            ts = 0
            while self.running:
                try:
                    if HAS_CRYPTO and "aes256_gcm" in self.encryption_mode:
                        self._send_encrypted_silence_gcm(seq, ts)
                    else:
                        keepalive = struct.pack(">I", seq) + b"\xc9"
                        try:
                            self.udp_sock.sendto(keepalive, (self.voice_ip, self.voice_port))
                        except Exception:
                            pass
                    seq = (seq + 1) & 0xFFFF
                    ts = (ts + 960) & 0xFFFFFFFF
                    time.sleep(random.uniform(0.95, 1.15))
                except Exception:
                    break

        self._audio_thread = threading.Thread(target=_pump, daemon=True)
        self._audio_thread.start()

    def _send_encrypted_silence_gcm(self, seq: int, ts: int) -> None:
        if not self.udp_sock or not self.secret_key:
            return
        try:
            rtp_header = struct.pack(
                ">BBHII",
                0x80,
                0x78,
                seq & 0xFFFF,
                ts & 0xFFFFFFFF,
                self.ssrc,
            )

            self.nonce_counter += 1
            nonce = struct.pack(">I", self.nonce_counter) + b"\x00" * 8

            cipher = AESGCM(self.secret_key[:32])
            encrypted = cipher.encrypt(nonce, OPUS_SILENCE, rtp_header)

            packet = rtp_header + encrypted + struct.pack(">I", self.nonce_counter)
            self.udp_sock.sendto(packet, (self.voice_ip, self.voice_port))
        except Exception:
            pass

    def close(self) -> None:
        self.running = False
        if self.udp_sock:
            try:
                self.udp_sock.close()
            except Exception:
                pass
            self.udp_sock = None
        if self.ws:
            self.ws.close()


class ScienceTelemetry:
    def __init__(self, token: str, console: Console, turbo: bool = False):
        self.token = token.strip()
        self.console = console
        self.turbo = turbo
        self.analytics_token = ""
        self.stream_start_time = 0.0
        self.heartbeat_session_id = ""
        self.launch_signature = ""
        self.sequence = random.randint(1000, 50000)
        self.running = False
        self._thread: threading.Thread | None = None
        self.credited_hours = 0.0

    def fetch_analytics_token(self) -> str:
        if self.analytics_token:
            return self.analytics_token
        req = urllib.request.Request(ME_URL, headers={
            "authorization": self.token,
            "user-agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.analytics_token = data.get("analytics_token", "")
        except Exception:
            pass
        return self.analytics_token

    def post_events(self, events: list[dict]) -> bool:
        token = self.fetch_analytics_token()
        if not token:
            return False
        now = int(time.time() * 1000)
        payload = {
            "token": token,
            "events": [
                {
                    "type": ev["type"],
                    "properties": {
                        "client_track_timestamp": now,
                        "client_send_timestamp": now,
                        "client_heartbeat_session_id": self.heartbeat_session_id,
                        "launch_signature": self.launch_signature,
                        "event_sequence_number": self._next_seq(),
                        "client_app_state": "focused",
                        **ev.get("properties", {}),
                    },
                }
                for ev in events
            ],
        }
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            SCIENCE_URL,
            data=data_bytes,
            headers={
                "authorization": self.token,
                "content-type": "application/json",
                "origin": "https://discord.com",
                "referer": "https://discord.com/channels/@me",
                "user-agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    def _next_seq(self) -> int:
        self.sequence += 1
        return self.sequence

    def start_stream(self, guild_id: str, channel_id: str) -> None:
        if self.running:
            return
        self.running = True
        self.stream_start_time = time.time()
        self.heartbeat_session_id = str(uuid.uuid4())
        self.launch_signature = str(uuid.uuid4())
        props = {"guild_id": str(guild_id), "channel_id": str(channel_id), "channel_type": 2}
        self.post_events([
            {"type": "media_session_joined", "properties": props},
            {"type": "video_stream_started", "properties": props},
        ])
        mode_str = "Turbo (+5ч/мин)" if self.turbo else "Real-Time (1:1)"
        self.console.ok("science", f"телеметрия стриминга Discord активирована ({mode_str})")

        def _loop():
            while self.running:
                time.sleep(60.0)
                if not self.running:
                    break
                if self.turbo:
                    claim_hours = 5.0
                    self.credited_hours += claim_hours
                    duration_tracked_ms = int(claim_hours * 3600000)
                    total_duration_ms = int(self.credited_hours * 3600000)
                else:
                    self.credited_hours += (1.0 / 60.0)
                    duration_tracked_ms = 60000
                    total_duration_ms = int((time.time() - self.stream_start_time) * 1000)

                ok = self.post_events([
                    {
                        "type": "video_stream_heartbeat",
                        "properties": {
                            "guild_id": str(guild_id),
                            "channel_id": str(channel_id),
                            "channel_type": 2,
                            "duration_tracked_ms": duration_tracked_ms,
                            "total_duration_ms": total_duration_ms,
                        },
                    }
                ])
                if ok:
                    self.console.live("science", f"heartbeat аналитики отправлен (учтено: {self.credited_hours:.1f}ч)")

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop_stream(self, guild_id: str, channel_id: str) -> None:
        if not self.running:
            return
        self.running = False
        duration_ms = int((time.time() - self.stream_start_time) * 1000) if self.stream_start_time else 0
        props = {
            "guild_id": str(guild_id),
            "channel_id": str(channel_id),
            "channel_type": 2,
            "duration_ms": duration_ms,
        }
        self.post_events([{"type": "video_stream_ended", "properties": props}])
        self.console.info(f"{self.console.CYAN}science{self.console.RESET} · телеметрия стрима завершена")


class AutoViewer:
    def __init__(self, token: str, guild_id: str, channel_id: str, console: Console, target_user_id: str = ""):
        self.token = token.strip()
        self.guild_id = str(guild_id)
        self.channel_id = str(channel_id)
        self.console = console
        self.target_user_id = str(target_user_id).strip()
        self.user_id = _user_id_from_token(self.token)
        self.session = Session.new()
        self.running = False
        self.ws: MiniWebSocket | None = None
        self.voice_conn: VoiceConnection | None = None
        self.thread: threading.Thread | None = None
        self.heartbeat_interval = 41.25
        self.sequence: int | None = None
        self.session_id = ""
        self.voice_session_id = ""
        self.voice_token = ""
        self.voice_endpoint = ""
        self.voice_connected = False
        self.default_stream_key = f"guild:{self.guild_id}:{self.channel_id}:{self.target_user_id}" if self.target_user_id else ""
        self.stream_key = self.default_stream_key
        self._watched_stream_key = ""
        self.is_watching = False
        self.watch_start_time = 0.0
        self._last_watch_attempt = 0.0
        self.username = ""
        self.conflict_paused = False
        self._voice_join_times: list[float] = []
        self._own_sessions: set[str] = set()
        self._watch_lock = threading.Lock()
        self.stream_rtc_ws: MiniWebSocket | None = None
        self.stream_rtc_connected = False
        self.stream_rtc_server_id = ""
        self._stream_hb_thread: threading.Thread | None = None
        self._last_join_attempt = 0.0

    def start(self, stream_key: str = "") -> None:
        if not self.token or self.running:
            return
        if stream_key:
            self.stream_key = stream_key
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _send_voice_state_join(self) -> None:
        if not self.ws or self.ws.closed or not self.running:
            return
        now = time.time()
        self._last_join_attempt = now
        self._voice_join_times = [t for t in self._voice_join_times if now - t < 300.0]
        if len(self._voice_join_times) >= 6:
            cooldown = random.uniform(20.0, 35.0)
            self.console.warn(
                "anti-ban",
                f"Защита от частых перезаходов в войс: пауза {cooldown:.0f} сек. для безопасности аккаунта..."
            )
            for _ in range(int(cooldown * 2)):
                if not self.running or not self.ws or self.ws.closed:
                    return
                time.sleep(0.5)
            self._voice_join_times.clear()

        self._voice_join_times.append(time.time())
        try:
            self.ws.send(json.dumps({
                "op": 4,
                "d": {
                    "guild_id": self.guild_id,
                    "channel_id": None,
                    "self_mute": True,
                    "self_deaf": True,
                    "self_video": False,
                }
            }))
            time.sleep(0.4)
            self.ws.send(json.dumps({
                "op": 4,
                "d": {
                    "guild_id": self.guild_id,
                    "channel_id": self.channel_id,
                    "self_mute": True,
                    "self_deaf": True,
                    "self_video": False,
                }
            }))
        except Exception:
            pass

    def _send_watch_stream(self, force: bool = False) -> None:
        if not self.stream_key and self.default_stream_key:
            self.stream_key = self.default_stream_key
        if not self.stream_key or not self.ws or self.ws.closed or not self.voice_connected:
            return
        if not force and self.is_watching and self._watched_stream_key == self.stream_key:
            return
        if not self._watch_lock.acquire(blocking=False):
            return
        try:
            self._last_watch_attempt = time.time()
            time.sleep(random.uniform(0.6, 1.1))
            if not self.stream_key or not self.ws or self.ws.closed or not self.voice_connected:
                return
            self.ws.send(json.dumps({
                "op": 20,
                "d": {
                    "stream_key": self.stream_key
                }
            }))
            self._watched_stream_key = self.stream_key
            self.is_watching = True
            self.watch_start_time = time.time()
            self.console.live("alt-viewer", f"твинк смотрит трансляцию ({self.stream_key})")
        except Exception:
            pass
        finally:
            self._watch_lock.release()

    def _connect_voice_server(self, endpoint: str, token: str) -> None:
        C = self.console
        if self.voice_conn:
            try:
                self.voice_conn.close()
            except Exception:
                pass
            self.voice_conn = None
            self.voice_connected = False

        self.voice_token = token
        self.voice_endpoint = endpoint
        session_id = self.session_id or self.voice_session_id

        self.voice_conn = VoiceConnection(
            endpoint=endpoint,
            server_id=self.guild_id,
            user_id=self.user_id,
            session_id=session_id,
            token=token,
            console=C,
            label="alt-voice",
            is_stream=False,
        )

        C.info(f"{C.CYAN}alt-voice{C.RESET} · подключение к голосовому серверу...")
        if self.voice_conn.connect():
            self.voice_conn.start_heartbeat()
            self.voice_conn.send_udp_keepalive_only()
            self.voice_connected = True
            C.ok("alt-voice", f"твинк закреплён в голосовом канале (ssrc={self.voice_conn.ssrc})")
            if not self.stream_key and self.default_stream_key:
                self.stream_key = self.default_stream_key
            if self.stream_key:
                threading.Thread(target=self._send_watch_stream, args=(True,), daemon=True).start()
            else:
                C.ok("alt-viewer", "твинк в голосовом канале, ожидает запуск стрима...")
        else:
            C.warn("alt-voice", "не удалось установить связь с голосовым сервером твинка")

    def _connect_stream_rtc(self, endpoint: str, token: str) -> None:
        C = self.console
        self._close_stream_rtc()
        if not endpoint or not token:
            return
        cleaned = endpoint.replace("wss://", "").replace("https://", "").rstrip("/")
        if ":" in cleaned:
            host, port_str = cleaned.split(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                port = 443
        else:
            host = cleaned
            port = 443

        try:
            ws = MiniWebSocket(host, port=port, path="/?v=8")
            ws.connect()
            self.stream_rtc_ws = ws

            hello = ws.recv(timeout=5)
            if not hello or hello.get("op") != 8:
                self._close_stream_rtc()
                return
            hb_interval = hello.get("d", {}).get("heartbeat_interval", 13750) / 1000.0

            rtc_server_id = self.stream_rtc_server_id or self.guild_id
            session_id = self.session_id or self.voice_session_id
            identify = {
                "op": 0,
                "d": {
                    "server_id": rtc_server_id,
                    "user_id": self.user_id,
                    "session_id": session_id,
                    "token": token,
                    "video": True,
                    "max_dave_protocol_version": 1,
                    "streams": [
                        {
                            "type": "video",
                            "rid": "100",
                            "quality": 100,
                        }
                    ],
                },
            }
            ws.send(json.dumps(identify))
            ready = ws.recv(timeout=5)
            if ready and ready.get("op") == 2:
                self.stream_rtc_connected = True
                d_ready = ready.get("d", {})
                ssrc = d_ready.get("ssrc", 0)
                C.ok("alt-viewer", f"твинк успешно подключен к WebRTC медиа-потоку стрима (RTC Ready ssrc={ssrc})")

                def _stream_hb():
                    last_hb = 0.0
                    while self.running and self.stream_rtc_ws and not self.stream_rtc_ws.closed:
                        now = time.time()
                        if now - last_hb >= max(2.0, hb_interval * 0.9):
                            try:
                                self.stream_rtc_ws.send(json.dumps({"op": 3, "d": int(time.time() * 1000)}))
                                last_hb = now
                            except Exception:
                                break
                        try:
                            msg = self.stream_rtc_ws.recv(timeout=1.0)
                            if msg and msg.get("op") == "close":
                                break
                        except Exception:
                            pass
                    self.stream_rtc_connected = False

                self._stream_hb_thread = threading.Thread(target=_stream_hb, daemon=True)
                self._stream_hb_thread.start()
            else:
                self._close_stream_rtc()
        except Exception:
            self._close_stream_rtc()

    def _close_stream_rtc(self) -> None:
        self.stream_rtc_connected = False
        if self.stream_rtc_ws:
            try:
                self.stream_rtc_ws.close()
            except Exception:
                pass
            self.stream_rtc_ws = None

    def _check_guild_active_streams(self, d: dict) -> None:
        g_id = str(d.get("id", ""))
        if g_id and g_id != self.guild_id:
            return
        voice_states = d.get("voice_states") or []
        for vs in voice_states:
            u_id = str(vs.get("user_id", ""))
            ch_id = str(vs.get("channel_id", ""))
            if ch_id == self.channel_id and vs.get("self_stream") and u_id and u_id != self.user_id:
                self.stream_key = f"guild:{self.guild_id}:{self.channel_id}:{u_id}"
                if self.voice_connected:
                    threading.Thread(target=self._send_watch_stream, args=(True,), daemon=True).start()
                break

    def _run(self) -> None:
        C = self.console
        backoff = 4.0
        can_resume = False

        while self.running:
            try:
                C.info(f"{C.CYAN}alt-viewer{C.RESET} · подключение твинка к шлюзу Discord...")
                self.ws = MiniWebSocket(GATEWAY_URL, path="/?v=9&encoding=json")
                self.ws.connect()
                last_hb = time.time()
                identified = False

                while self.running and self.ws and not self.ws.closed:
                    now = time.time()
                    if identified and (now - last_hb > self.heartbeat_interval):
                        try:
                            self.ws.send(json.dumps({"op": 1, "d": self.sequence}))
                            last_hb = now
                        except Exception:
                            break

                    if self.voice_connected and self.voice_conn and not self.voice_conn.running and not self.conflict_paused:
                        C.warn("alt-voice", "голосовой сокет закрылся, выполняем плавный перезаход...")
                        self.voice_connected = False
                        self.is_watching = False
                        self._watched_stream_key = ""
                        try:
                            self.voice_conn.close()
                        except Exception:
                            pass
                        self.voice_conn = None
                        threading.Thread(target=self._send_voice_state_join, daemon=True).start()

                    if (
                        self.voice_connected
                        and self.stream_key
                        and not self.is_watching
                        and not self.conflict_paused
                        and (now - self._last_watch_attempt >= 15.0)
                    ):
                        threading.Thread(target=self._send_watch_stream, args=(True,), daemon=True).start()

                    if (
                        identified
                        and not self.voice_connected
                        and not self.conflict_paused
                        and (now - self._last_join_attempt >= 18.0)
                    ):
                        threading.Thread(target=self._send_voice_state_join, daemon=True).start()

                    msg = self.ws.recv(timeout=1.0)
                    if not msg:
                        continue

                    if isinstance(msg, dict) and msg.get("op") == "close":
                        code = msg.get("code")
                        reason = msg.get("reason", "")
                        C.warn("alt-viewer", f"шлюз закрыл соединение (код {code}: {reason})")
                        if code == 4004:
                            C.err("alt-viewer", "КРИТИЧЕСКАЯ ОШИБКА (4004): Токен твинка недействителен или был сброшен! Остановка во избежание бана.")
                            self.running = False
                            return
                        if code in (4010, 4011, 4012, 4013, 4014):
                            C.err("alt-viewer", f"КРИТИЧЕСКАЯ ОШИБКА шлюза ({code}): остановка.")
                            self.running = False
                            return
                        can_resume = True
                        break

                    op = msg.get("op")
                    seq = msg.get("s")
                    t = msg.get("t")
                    d = msg.get("d") or {}

                    if seq is not None:
                        self.sequence = seq

                    if op == 10:
                        self.heartbeat_interval = d.get("heartbeat_interval", 41250) / 1000.0
                        last_hb = time.time()
                        self.ws.send(json.dumps({"op": 1, "d": self.sequence}))

                        if can_resume and self.session_id and self.sequence is not None:
                            C.info(f"{C.CYAN}alt-viewer{C.RESET} · восстановление сессии (Op 6 Resume)...")
                            self.ws.send(json.dumps({
                                "op": 6,
                                "d": {
                                    "token": self.token,
                                    "session_id": self.session_id,
                                    "seq": self.sequence,
                                }
                            }))
                        else:
                            self.ws.send(json.dumps({
                                "op": 2,
                                "d": {
                                    "token": self.token,
                                    "capabilities": 16381,
                                    "properties": self.session.props_dict,
                                    "presence": {
                                        "status": "online",
                                        "since": 0,
                                        "activities": [],
                                        "afk": False,
                                    },
                                    "compress": False,
                                    "client_state": _default_client_state(),
                                }
                            }))
                        identified = True
                        backoff = 4.0

                    elif op == 1:
                        self.ws.send(json.dumps({"op": 1, "d": self.sequence}))
                        last_hb = time.time()

                    elif op == 11:
                        last_hb = time.time()

                    elif op == 7:
                        C.warn("alt-viewer", "Discord запросил переподключение (Op 7 Reconnect)...")
                        can_resume = True
                        break

                    elif op == 9:
                        resumable = bool(d)
                        C.warn("alt-viewer", f"сессия устарела (Op 9, resumable={resumable})...")
                        can_resume = resumable
                        if not resumable:
                            self.session_id = ""
                            self.sequence = None
                        time.sleep(random.uniform(2.0, 4.5))
                        break

                    elif op == 0:
                        if t == "READY":
                            u = d.get("user") or {}
                            self.username = u.get("username", "alt")
                            self.user_id = str(u.get("id") or self.user_id)
                            self.session_id = d.get("session_id", "")
                            if self.session_id:
                                self._own_sessions.add(self.session_id)
                            self.voice_session_id = self.session_id
                            self.conflict_paused = False
                            C.ok("alt-viewer", f"твинк авторизован ({self.username})")
                            try:
                                self.ws.send(json.dumps({
                                    "op": 14,
                                    "d": {
                                        "guild_id": self.guild_id,
                                        "typing": True,
                                        "threads": False,
                                        "activities": True,
                                    }
                                }))
                            except Exception:
                                pass
                            for g in (d.get("guilds") or []):
                                if isinstance(g, dict):
                                    self._check_guild_active_streams(g)

                            def _delayed_join():
                                time.sleep(random.uniform(1.2, 2.2))
                                if self.running and not self.conflict_paused:
                                    self._send_voice_state_join()
                            threading.Thread(target=_delayed_join, daemon=True).start()

                        elif t == "READY_SUPPLEMENTAL":
                            for g in (d.get("guilds") or []):
                                if isinstance(g, dict):
                                    self._check_guild_active_streams(g)

                        elif t == "RESUMED":
                            C.ok("alt-viewer", "сессия твинка успешно восстановлена (RESUMED)")
                            if not self.voice_connected and not self.conflict_paused:
                                def _resumed_join():
                                    time.sleep(1.0)
                                    self._send_voice_state_join()
                                threading.Thread(target=_resumed_join, daemon=True).start()

                        elif t == "GUILD_CREATE":
                            self._check_guild_active_streams(d)

                        elif t == "VOICE_STATE_UPDATE":
                            u_id = str(d.get("user_id", ""))
                            if u_id == self.user_id:
                                active_ch = d.get("channel_id")
                                sess = d.get("session_id", "")

                                if sess and self.session_id and sess != self.session_id and sess not in self._own_sessions:
                                    if active_ch is not None:
                                        if not self.conflict_paused:
                                            self.conflict_paused = True
                                            self.voice_connected = False
                                            self.is_watching = False
                                            self._watched_stream_key = ""
                                            if self.voice_conn:
                                                self.voice_conn.close()
                                                self.voice_conn = None
                                            C.err(
                                                "anti-ban",
                                                "ОБНАРУЖЕН КОНФЛИКТ СЕССИЙ: Твинк подключён к войсу с другого устройства или VDS!"
                                            )
                                            C.warn(
                                                "anti-ban",
                                                "Локальный перезаход поставлен на паузу, чтобы Discord НЕ заблокировал аккаунт за борьбу сессий."
                                            )
                                        continue
                                    else:
                                        if self.conflict_paused:
                                            C.ok("anti-ban", "Внешняя сессия твинка покинула войс. Возвращаемся через 10 сек...")
                                            self.conflict_paused = False
                                            def _rejoin_after_conflict():
                                                time.sleep(random.uniform(9.0, 14.0))
                                                if self.running and not self.conflict_paused:
                                                    self._send_voice_state_join()
                                            threading.Thread(target=_rejoin_after_conflict, daemon=True).start()
                                        continue

                                if sess and sess != self.session_id and sess in self._own_sessions:
                                    continue

                                if sess:
                                    self.voice_session_id = sess

                                if active_ch is None:
                                    if self.conflict_paused:
                                        continue
                                    if self.voice_connected or self.is_watching:
                                        C.warn("alt-viewer", "твинк был отключен от войса, выполняем безопасный перезаход...")
                                    self.voice_connected = False
                                    self.is_watching = False
                                    self._watched_stream_key = ""
                                    if self.voice_conn:
                                        self.voice_conn.close()
                                        self.voice_conn = None
                                    if self.running:
                                        def _safe_rejoin():
                                            time.sleep(random.uniform(4.0, 7.0))
                                            if self.running and not self.conflict_paused and not self.voice_connected:
                                                self._send_voice_state_join()
                                        threading.Thread(target=_safe_rejoin, daemon=True).start()
                            else:
                                ch = str(d.get("channel_id") or "")
                                if d.get("self_stream") and ch == self.channel_id and u_id != self.user_id:
                                    new_sk = f"guild:{self.guild_id}:{ch}:{u_id}"
                                    if new_sk != self.stream_key or not self.is_watching:
                                        self.stream_key = new_sk
                                        if self.voice_connected:
                                            threading.Thread(target=self._send_watch_stream, args=(True,), daemon=True).start()
                                elif not d.get("self_stream") or ch != self.channel_id:
                                    if self.stream_key and self.stream_key.endswith(f":{u_id}"):
                                        self.stream_key = ""
                                        self._watched_stream_key = ""
                                        if self.is_watching:
                                            self.is_watching = False
                                            C.warn("alt-viewer", "стример завершил трансляцию, твинк ожидает в голосовом канале...")

                        elif t == "VOICE_SERVER_UPDATE":
                            v_guild = str(d.get("guild_id", ""))
                            if v_guild == str(self.guild_id) and not self.conflict_paused:
                                token = d.get("token", "")
                                endpoint = d.get("endpoint") or ""
                                if endpoint and token:
                                    self._connect_voice_server(endpoint, token)

                        elif t == "STREAM_CREATE":
                            sk = d.get("stream_key", "")
                            if d.get("rtc_server_id"):
                                self.stream_rtc_server_id = str(d["rtc_server_id"])
                            if sk and f":{self.channel_id}:" in sk and not sk.endswith(f":{self.user_id}"):
                                self.stream_key = sk
                                if self.voice_connected:
                                    threading.Thread(target=self._send_watch_stream, args=(True,), daemon=True).start()

                        elif t == "STREAM_SERVER_UPDATE":
                            endpoint = d.get("endpoint") or ""
                            token = d.get("token") or ""
                            if d.get("rtc_server_id"):
                                self.stream_rtc_server_id = str(d["rtc_server_id"])
                            if endpoint and token and self.running and not self.conflict_paused:
                                threading.Thread(target=self._connect_stream_rtc, args=(endpoint, token), daemon=True).start()

                        elif t == "STREAM_DELETE":
                            self._close_stream_rtc()
                            del_sk = d.get("stream_key", "")
                            if not self.stream_key or del_sk == self.stream_key:
                                self.stream_key = ""
                                self._watched_stream_key = ""
                                if self.is_watching:
                                    self.is_watching = False
                                    C.warn("alt-viewer", "стрим пока не активен или завершён, твинк ждёт запуска стрима в канале...")

            except Exception as exc:
                if self.running:
                    C.warn("alt-viewer", f"сбой соединения твинка: {exc}, повтор через {backoff:.0f}с...")

            self._close_stream_rtc()
            if self.voice_conn:
                try:
                    self.voice_conn.close()
                except Exception:
                    pass
                self.voice_conn = None
            self.voice_connected = False
            self.is_watching = False
            self._watched_stream_key = ""
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass
                self.ws = None
            if self.running:
                sleep_t = backoff + random.uniform(0.5, 2.5)
                time.sleep(sleep_t)
                backoff = min(30.0, backoff * 1.5)

    def stop(self) -> None:
        self.running = False
        self.is_watching = False
        self.voice_connected = False
        self._watched_stream_key = ""
        self._close_stream_rtc()
        if self.voice_conn:
            try:
                self.voice_conn.close()
            except Exception:
                pass
            self.voice_conn = None
        if self.ws and not self.ws.closed:
            try:
                self.ws.send(json.dumps({
                    "op": 4,
                    "d": {
                        "guild_id": self.guild_id,
                        "channel_id": None,
                        "self_mute": True,
                        "self_deaf": True,
                    }
                }))
            except Exception:
                pass
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None


PERMISSION_ADMINISTRATOR = 1 << 3
PERMISSION_STREAM        = 1 << 9
PERMISSION_VIEW_CHANNEL  = 1 << 10
PERMISSION_CONNECT       = 1 << 20

def _discord_api_get(url: str, token: str, super_props: str = "") -> tuple[int, dict]:
    headers = {
        "authorization": token,
        "user-agent": USER_AGENT,
    }
    if super_props:
        headers["x-super-properties"] = super_props
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return resp.status, data
    except urllib.error.HTTPError as exc:
        try:
            err_data = json.loads(exc.read().decode("utf-8"))
        except Exception:
            err_data = {"message": str(exc.reason)}
        return exc.code, err_data
    except Exception as exc:
        return 0, {"message": str(exc)}


def validate_stream_target(
    token: str,
    super_props: str,
    guild_id: str,
    channel_id: str,
    console: Console,
) -> tuple[bool, str]:
    C = console

    if not guild_id.isdigit():
        C.err("server", f"invalid server ID format: '{guild_id}' (must be numeric digits)")
        return False, "invalid server id format"
    if not channel_id.isdigit():
        C.err("channel", f"invalid channel ID format: '{channel_id}' (must be numeric digits)")
        return False, "invalid channel id format"

    status, user_data = _discord_api_get("https://discord.com/api/v9/users/@me", token, super_props)
    if status == 401:
        C.err("auth", "authentication failed: your discord token is invalid or has expired (HTTP 401).")
        C.info(f"{C.GREY}please update your token using Option 3 in the menu.{C.RESET}")
        return False, "unauthorized"
    if status != 200:
        C.err("auth", f"could not reach discord API (HTTP {status}: {user_data.get('message', 'network error')})")
        return False, "api error"

    g_status, g_data = _discord_api_get(f"https://discord.com/api/v9/guilds/{guild_id}", token, super_props)
    if g_status in (403, 404):
        ug_status, ug_list = _discord_api_get("https://discord.com/api/v9/users/@me/guilds", token, super_props)
        guild_found = None
        if ug_status == 200 and isinstance(ug_list, list):
            guild_found = next((g for g in ug_list if str(g.get("id")) == str(guild_id)), None)

        if not guild_found:
            C.err("server", f"server not found ({guild_id})")
            C.err("server", "either this server does not exist or your account is not a member of it.")
            return False, "server not found"
        guild_name = guild_found.get("name", "Server")
    elif g_status != 200:
        C.err("server", f"server lookup failed (HTTP {g_status}: {g_data.get('message', 'unknown error')})")
        return False, "server lookup error"
    else:
        guild_name = g_data.get("name", "Server")

    C.ok("server", f"server verified: {C.WHITE}{guild_name}{C.RESET} ({guild_id})")

    ch_status, ch_data = _discord_api_get(f"https://discord.com/api/v9/channels/{channel_id}", token, super_props)
    if ch_status in (403, 404):
        C.err("channel", f"channel not found ({channel_id})")
        C.err("channel", "either this channel does not exist or your account lacks access to view it.")
        return False, "channel not found"
    elif ch_status != 200:
        C.err("channel", f"channel lookup failed (HTTP {ch_status}: {ch_data.get('message', 'unknown error')})")
        return False, "channel lookup error"

    ch_name = ch_data.get("name", "channel")
    ch_guild_id = str(ch_data.get("guild_id", ""))
    ch_type = ch_data.get("type")

    if ch_guild_id and ch_guild_id != str(guild_id):
        C.err("channel", f"channel mismatch: channel '{ch_name}' belongs to server {ch_guild_id}, NOT {guild_id}.")
        return False, "channel guild mismatch"

    channel_type_names = {
        0: "Text Channel",
        2: "Voice Channel",
        4: "Category",
        5: "Announcement Channel",
        13: "Stage Channel",
        15: "Forum Channel",
    }
    if ch_type not in (2, 13):
        type_name = channel_type_names.get(ch_type, f"Type {ch_type}")
        C.err("channel", f"invalid channel type: '{ch_name}' is a {type_name}, NOT a Voice Channel.")
        C.info(f"{C.GREY}hint: right-click a voice channel (speaker icon) in Discord -> Copy Channel ID.{C.RESET}")
        return False, "not a voice channel"

    type_name = "Stage Channel" if ch_type == 13 else "Voice Channel"
    C.ok("channel", f"voice channel verified: {C.WHITE}#{ch_name}{C.RESET} ({type_name})")

    raw_perms = ch_data.get("permissions")
    if raw_perms is not None:
        try:
            perms = int(raw_perms)
            is_admin = bool(perms & PERMISSION_ADMINISTRATOR)
            can_view = is_admin or bool(perms & PERMISSION_VIEW_CHANNEL)
            can_connect = is_admin or bool(perms & PERMISSION_CONNECT)
            can_stream = is_admin or bool(perms & PERMISSION_STREAM)

            if not can_view:
                C.err("perms", f"missing permission: your account cannot view channel '#{ch_name}' (VIEW_CHANNEL missing).")
                return False, "cannot view channel"
            if not can_connect:
                C.err("perms", f"missing permission: your account cannot connect to channel '#{ch_name}' (CONNECT missing).")
                return False, "cannot connect"
            if not can_stream:
                C.err("perms", f"missing permission: your account cannot Go Live in channel '#{ch_name}' (STREAM / Video missing).")
                C.info(f"{C.GREY}hint: a server administrator must grant your role 'Video / Go Live' permission.{C.RESET}")
                return False, "cannot stream"

            C.ok("perms", f"permissions verified: {C.GREEN}View Channel ✓ · Connect ✓ · Go Live (Stream) ✓{C.RESET}")
            return True, f"server: {guild_name}, channel: #{ch_name}"
        except Exception:
            pass

    C.ok("perms", "voice channel permissions check completed")
    return True, f"server: {guild_name}, channel: #{ch_name}"



class GatewayStreamEngine:
    def __init__(self, state: State, session: Session, console: Console) -> None:
        self.state = state
        self.session = session
        self.console = console
        self.telemetry = ScienceTelemetry(self.state.token, self.console) if self.state.token else None
        self.ws: MiniWebSocket | None = None
        self.voice_conn: VoiceConnection | None = None
        self.stream_conn: VoiceConnection | None = None
        self.running = False
        self.heartbeat_interval = 41.25
        self.sequence: int | None = None
        self.start_time = 0.0

        self.gateway_session_id = ""
        self.user_id = ""
        self.stream_key = ""

        self.voice_session_id = ""
        self.voice_token = ""
        self.voice_endpoint = ""
        self.voice_connected = False

        self.stream_token = ""
        self.stream_endpoint = ""
        self.stream_connected = False
        self.stream_ever_active = False
        self._stream_recreate_attempts = 0
        self._user_left_vc = False

        self._status_thread: threading.Thread | None = None
        self.auto_viewer: AutoViewer | None = None

    def start_stream(self, guild_id: str, channel_id: str) -> None:
        C = self.console
        C.rule()
        C.info(f"{C.MAGENTA}Headless Gateway Live Streamer{C.WHITE}")
        C.rule()
        C.info("connecting to discord gateway (wss://gateway.discord.gg)...")

        self.ws = MiniWebSocket(GATEWAY_URL, path="/?v=9&encoding=json")
        try:
            self.ws.connect()
        except Exception as exc:
            C.err("gateway", f"connection failed: {exc}")
            C.rule()
            if not getattr(self.state, "auto_start", False) and sys.stdin.isatty():
                input(f"  {C.ORANGE}›{C.RESET} press enter to return to menu")
            return

        C.ok("gateway", "connected")
        self.running = True
        self.start_time = time.time()
        self.user_id = _user_id_from_token(self.state.token) or ""
        self.stream_ever_active = False
        self._stream_recreate_attempts = 0
        self._user_left_vc = False

        event_thread = threading.Thread(
            target=self._event_loop,
            args=(guild_id, channel_id),
            daemon=True,
        )
        event_thread.start()

        self._status_thread = threading.Thread(
            target=self._status_display,
            daemon=True,
        )
        self._status_thread.start()

        print()
        C.info(f"{C.GREY}press enter at any time to stop streaming...{C.RESET}")
        C.rule()
        try:
            import msvcrt
            while self.running:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    if ch in (b'\r', b'\n', b'\x03'):
                        break
                time.sleep(0.1)
        except Exception:
            while self.running:
                time.sleep(0.5)

        self._cleanup(guild_id)

    def _status_display(self) -> None:
        while self.running:
            time.sleep(60)
            if not self.running:
                break
            elapsed = time.time() - self.start_time
            hours = elapsed / 3600.0
            mins = int(elapsed / 60)
            if self.stream_connected:
                status = f"{self.console.MAGENTA}🔴 LIVE{self.console.RESET}"
            elif self.voice_connected:
                status = f"{self.console.CYAN}⏳ starting stream{self.console.RESET}"
            else:
                status = f"{self.console.YELLOW}⏳ reconnecting{self.console.RESET}"
            viewer_str = ""
            if self.auto_viewer:
                if self.auto_viewer.conflict_paused:
                    viewer_str = f" · {self.console.RED}твинк на паузе (конфликт сессий){self.console.WHITE}"
                elif self.auto_viewer.is_watching:
                    viewer_str = f" · {self.console.CYAN}твинк смотрит{self.console.WHITE}"
                elif self.auto_viewer.voice_connected:
                    viewer_str = f" · {self.console.GREY}твинк в войсе{self.console.WHITE}"
                else:
                    viewer_str = f" · {self.console.YELLOW}твинк подключается{self.console.WHITE}"
            self.console.live(
                "stream",
                f"{status} · {mins}m ({hours:.2f}h) elapsed{viewer_str}"
            )

    def _reconnect(self, guild_id: str, channel_id: str, can_resume: bool = True) -> bool:
        C = self.console
        if not self.running:
            return False

        self.stream_connected = False
        self.voice_connected = False

        if self.auto_viewer:
            self.auto_viewer.stop()
            self.auto_viewer = None
        if self.stream_conn:
            try:
                self.stream_conn.close()
            except Exception:
                pass
            self.stream_conn = None

        if self.voice_conn:
            try:
                self.voice_conn.close()
            except Exception:
                pass
            self.voice_conn = None

        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

        attempts = 0
        backoff = 4
        while self.running:
            attempts += 1
            C.warn("gateway", f"reconnecting to gateway in {backoff}s (attempt {attempts})...")
            for _ in range(backoff * 10):
                if not self.running:
                    return False
                time.sleep(0.1)

            try:
                self.ws = MiniWebSocket(GATEWAY_URL, path="/?v=9&encoding=json")
                self.ws.connect()

                hello_msg = None
                for _ in range(15):
                    m = self.ws.recv(timeout=1.0)
                    if isinstance(m, dict) and m.get("op") == "close":
                        code = m.get("code")
                        if code == 4004:
                            C.err("auth", "fatal gateway authentication failure (code 4004: invalid token)")
                            C.err("auth", "please use Option 4 in the menu to update your token.")
                            self.running = False
                            return False
                    if m and m.get("op") == 10:
                        hello_msg = m
                        break
                    time.sleep(0.1)

                if not hello_msg:
                    C.warn("gateway", "did not receive hello from gateway, retrying...")
                    self.ws.close()
                    self.ws = None
                    backoff = min(20, backoff + 3)
                    continue

                d_hello = hello_msg.get("d", {})
                self.heartbeat_interval = d_hello.get("heartbeat_interval", 41250) / 1000.0

                self.ws.send(json.dumps({"op": 1, "d": self.sequence}))

                resumed = False
                if can_resume and self.gateway_session_id and self.sequence is not None:
                    C.info("attempting to resume gateway session (op 6)...")
                    try:
                        self.ws.send(json.dumps({
                            "op": 6,
                            "d": {
                                "token": self.state.token,
                                "session_id": self.gateway_session_id,
                                "seq": self.sequence,
                            },
                        }))

                        res_msg = self.ws.recv(timeout=4.0)
                        if res_msg:
                            op_res = res_msg.get("op")
                            t_res = res_msg.get("t")
                            if op_res == 0 and t_res == "RESUMED":
                                C.ok("gateway", "session resumed successfully")
                                resumed = True
                            elif op_res == 9:
                                C.warn("gateway", "resume failed (invalid session), falling back to identify")
                                time.sleep(random.uniform(1.5, 3.0))
                            elif op_res == 0 and t_res == "READY":
                                self._handle_dispatch("READY", res_msg.get("d", {}), guild_id, channel_id)
                                resumed = True
                    except Exception:
                        resumed = False

                if not resumed:
                    C.info("sending identify (op 2)...")
                    self.ws.send(json.dumps({
                        "op": 2,
                        "d": {
                            "token": self.state.token,
                            "capabilities": 16381,
                            "properties": self.session.props_dict,
                            "presence": {
                                "status": "online",
                                "since": 0,
                                "activities": [],
                                "afk": False,
                            },
                            "compress": False,
                            "client_state": _default_client_state(),
                        },
                    }))

                    test_msg = self.ws.recv(timeout=2.0)
                    if isinstance(test_msg, dict) and test_msg.get("op") == "close":
                        code = test_msg.get("code")
                        if code == 4004:
                            C.err("auth", "fatal gateway authentication failure (code 4004: invalid token)")
                            C.err("auth", "please use Option 4 in the menu to update your token.")
                            self.running = False
                            return False
                    elif isinstance(test_msg, dict) and test_msg.get("op") == 0 and test_msg.get("t") == "READY":
                        self._handle_dispatch("READY", test_msg.get("d", {}), guild_id, channel_id)
                        resumed = True

                if resumed:
                    time.sleep(random.uniform(1.5, 2.8))
                    self.ws.send(json.dumps({
                        "op": 4,
                        "d": {
                            "guild_id": guild_id,
                            "channel_id": channel_id,
                            "self_mute": False,
                            "self_deaf": False,
                            "self_video": False,
                        },
                    }))
                    C.ok("voice", f"re-joining voice channel {C.ORANGE}{channel_id}{C.RESET}")

                C.ok("gateway", "gateway reconnected successfully")
                return True

            except Exception as exc:
                C.warn("gateway", f"reconnect attempt {attempts} failed: {exc}")
                if self.ws:
                    self.ws.close()
                    self.ws = None
                backoff = min(20, backoff + 3)

        return False

    def _event_loop(self, guild_id: str, channel_id: str) -> None:
        last_hb = time.time()

        while self.running:
            try:
                if not self.ws or self.ws.closed:
                    if not self._reconnect(guild_id, channel_id):
                        break
                    last_hb = time.time()
                    continue

                now = time.time()

                if now - last_hb > self.heartbeat_interval:
                    if self.ws and not self.ws.closed:
                        try:
                            self.ws.send(json.dumps({"op": 1, "d": self.sequence}))
                            last_hb = now
                        except Exception as exc:
                            self.console.warn("gateway", f"heartbeat failed ({exc})")
                            if not self._reconnect(guild_id, channel_id):
                                break
                            last_hb = time.time()
                            continue

                if self.voice_connected and self.voice_conn and not self.voice_conn.running and not self._user_left_vc:
                    self.console.warn("voice", "voice WebSocket dropped, re-joining voice channel...")
                    self.voice_connected = False
                    self.stream_connected = False
                    try:
                        self.voice_conn.close()
                    except Exception:
                        pass
                    self.voice_conn = None
                    if self.stream_conn:
                        try:
                            self.stream_conn.close()
                        except Exception:
                            pass
                        self.stream_conn = None
                    time.sleep(random.uniform(2.0, 4.0))
                    if self.running and self.ws and not self.ws.closed:
                        self.ws.send(json.dumps({
                            "op": 4,
                            "d": {
                                "guild_id": guild_id,
                                "channel_id": channel_id,
                                "self_mute": False,
                                "self_deaf": False,
                                "self_video": False,
                            },
                        }))

                msg = self.ws.recv(timeout=1.0)
                if not msg:
                    continue

                if isinstance(msg, dict) and msg.get("op") == "close":
                    code = msg.get("code")
                    reason = msg.get("reason", "")
                    self.console.warn("gateway", f"gateway closed connection (code {code}: {reason})")
                    if code == 4004:
                        self.console.err("auth", "fatal: gateway authentication failed (code 4004: invalid token)")
                        self.console.err("auth", "please use Option 4 in the menu to update your token.")
                        self.running = False
                        break
                    elif code in (4010, 4011, 4012, 4013, 4014):
                        self.console.err("gateway", f"fatal gateway error (code {code}: {reason}) — stopping.")
                        self.running = False
                        break

                    if not self._reconnect(guild_id, channel_id):
                        break
                    last_hb = time.time()
                    continue

                op = msg.get("op")
                seq = msg.get("s")
                t = msg.get("t")
                d = msg.get("d") or {}

                if seq is not None:
                    self.sequence = seq

                if op == 10:
                    self.heartbeat_interval = d.get("heartbeat_interval", 41250) / 1000.0
                    last_hb = time.time()

                    self.ws.send(json.dumps({"op": 1, "d": self.sequence}))

                    self.ws.send(json.dumps({
                        "op": 2,
                        "d": {
                            "token": self.state.token,
                            "capabilities": 16381,
                            "properties": self.session.props_dict,
                            "presence": {
                                "status": "online",
                                "since": 0,
                                "activities": [],
                                "afk": False,
                            },
                            "compress": False,
                            "client_state": _default_client_state(),
                        },
                    }))

                elif op == 1:
                    self.ws.send(json.dumps({"op": 1, "d": self.sequence}))
                    last_hb = now

                elif op == 11:
                    pass

                elif op == 0:
                    if t == "RESUMED":
                        self.console.ok("gateway", "gateway session resumed successfully")
                        time.sleep(random.uniform(1.5, 2.5))
                        self.ws.send(json.dumps({
                            "op": 4,
                            "d": {
                                "guild_id": guild_id,
                                "channel_id": channel_id,
                                "self_mute": False,
                                "self_deaf": False,
                                "self_video": False,
                            },
                        }))
                    else:
                        self._handle_dispatch(t, d, guild_id, channel_id)

                elif op == 7:
                    self.console.warn("gateway", "discord requested gateway reconnect (op 7)")
                    if not self._reconnect(guild_id, channel_id, can_resume=True):
                        break
                    last_hb = time.time()

                elif op == 9:
                    resumable = bool(d)
                    self.console.warn("gateway", f"session invalidated (op 9, resumable={resumable})")
                    if not self._reconnect(guild_id, channel_id, can_resume=resumable):
                        break
                    last_hb = time.time()

            except Exception as exc:
                if not self.running:
                    break
                self.console.warn("gateway", f"connection dropped: {exc}")
                if not self._reconnect(guild_id, channel_id):
                    break
                last_hb = time.time()

    def _handle_dispatch(self, event_type: str, d: dict, guild_id: str, channel_id: str) -> None:
        C = self.console

        if event_type == "READY":
            self.gateway_session_id = d.get("session_id", "")
            user_data = d.get("user", {})
            username = user_data.get("username", "?")
            self.user_id = str(user_data.get("id", self.user_id))
            C.ok("auth", f"gateway authenticated ({username})")

            time.sleep(random.uniform(2.0, 3.5))
            self.ws.send(json.dumps({
                "op": 4,
                "d": {
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "self_mute": False,
                    "self_deaf": False,
                    "self_video": False,
                },
            }))
            C.ok("voice", f"joining voice channel {C.ORANGE}{channel_id}{C.RESET}")

        elif event_type == "VOICE_STATE_UPDATE":
            if str(d.get("user_id")) == str(self.user_id):
                active_channel = d.get("channel_id")
                new_session = d.get("session_id", "")
                if new_session:
                    self.voice_session_id = new_session

                if (self.voice_connected or self.stream_connected or self.stream_ever_active):
                    if active_channel is None:
                        C.warn("voice", "user manually disconnected from voice channel in Discord app")
                        C.info("gracefully stopping stream engine and saving accumulated stream hours...")
                        self._user_left_vc = True
                        self.voice_connected = False
                        self.stream_connected = False
                        self.running = False
                        return

                    if str(active_channel) != str(channel_id):
                        C.warn("voice", f"user moved to another voice channel ({active_channel}) in Discord app")
                        C.info("stopping stream on old channel and saving accumulated stream hours...")
                        self._user_left_vc = True
                        self.voice_connected = False
                        self.stream_connected = False
                        self.running = False
                        return

                C.ok("voice", f"voice state updated (session: {self.voice_session_id[:8]}...)")

        elif event_type == "VOICE_SERVER_UPDATE":
            token = d.get("token", "")
            endpoint = d.get("endpoint") or ""
            v_guild = d.get("guild_id", guild_id)

            if not endpoint or not token:
                C.err("voice", "empty voice server endpoint/token")
                return

            if self.voice_connected:
                C.warn("voice", "voice server update received, re-establishing voice connection...")
                if self.voice_conn:
                    self.voice_conn.close()
                self.voice_connected = False

            self.voice_token = token
            self.voice_endpoint = endpoint
            C.ok("voice", f"voice server: {endpoint}")

            session_id = self.voice_session_id or self.gateway_session_id
            self.voice_conn = VoiceConnection(
                endpoint=self.voice_endpoint,
                server_id=v_guild,
                user_id=self.user_id,
                session_id=session_id,
                token=self.voice_token,
                console=C,
                label="voice",
                is_stream=False,
            )

            C.info("performing voice WebSocket handshake...")
            if self.voice_conn.connect():
                self.voice_conn.start_heartbeat()
                self.voice_conn.send_speaking(False)
                self.voice_conn.start_silence_pump()
                self.voice_connected = True
                C.ok("voice", f"{C.GREEN}voice connection established (ssrc={self.voice_conn.ssrc}){C.RESET}")

                time.sleep(random.uniform(1.8, 3.2))
                C.info("requesting Go Live stream (op 18 Stream Create)...")
                try:
                    self.ws.send(json.dumps({
                        "op": 18,
                        "d": {
                            "type": "guild",
                            "guild_id": guild_id,
                            "channel_id": channel_id,
                            "preferred_region": None,
                        },
                    }))
                except Exception as exc:
                    C.warn("stream", f"stream create send error: {exc}")
            else:
                C.err("voice", "voice WebSocket handshake FAILED, retrying in 5s...")
                def _retry_voice():
                    time.sleep(random.uniform(4.5, 6.5))
                    if self.running and self.ws and not self.ws.closed:
                        try:
                            self.ws.send(json.dumps({
                                "op": 4,
                                "d": {
                                    "guild_id": guild_id,
                                    "channel_id": channel_id,
                                    "self_mute": False,
                                    "self_deaf": False,
                                    "self_video": False,
                                },
                            }))
                        except Exception:
                            pass
                threading.Thread(target=_retry_voice, daemon=True).start()

        elif event_type == "STREAM_CREATE":
            self.stream_key = d.get("stream_key", "")
            C.ok("stream", f"Go Live created! stream_key: {C.CYAN}{self.stream_key}{C.RESET}")
            if self.telemetry:
                self.telemetry.start_stream(guild_id, channel_id)
            if self.state.viewer_token and not self.auto_viewer:
                self.auto_viewer = AutoViewer(
                    self.state.viewer_token,
                    guild_id,
                    channel_id,
                    self.console,
                    target_user_id=self.user_id,
                )
                self.auto_viewer.start(self.stream_key)

            self.stream_connected = True
            self.stream_ever_active = True
            self._stream_recreate_attempts = 0

            if self.voice_conn and self.voice_conn.ws and not self.voice_conn.ws.closed:
                try:
                    video_ssrc = self.voice_conn.ssrc + 1
                    rtx_ssrc = self.voice_conn.ssrc + 2
                    self.voice_conn.ws.send(json.dumps({
                        "op": 12,
                        "d": {
                            "audio_ssrc": self.voice_conn.ssrc,
                            "video_ssrc": video_ssrc,
                            "rtx_ssrc": rtx_ssrc,
                            "streams": [
                                {
                                    "type": "screen",
                                    "rid": "100",
                                    "ssrc": video_ssrc,
                                    "rtx_ssrc": rtx_ssrc,
                                    "active": True,
                                    "quality": 100,
                                    "max_bitrate": 8000000,
                                    "max_framerate": 30,
                                    "max_resolution": {"type": "fixed", "width": 1920, "height": 1080},
                                }
                            ],
                        },
                    }))
                    self.voice_conn.send_speaking(True)
                except Exception:
                    pass

            print()
            C.rule()
            C.live("stream", f"{C.MAGENTA}● BROADCASTING LIVE{C.RESET}, discord shows your account as {C.MAGENTA}LIVE{C.RESET}")
            C.rule()
            print(f"  {C.YELLOW}⚠️  CRITICAL REQUIREMENTS FOR STREAM TIME TO ACCUMULATE:{C.RESET}")
            print(f"  {C.ORANGE}1.{C.RESET} {C.WHITE}An alt account (or friend) {C.YELLOW}MUST be in the voice channel{C.RESET}{C.WHITE} sitting or watching.{C.RESET}")
            print(f"     {C.GREY}› Discord will NOT count streaming hours if you stream alone in an empty channel!{C.RESET}")
            print(f"  {C.ORANGE}2.{C.RESET} {C.WHITE}Ensure {C.YELLOW}'Use data to personalize my Discord experience'{C.RESET}{C.WHITE} is turned {C.GREEN}ON{C.RESET}{C.WHITE}.{C.RESET}")
            print(f"     {C.GREY}› Check in Discord: User Settings (⚙) → Data & Privacy → Personalization.{C.RESET}")
            C.rule()
            if HAS_CRYPTO and self.voice_conn and self.voice_conn.secret_key:
                C.ok("audio", "encrypted silence pump active (cryptography lib found)")
            else:
                C.info(f"{C.GREY}tip: pip install cryptography (enables encrypted audio frames){C.RESET}")

        elif event_type == "STREAM_SERVER_UPDATE":
            token = d.get("token", "")
            endpoint = d.get("endpoint") or ""
            if endpoint and token:
                self.stream_token = token
                self.stream_endpoint = endpoint
                C.ok("stream", f"stream media route: {endpoint}")

        elif event_type == "STREAM_DELETE":
            self.stream_connected = False
            if self.stream_conn:
                self.stream_conn.close()
                self.stream_conn = None

            if not self.running or not self.voice_connected or self._user_left_vc:
                return

            if self._stream_recreate_attempts < 3:
                self._stream_recreate_attempts += 1
                C.warn("stream", f"stream stopped by server, re-requesting Go Live in 4s (attempt {self._stream_recreate_attempts}/3)...")
                def _recreate():
                    time.sleep(random.uniform(3.5, 5.5))
                    if self.running and self.ws and not self.ws.closed and self.voice_connected and not self._user_left_vc:
                        try:
                            self.ws.send(json.dumps({
                                "op": 18,
                                "d": {
                                    "type": "guild",
                                    "guild_id": guild_id,
                                    "channel_id": channel_id,
                                    "preferred_region": None,
                                },
                            }))
                        except Exception:
                            pass
                threading.Thread(target=_recreate, daemon=True).start()
            else:
                C.err("stream", "stream stopped repeatedly by server — stopping session to prevent rate limits.")
                self.running = False

        elif event_type == "VOICE_CONNECTION_STATUS":
            status = d.get("state", "")
            if status:
                C.info(f"voice connection status: {status}")

    def _cleanup(self, guild_id: str) -> None:
        C = self.console
        self.running = False

        if self.telemetry:
            try:
                self.telemetry.stop_stream(guild_id, getattr(self.state, "last_channel_id", ""))
            except Exception:
                pass

        if self.auto_viewer:
            try:
                self.auto_viewer.stop()
            except Exception:
                pass
            self.auto_viewer = None

        if self.ws and not self.ws.closed and self.stream_key:
            try:
                self.ws.send(json.dumps({
                    "op": 19,
                    "d": {"stream_key": self.stream_key},
                }))
            except Exception:
                pass

        if self.ws and not self.ws.closed and not self._user_left_vc:
            try:
                self.ws.send(json.dumps({
                    "op": 4,
                    "d": {
                        "guild_id": guild_id,
                        "channel_id": None,
                        "self_mute": False,
                        "self_deaf": False,
                        "self_video": False,
                    },
                }))
            except Exception:
                pass

        if self.stream_conn:
            self.stream_conn.close()
            self.stream_conn = None
        if self.voice_conn:
            self.voice_conn.close()
            self.voice_conn = None
        if self.ws:
            time.sleep(0.3)
            self.ws.close()
            self.ws = None

        if (self.stream_connected or self.stream_ever_active) and self.start_time > 0:
            elapsed = time.time() - self.start_time
            hours = elapsed / 3600.0
            if elapsed >= 10:
                self.state.total_stream_hours_claimed += hours
                self.state.total_stream_sessions += 1
                self.state.save()
                print()
                C.ok("stream", f"ended · {elapsed:.0f}s ({hours:.2f}h) streamed live")
                C.info(f"{C.GREY}note: badge progression typically syncs within 24-48 hours.{C.RESET}")
        else:
            print()
            C.info("stream session ended.")

        C.rule()
        if not getattr(self.state, "auto_start", False) and sys.stdin.isatty():
            input(f"  {C.ORANGE}›{C.RESET} press enter to return to menu")



def inspect_profile(state: State, session: Session, console: Console) -> None:
    C = console
    active_tok = state.token or state.viewer_token
    if not active_tok:
        C.err("auth", "no token saved")
        C.rule()
        input(f"  {C.ORANGE}›{C.RESET} press enter to continue")
        return

    C.info("fetching profile data from discord...")
    uid = state.streamer_id or _user_id_from_token(state.token) or _user_id_from_token(active_tok)
    if not uid:
        try:
            req_me = urllib.request.Request(
                ME_URL,
                headers={
                    "authorization": active_tok,
                    "user-agent": USER_AGENT,
                    "x-super-properties": session.super_props,
                },
            )
            with urllib.request.urlopen(req_me, timeout=10) as resp:
                uid = json.loads(resp.read().decode("utf-8")).get("id", "")
        except Exception:
            pass

    if not uid:
        C.err("profile", "unable to resolve user id from token")
        C.rule()
        input(f"  {C.ORANGE}›{C.RESET} press enter to continue")
        return

    url = f"{USERS_URL}/{uid}/profile?with_mutual_guilds=false&with_mutual_friends_count=false"
    req = urllib.request.Request(
        url,
        headers={
            "authorization": active_tok,
            "user-agent": USER_AGENT,
            "x-super-properties": session.super_props,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        user = data.get("user", {})
        badges = data.get("badges", [])
        C.rule()
        username = user.get("username", "Unknown")
        global_name = user.get("global_name")
        display = f"{global_name} (@{username})" if global_name else f"@{username}"
        C.ok("account", f"{display} · ID: {uid}")

        if badges:
            C.info(f"badges ({len(badges)}):")
            for b in badges:
                bid = b.get("id", "badge")
                desc = b.get("description", "")
                C.info(f"  {C.CYAN}•{C.WHITE} {C.ORANGE}{bid}{C.WHITE}: {desc}")
        else:
            C.info("no public badges equipped on profile")
    except Exception as exc:
        C.err("profile", f"failed to load: {exc}")

    C.rule()
    input(f"  {C.ORANGE}›{C.RESET} press enter to continue")



class StreamMaximizerApp:
    def __init__(self) -> None:
        self.console = Console()
        self.state = State.load()
        self.session: Session | None = None

    def start(self) -> None:
        C = self.console
        C.banner()
        time.sleep(0.3)

        self._new_session()

        if self.state.has_token:
            C.ok("auth", "streamer_token loaded from config")
            C.info("validating session...")
            try:
                ensure_token(self.state, self.session.super_props)
                C.ok("auth", "ready · analytics token cached")
            except Exception as exc:
                C.warn("auth", f"streamer_token check failed ({exc})")
        elif self.state.viewer_token:
            C.ok("auth", "Режим безопасности основы: задан только viewer_token (основной аккаунт не используется в боте)")
        else:
            C.info("Токены не найдены в config.json. Вы можете ввести токен основы (Режим 1) или пропустить (Enter) для Режима 2 (Только Твинк):")
            self._prompt_token()

        if HAS_CRYPTO:
            C.ok("crypto", "cryptography library available (encrypted audio enabled)")
        else:
            C.warn("crypto", "cryptography not installed · pip install cryptography for audio frames")

        if getattr(self.state, "auto_start", False) or "--auto" in sys.argv or "--viewer-only" in sys.argv:
            mode = getattr(self.state, "auto_mode", 1)
            if "--viewer-only" in sys.argv or (not self.state.has_token and self.state.viewer_token):
                mode = 2
            if self.state.last_guild_id and self.state.last_channel_id:
                if mode == 2:
                    C.ok("auto", f"auto-start (Режим 2: Только Твинк-Зритель) · сервер {self.state.last_guild_id} канал {self.state.last_channel_id}")
                    self.run_standalone_alt_viewer(auto_run=True)
                    return
                else:
                    C.ok("auto", f"auto-start (Режим 1: Авто-Стрим + Твинк) · сервер {self.state.last_guild_id} канал {self.state.last_channel_id}")
                    engine = GatewayStreamEngine(self.state, self.session, self.console)
                    engine.start_stream(self.state.last_guild_id, self.state.last_channel_id)
                    return
            else:
                C.warn("auto", "auto_start requested but guild_id or channel_id missing")

        time.sleep(0.5)
        self.menu()

    def _new_session(self) -> None:
        self.session = Session.new()

    def _prompt_token(self) -> None:
        self.console.info("Вставьте токен основы (streamer_token) или нажмите Enter, чтобы использовать только твинк:")
        entered = input(f"  {self.console.GREY}streamer_token:{self.console.RESET} ").strip()
        if entered:
            self.state.token = entered
            self.state.save()
            self.console.ok("token", "saved")

    def _prompt_cookie(self) -> None:
        self.console.info("paste your cf_clearance cookie header")
        entered = input(f"  {self.console.GREY}cookie:{self.console.RESET} ").strip()
        if entered:
            self.state.cookie = entered
            self.state.save()
            self.console.ok("cookie", "saved")

    def _refresh(self) -> None:
        C = self.console
        C.info("Вставьте новый streamer_token (или Enter чтобы оставить текущий):")
        tok = input(f"  {C.GREY}streamer_token:{C.RESET} ").strip()
        if tok:
            self.state.token = tok
            C.ok("token", "streamer_token обновлён")

        C.info("Вставьте новый viewer_token твинка (или Enter чтобы оставить текущий):")
        vtok = input(f"  {C.GREY}viewer_token:{C.RESET} ").strip()
        if vtok:
            self.state.viewer_token = vtok
            C.ok("token", "viewer_token твинка обновлён")

        C.info("ID основного аккаунта streamer_id (опционально для Режима 2 без токена основы, Enter чтобы пропустить):")
        sid = input(f"  {C.GREY}streamer_id [{self.state.streamer_id}]:{C.RESET} ").strip()
        if sid:
            self.state.streamer_id = sid

        self.state.save()
        if self.state.has_token:
            C.info("refreshing analytics token...")
            try:
                self.state.fetched_at = 0
                ensure_token(self.state, self.session.super_props)
                self._new_session()
                self.state.save()
                C.ok("auth", "refreshed successfully")
            except Exception as exc:
                C.err("auth", str(exc))
        time.sleep(1)

    def run_gateway_live_streamer(self) -> None:
        C = self.console
        C.rule()

        if not self.state.has_token:
            C.warn("auth", "Для Режима 1 требуется токен аккаунта-стримера (streamer_token).")
            self._prompt_token()
            if not self.state.has_token:
                return

        C.info(f"{C.CYAN}Streaming Badge Tiers:{C.RESET}")
        for tier, name, req_hours in STREAM_TIERS:
            symbol = "●" if self.state.total_stream_hours_claimed >= req_hours else "○"
            color = C.GREEN if self.state.total_stream_hours_claimed >= req_hours else C.GREY
            C.info(f"  {color}{symbol} Tier {tier:2d} · {C.ORANGE}{name:12s}{color} {req_hours:5,d}h{C.RESET}")
        print()

        C.info("this connects your account to discord gateway, joins a voice channel,")
        C.info("and broadcasts an active Go Live stream to accumulate streaming hours.")
        print()
        C.warn("rule 1", f"An alt or friend {C.YELLOW}MUST sit in the voice channel{C.RESET} - solo streams with 0 viewers do NOT count!")
        C.warn("rule 2", f"Discord Settings → Data & Privacy → {C.YELLOW}'Personalize my experience'{C.RESET} must be {C.GREEN}ON{C.RESET}!")
        C.warn("anti-ban", f"{C.RED}НЕ запускайте один и тот же токен одновременно на ПК и на VDS!{C.RESET}")
        print()

        while True:
            guild_id = input(f"  {C.GREY}server (guild) id [{self.state.last_guild_id}] (Enter to use):{C.RESET} ").strip() or self.state.last_guild_id
            channel_id = input(f"  {C.GREY}voice channel id [{self.state.last_channel_id}] (Enter to use):{C.RESET} ").strip() or self.state.last_channel_id

            if not guild_id or not channel_id:
                C.err("input", "server id and voice channel id are required")
                time.sleep(1.5)
                return

            print()
            C.info("verifying server, voice channel, and streaming permissions...")
            valid, info = validate_stream_target(
                token=self.state.token,
                super_props=self.session.super_props,
                guild_id=guild_id,
                channel_id=channel_id,
                console=C,
            )

            if valid:
                self.state.last_guild_id = guild_id
                self.state.last_channel_id = channel_id
                self.state.save()
                break

            print()
            retry = input(f"  {C.ORANGE}›{C.RESET} try entering IDs again? (y/n, default: y): ").strip().lower()
            if retry in ("n", "no", "q", "exit"):
                return
            print()

        engine = GatewayStreamEngine(self.state, self.session, self.console)
        engine.start_stream(guild_id, channel_id)

    def run_standalone_alt_viewer(self, auto_run: bool = False) -> None:
        C = self.console
        C.rule()
        C.info(f"{C.CYAN}Режим Авто-Зрителя (Твинк-бот · 100% безопасность основы){C.RESET}")
        C.info("Этот режим подключает ТОЛЬКО ваш твинк в голосовой канал.")
        C.info("Вы можете стримить сами со своего официального Discord клиента без ввода токена основы,")
        C.info("а твинк будет сидеть рядом и смотреть трансляцию 24/7 (часы идут в зачёт).")
        C.warn("anti-ban", f"{C.RED}ВАЖНО: Не запускайте один и тот же твинк одновременно на ПК и на VDS!{C.RESET}")
        C.rule()
        print()

        if not self.state.viewer_token:
            if auto_run:
                C.err("auth", "viewer_token отсутствует в config.json для auto_start")
                return
            C.warn("auth", "Токен твинка не найден в конфигурации.")
            C.info("Вставьте токен вашего второго аккаунта (твинка):")
            entered = input(f"  {C.GREY}viewer_token:{C.RESET} ").strip()
            if not entered:
                C.err("auth", "Токен твинка обязателен для этого режима.")
                time.sleep(1.5)
                return
            self.state.viewer_token = entered
            self.state.save()

        if auto_run:
            guild_id = self.state.last_guild_id
            channel_id = self.state.last_channel_id
        else:
            guild_id = input(f"  {C.GREY}ID сервера [{self.state.last_guild_id}] (Enter чтобы использовать):{C.RESET} ").strip() or self.state.last_guild_id
            channel_id = input(f"  {C.GREY}ID голосового канала [{self.state.last_channel_id}] (Enter чтобы использовать):{C.RESET} ").strip() or self.state.last_channel_id

        if not guild_id or not channel_id:
            C.err("input", "ID сервера и канала обязательны.")
            time.sleep(1.5)
            return

        self.state.last_guild_id = guild_id
        self.state.last_channel_id = channel_id
        self.state.save()

        target_uid = self.state.streamer_id or _user_id_from_token(self.state.token)
        if not target_uid and not auto_run:
            target_uid = input(f"  {C.GREY}ID стримера (основы) [{self.state.streamer_id}]:{C.RESET} ").strip() or self.state.streamer_id
            if target_uid:
                self.state.streamer_id = target_uid
                self.state.save()

        viewer = AutoViewer(self.state.viewer_token, guild_id, channel_id, self.console, target_user_id=target_uid)
        viewer.start()

        telemetry = None
        if self.state.token:
            telemetry = ScienceTelemetry(self.state.token, self.console)
            telemetry.start_stream(guild_id, channel_id)

        print()
        C.rule()
        C.ok("status", "Твинк запущен в защищённом режиме (Anti-Ban v2.0).")
        C.info("Теперь вы можете запустить стрим в Discord клиенте.")
        C.info(f"Нажмите {C.ORANGE}[Enter]{C.WHITE} в любой момент, чтобы остановить твинка и выйти в меню.")
        C.rule()

        start_t = time.time()
        last_status_t = time.time()
        try:
            import msvcrt
            has_msvcrt = True
        except Exception:
            has_msvcrt = False

        try:
            while viewer.running:
                if has_msvcrt and msvcrt.kbhit():
                    ch = msvcrt.getch()
                    if ch in (b'\r', b'\n', b'\x03'):
                        break
                time.sleep(0.2)
                now = time.time()
                if now - last_status_t >= 30.0:
                    last_status_t = now
                    elapsed_m = int((now - start_t) / 60)
                    if viewer.conflict_paused:
                        st = f"{C.RED}⏸ Пауза Anti-Ban (аккаунт в войсе с другого устройства/VDS){C.WHITE}"
                    elif viewer.stream_rtc_connected:
                        st = f"{C.GREEN}👁 Смотрит стрим (WebRTC активен){C.WHITE}"
                    elif viewer.is_watching:
                        st = f"{C.GREEN}👁 Смотрит стрим{C.WHITE}"
                    elif viewer.voice_connected:
                        st = f"{C.CYAN}🔊 В голосовом канале (ждёт стрим){C.WHITE}"
                    else:
                        st = f"{C.YELLOW}⏳ Подключение к войсу...{C.WHITE}"
                    C.info(f"{C.CYAN}[alt-viewer]{C.RESET} В сети: {elapsed_m} мин. · Статус: {st}")
        except (KeyboardInterrupt, Exception):
            pass
        finally:
            if telemetry:
                telemetry.stop_stream(guild_id, channel_id)
            viewer.stop()
            print()
            C.ok("exit", "Твинк успешно отключен от голосового канала.")
            time.sleep(1)

    def menu(self) -> None:
        C = self.console
        while True:
            C.clear()
            C.banner()
            C.info(f"total stream hours: {C.ORANGE}{self.state.total_stream_hours_claimed:,.1f}h{C.WHITE} ({self.state.total_stream_sessions} sessions)")
            C.rule()
            C.info(f"{C.ORANGE}1{C.RESET} · {C.MAGENTA}Авто-Стрим + Твинк{C.RESET} (Headless вещание с основы 0% CPU/GPU + авто-зритель)")
            C.info(f"{C.ORANGE}2{C.RESET} · {C.CYAN}Только Твинк-Зритель{C.RESET} (100% без риска для основы: вы стримите сами в Discord, твинк смотрит)")
            C.info(f"{C.ORANGE}3{C.RESET} · Проверить профиль и статистику значков")
            C.info(f"{C.ORANGE}4{C.RESET} · Обновить токены и настройки авторизации")
            C.info(f"{C.ORANGE}5{C.RESET} · Выход")
            C.rule()

            choice = input(f"  {C.ORANGE}›{C.RESET} ").strip()
            print()
            if choice == "1":
                self.run_gateway_live_streamer()
            elif choice == "2":
                self.run_standalone_alt_viewer()
            elif choice == "3":
                inspect_profile(self.state, self.session, self.console)
            elif choice == "4":
                self._refresh()
            elif choice in ("5", "exit", "q"):
                break
            else:
                C.warn("input", "invalid option")
                time.sleep(1)


def main() -> None:
    try:
        StreamMaximizerApp().start()
    except KeyboardInterrupt:
        print()
        Console().warn("exit", "operation interrupted by user")


if __name__ == "__main__":
    main()
