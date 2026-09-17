# -*- coding: utf-8 -*-
"""卡丁快跑综合科目 ASR PC 桥。

新协议：
    MCU -> PC: ASR <session_id> <status> <base64-pcm>
    PC  -> MCU: ASR:<session_id>:SEQ,<count>,<id...>
控制：ASR HELLO <token> / ASR CANCEL <session_id>。

session_id 把迟到结果与下一轮录音隔离；status=0 开新会话时会显式取消旧线程。
旧三字段音频格式仍可接收，便于过渡。
"""

import os
import sys
import ssl
import json
import time
import hmac
import struct
import base64
import hashlib
import socket
import threading
import queue
from urllib.parse import urlencode
from datetime import datetime, timezone

import asr_vocab   # 整段中文 -> 严格灯光/鸣笛动作 ID 队列


def _load_creds():
    cand = []
    if getattr(sys, 'frozen', False):
        cand.append(os.path.dirname(sys.executable))
    cand.append(os.path.dirname(os.path.abspath(__file__)))
    cand.append(os.getcwd())
    seen = set()
    for d in cand:
        if d in seen:
            continue
        seen.add(d)
        p = os.path.join(d, 'xfyun_credentials.py')
        if os.path.isfile(p):
            ns = {}
            try:
                with open(p, encoding='utf-8') as f:
                    exec(compile(f.read(), p, 'exec'), ns)
                a = ns.get('APP_ID', '')
                k = ns.get('API_KEY', '')
                s = ns.get('API_SECRET', '')
                if a and k and s:
                    return a, k, s
            except Exception:
                pass
    return (os.environ.get('XFYUN_APPID', ''),
            os.environ.get('XFYUN_API_KEY', ''),
            os.environ.get('XFYUN_API_SECRET', ''))


APP_ID, API_KEY, API_SECRET = _load_creds()

XFYUN_HOSTS = ["ws-api.xfyun.cn", "iat-api.xfyun.cn"]
XFYUN_PORT = 443
XFYUN_PATH = "/v2/iat"

SR_HZ = 8000
FRAME_BYTES = 640
FRAME_PACE_S = 0.04
CONNECT_TIMEOUT_S = 3.0
SESSION_MAX_S = 40.0
QUIET_FINALIZE_S = 3.0
PROBE_TIMEOUT_S = 2.0
PROBE_READY_TTL_S = 30.0
MAX_WS_PAYLOAD = 1024 * 1024

_LOG_PATH = None

# ---- 音频前处理(2026-07-20,纯 PC 端) -----------------------------------
# 一阶高通 ~120Hz:压电机嗡嗡/风噪低频能量,人声基频以上基本不动。
# 增益归一化:小振幅音频讯飞错字率明显升高,按会话内峰值把幅度拉向目标值,
# 增益只升不降且封顶,避免把噪声地板放大到人声量级。
# 出任何异常回退原始 PCM —— 前处理只能让音频更好,绝不能弄断链路。
PREPROC_ENABLE = True
HPF_CUTOFF_HZ = 120.0
GAIN_TARGET_PEAK = 20000      # int16 目标峰值(约 -4dBFS)
GAIN_MAX = 8.0                # 增益封顶,防噪声地板被无限放大

import math as _math
_HPF_ALPHA = 1.0 / (1.0 + 2.0 * _math.pi * HPF_CUTOFF_HZ / SR_HZ)


class _AudioPreproc:
    """会话级前处理状态(滤波器记忆 + 会话内峰值)。每个 session_id 一个实例,
    跨 chunk 连续滤波,不在 chunk 边界产生跳变。"""

    def __init__(self):
        self._px = 0.0        # 上一输入样本
        self._py = 0.0        # 上一输出样本
        self._peak = 1.0      # 会话内已见峰值(滤波后)

    def process(self, pcm: bytes) -> bytes:
        if not PREPROC_ENABLE or len(pcm) < 2:
            return pcm
        try:
            n = len(pcm) // 2
            samples = struct.unpack('<%dh' % n, pcm[:n * 2])
            # 一阶高通(差分方程 y[i] = a*(y[i-1] + x[i] - x[i-1]))
            out = []
            px, py, a = self._px, self._py, _HPF_ALPHA
            for x in samples:
                py = a * (py + x - px)
                px = float(x)
                out.append(py)
            self._px, self._py = px, py
            # 会话内峰值跟踪 -> 增益(只增不减、封顶;首个 chunk 起即可用)
            peak = max(self._peak, max(abs(v) for v in out))
            self._peak = peak
            gain = min(GAIN_MAX, max(1.0, GAIN_TARGET_PEAK / peak))
            clamped = []
            for v in out:
                s = int(v * gain)
                if s > 32767:
                    s = 32767
                elif s < -32768:
                    s = -32768
                clamped.append(s)
            return struct.pack('<%dh' % n, *clamped)
        except Exception as e:
            _log(f"音频前处理异常(回退原始 PCM): {e}")
            return pcm


def _log(msg):
    line = "[ASR] " + str(msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        global _LOG_PATH
        if _LOG_PATH is None:
            base = (os.path.dirname(sys.executable) if getattr(sys, 'frozen', False)
                    else os.path.dirname(os.path.abspath(__file__)))
            _LOG_PATH = os.path.join(base, 'asr_bridge.log')
        with open(_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + line + '\n')
    except Exception:
        pass


def _gen_ws_url(host):
    now = datetime.now(timezone.utc)
    date = now.strftime('%a, %d %b %Y %H:%M:%S GMT')
    signature_origin = f"host: {host}\ndate: {date}\nGET {XFYUN_PATH} HTTP/1.1"
    signature = base64.b64encode(
        hmac.new(API_SECRET.encode(), signature_origin.encode(), hashlib.sha256).digest()
    ).decode()
    auth_origin = (f'api_key="{API_KEY}", algorithm="hmac-sha256", '
                   f'headers="host date request-line", signature="{signature}"')
    authorization = base64.b64encode(auth_origin.encode()).decode()
    qs = urlencode({'authorization': authorization, 'date': date, 'host': host})
    return f"{XFYUN_PATH}?{qs}", date


def _ws_client_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """客户端帧：FIN=1、MASK=1。"""
    header = bytearray([0x80 | (opcode & 0x0F)])
    n = len(payload)
    mask = os.urandom(4)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack('>H', n)
    else:
        header.append(0x80 | 127)
        header += struct.pack('>Q', n)
    header += mask
    header += bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return bytes(header)


def _ws_text_frame(payload: bytes) -> bytes:
    return _ws_client_frame(payload, 0x1)


def _extract_ws_frames(buffer: bytearray):
    """从 TCP 字节流中提取 0..N 个完整 WebSocket 帧，残包留在 buffer。

    返回 [(fin, opcode, payload), ...]；同时支持一次 recv 多帧、帧头/载荷跨 recv、
    服务器异常 MASK 帧。超大载荷直接拒绝，避免坏长度导致内存无限增长。
    """
    out = []
    while True:
        if len(buffer) < 2:
            break
        b0, b1 = buffer[0], buffer[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        pos = 2
        if length == 126:
            if len(buffer) < pos + 2:
                break
            length = struct.unpack('>H', bytes(buffer[pos:pos + 2]))[0]
            pos += 2
        elif length == 127:
            if len(buffer) < pos + 8:
                break
            length = struct.unpack('>Q', bytes(buffer[pos:pos + 8]))[0]
            pos += 8
        if length > MAX_WS_PAYLOAD:
            raise ValueError(f"WebSocket payload too large: {length}")

        mask = None
        if masked:
            if len(buffer) < pos + 4:
                break
            mask = bytes(buffer[pos:pos + 4])
            pos += 4
        frame_end = pos + length
        if len(buffer) < frame_end:
            break
        payload = bytes(buffer[pos:frame_end])
        del buffer[:frame_end]
        if mask is not None:
            payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        out.append((fin, opcode, payload))
    return out


def _build_iat_json(status: int, pcm: bytes) -> str:
    data = {
        "status": status,
        "format": "audio/L16;rate=%d" % SR_HZ,
        "audio": base64.b64encode(pcm).decode() if pcm else "",
        "encoding": "raw",
    }
    if status == 0:
        obj = {
            "common": {"app_id": APP_ID},
            "business": {"domain": "iat", "language": "zh_cn",
                         "accent": "mandarin", "vinfo": 1, "vad_eos": 10000},
            "data": data,
        }
    else:
        obj = {"data": data}
    return json.dumps(obj, ensure_ascii=False)


def _result_text_for_wire(text: str):
    """返回供 PC 端解析的单行最终文本；None 表示云结果格式非法。"""
    if '\r' in text or '\n' in text:
        return None
    return text.strip()


class _AsrSession:
    def __init__(self, send_to_car, session_id=None, legacy=False):
        self._send_to_car = send_to_car
        self.session_id = session_id
        self.legacy = legacy
        self._q = queue.Queue()
        self._buf = b''
        self._pre = _AudioPreproc()   # 会话级滤波/增益状态,跨 chunk 连续
        self._first_sent = False
        self._mcu_done = False
        self._text = ""
        self._tls = None
        self._ws_initial = b''
        self._start_ms = time.time()
        self._alive = True
        self._cancelled = threading.Event()
        self._send_lock = threading.Lock()
        self._reply_lock = threading.Lock()
        self._replied = False
        self._t = threading.Thread(target=self._run, daemon=True,
                                   name=f'asr-{session_id if session_id is not None else "legacy"}')
        self._t.start()

    def feed(self, pcm: bytes, mcu_status: int):
        if self._alive and not self._cancelled.is_set():
            self._q.put((pcm, mcu_status))

    def cancel(self):
        self._cancelled.set()
        self._alive = False
        try:
            if self._tls:
                self._tls.close()
        except Exception:
            pass

    def _reply_once(self, line: str):
        if self._cancelled.is_set():
            return
        with self._reply_lock:
            if self._replied or self._cancelled.is_set():
                return
            self._replied = True
        try:
            self._send_to_car(line)
        except Exception as e:
            _log(f"回发车端失败: {e}")

    def _reply_result(self, text: str):
        final = _result_text_for_wire(text)
        if final is None:
            self._reply_failure("INVALID_TEXT")
            return
        try:
            action_ids, reason = asr_vocab.canonicalize_sequence(final)
        except Exception as e:
            _log(f"序列解析异常 sid={self.session_id}: {e}")
            self._reply_failure("PARSER_ERROR")
            return
        if not action_ids:
            _log(f"序列整批拒绝 sid={self.session_id}: {final!r} [{reason}]")
            self._reply_failure(reason)
            return
        payload = ",".join(str(action_id) for action_id in action_ids)
        sequence = f"SEQ,{len(action_ids)},{payload}"
        _log(f"序列解析 sid={self.session_id}: {final!r} -> {sequence}")
        if self.legacy:
            self._reply_once("ASR:" + sequence)
        else:
            self._reply_once(f"ASR:{self.session_id}:{sequence}")

    def _reply_failure(self, reason: str):
        if self.legacy:
            self._reply_once("ASR:(error)")
        else:
            self._reply_once(f"ASR:FAIL {self.session_id} {reason}")

    def _run(self):
        try:
            self._connect()
            reader = threading.Thread(target=self._read_results, daemon=True,
                                      name=f'asr-reader-{self.session_id}')
            reader.start()
            self._send_loop()
            reader.join(timeout=max(0.1, SESSION_MAX_S - (time.time() - self._start_ms)))
            if reader.is_alive() and not self._cancelled.is_set():
                self._reply_failure("RESULT_TIMEOUT")
        except Exception as e:
            if not self._cancelled.is_set():
                _log(f"session error sid={self.session_id}: {e}")
                self._reply_failure("CLOUD_ERROR")
        finally:
            self._alive = False
            try:
                if self._tls:
                    self._tls.close()
            except Exception:
                pass

    def _connect(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        last_err = None
        for host in XFYUN_HOSTS:
            if self._cancelled.is_set():
                raise RuntimeError("cancelled")
            try:
                ws_path, date_str = _gen_ws_url(host)
                raw = socket.create_connection((host, XFYUN_PORT), timeout=CONNECT_TIMEOUT_S)
                tls = ctx.wrap_socket(raw, server_hostname=host)
                handshake = (
                    f"GET {ws_path} HTTP/1.1\r\n"
                    f"Host: {host}\r\n"
                    f"Date: {date_str}\r\n"
                    f"Upgrade: websocket\r\n"
                    f"Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    f"Sec-WebSocket-Version: 13\r\n\r\n"
                )
                tls.sendall(handshake.encode())
                resp = bytearray()
                while b'\r\n\r\n' not in resp and len(resp) < 16384:
                    chunk = tls.recv(4096)
                    if not chunk:
                        break
                    resp.extend(chunk)
                header, sep, remainder = bytes(resp).partition(b'\r\n\r\n')
                if sep and b" 101 " in header.split(b'\r\n', 1)[0]:
                    tls.settimeout(SESSION_MAX_S)
                    self._tls = tls
                    self._ws_initial = remainder
                    _log(f"讯飞 WSS 握手成功 sid={self.session_id} @ {host}")
                    return
                try:
                    tls.close()
                except Exception:
                    pass
                last_err = RuntimeError(f"handshake not 101: {bytes(resp[:80])!r}")
            except Exception as e:
                last_err = e
                _log(f"{host} 连接失败 sid={self.session_id}: {e}")
        raise RuntimeError(f"所有讯飞 host 都连不上 (last={last_err})")

    def _send_ws(self, payload: bytes, opcode: int = 0x1):
        if self._cancelled.is_set() or not self._tls:
            raise RuntimeError("cancelled")
        with self._send_lock:
            self._tls.sendall(_ws_client_frame(payload, opcode))

    def _emit(self, pcm: bytes, status: int):
        self._send_ws(_build_iat_json(status, pcm).encode(), 0x1)
        time.sleep(FRAME_PACE_S)

    def _send_loop(self):
        last_rx = time.time()
        got_any = False
        while self._alive and not self._cancelled.is_set():
            if (time.time() - self._start_ms) > SESSION_MAX_S:
                _log(f"session timeout sid={self.session_id}, force-finalize")
                self._mcu_done = True
            try:
                pcm, st = self._q.get(timeout=0.05)
                self._buf += self._pre.process(pcm)
                got_any = True
                last_rx = time.time()
                if st == 2:
                    self._mcu_done = True
            except queue.Empty:
                # 新 session_id 协议的 MCU 会保留未入 TX ring 的原帧并重试，不能把
                # 正常背压造成的间隔误判成“末帧丢失”。静默兜底只留给旧协议。
                if (self.legacy and got_any and not self._mcu_done and
                        (time.time() - last_rx) > QUIET_FINALIZE_S):
                    _log(f"静默收尾 sid={self.session_id}: >{QUIET_FINALIZE_S:.1f}s 无新帧")
                    self._mcu_done = True

            if not self._mcu_done:
                while len(self._buf) >= FRAME_BYTES:
                    chunk, self._buf = self._buf[:FRAME_BYTES], self._buf[FRAME_BYTES:]
                    self._emit(chunk, 0 if not self._first_sent else 1)
                    self._first_sent = True
            else:
                while len(self._buf) > FRAME_BYTES:
                    chunk, self._buf = self._buf[:FRAME_BYTES], self._buf[FRAME_BYTES:]
                    self._emit(chunk, 0 if not self._first_sent else 1)
                    self._first_sent = True
                if not self._first_sent:
                    self._emit(self._buf, 0)
                    self._first_sent = True
                    self._emit(b'', 2)
                else:
                    self._emit(self._buf, 2)
                self._buf = b''
                return

    @staticmethod
    def _parse_iat_payload(payload: bytes):
        obj = json.loads(payload.decode('utf-8'))
        if obj.get('code', 0) != 0:
            raise RuntimeError(f"xfyun code={obj.get('code')} msg={obj.get('message')}")
        data = obj.get('data') or {}
        status = data.get('status', 1)
        text = ""
        result = data.get('result') or {}
        for ws in result.get('ws', []):
            for cw in ws.get('cw', []):
                text += cw.get('w', "")
        return text, status

    def _read_results(self):
        rx = bytearray()
        fragments = bytearray()
        fragmented_opcode = None
        try:
            while self._alive and not self._cancelled.is_set():
                if self._ws_initial:
                    data = self._ws_initial
                    self._ws_initial = b''
                else:
                    data = self._tls.recv(4096)
                if not data:
                    break
                rx.extend(data)
                for fin, opcode, payload in _extract_ws_frames(rx):
                    if opcode == 0x8:  # close
                        raise RuntimeError("WebSocket closed before final result")
                    if opcode == 0x9:  # ping
                        self._send_ws(payload, 0xA)
                        continue
                    if opcode == 0x1:
                        if fin:
                            message = payload
                        else:
                            fragments = bytearray(payload)
                            fragmented_opcode = opcode
                            continue
                    elif opcode == 0x0 and fragmented_opcode == 0x1:
                        fragments.extend(payload)
                        if not fin:
                            continue
                        message = bytes(fragments)
                        fragments.clear()
                        fragmented_opcode = None
                    else:
                        continue

                    text, server_status = self._parse_iat_payload(message)
                    if text:
                        self._text += text
                    if server_status == 2:
                        final = self._text.strip()
                        _log(f"最终识别 sid={self.session_id}: {final!r}")
                        self._reply_result(final)
                        return
        except Exception as e:
            if not self._cancelled.is_set():
                _log(f"read error sid={self.session_id}: {e}")
        if not self._cancelled.is_set():
            self._reply_failure("NO_FINAL_RESULT")


# ---- Mission 入口健康探测 ------------------------------------------------
_probe_lock = threading.Lock()
_probe_inflight = False
_probe_waiters = {}
_cloud_ready_until = 0.0


def _cloud_connectivity_check():
    last = None
    for host in XFYUN_HOSTS:
        try:
            s = socket.create_connection((host, XFYUN_PORT), timeout=PROBE_TIMEOUT_S)
            s.close()
            return True, "OK"
        except Exception as e:
            last = e
    return False, type(last).__name__ if last else "OFFLINE"


def _probe_worker():
    global _probe_inflight, _cloud_ready_until
    ok, reason = _cloud_connectivity_check()
    with _probe_lock:
        waiters = list(_probe_waiters.values())
        _probe_waiters.clear()
        _probe_inflight = False
        _cloud_ready_until = time.time() + PROBE_READY_TTL_S if ok else 0.0
    for token, send_to_car in waiters:
        try:
            send_to_car(f"ASR:READY {token}" if ok else f"ASR:ERROR {token} {reason}")
        except Exception as e:
            _log(f"桥健康探测回包失败: {e}")


def _handle_hello(token: int, send_to_car):
    global _probe_inflight
    if not (APP_ID and API_KEY and API_SECRET):
        send_to_car(f"ASR:ERROR {token} NO_CREDENTIALS")
        return
    start = False
    with _probe_lock:
        if time.time() < _cloud_ready_until:
            ready = True
        else:
            ready = False
            _probe_waiters[(token, id(send_to_car))] = (token, send_to_car)
            if not _probe_inflight:
                _probe_inflight = True
                start = True
    if ready:
        send_to_car(f"ASR:READY {token}")
    elif start:
        threading.Thread(target=_probe_worker, daemon=True, name='asr-health').start()


# ---- 模块级会话管理 ------------------------------------------------------
_cur = None
_lock = threading.Lock()


def _cancel_current(session_id=None):
    global _cur
    with _lock:
        if _cur is None:
            return False
        if session_id is not None and _cur.session_id != session_id:
            return False
        old = _cur
        _cur = None
    old.cancel()
    return True


def handle_asr_line(line: str, send_to_car):
    """tuning_tool 收到以 ``ASR `` 开头的车端行时调用；本函数不做阻塞云 I/O。"""
    global _cur
    try:
        # tuning_tool 在分发前会 strip()，所以空 payload 的新协议末帧
        # ``ASR <sid> 2 `` 会变成三个 token：``ASR <sid> 2``。
        parts = line.split()
        if len(parts) < 2 or parts[0] != 'ASR':
            return

        if parts[1] == 'HELLO':
            if len(parts) >= 3:
                _handle_hello(int(parts[2]), send_to_car)
            return
        if parts[1] == 'CANCEL':
            if len(parts) >= 3:
                _cancel_current(int(parts[2]))
            return

        new_empty_payload = (len(parts) == 3 and parts[2] in ('0', '1', '2'))
        legacy = (len(parts) <= 3 and not new_empty_payload)
        if legacy:
            if len(parts) < 2:
                return
            session_id = None
            status = int(parts[1])
            b64 = parts[2] if len(parts) >= 3 else ''
        else:
            if len(parts) < 3:
                return
            session_id = int(parts[1])
            status = int(parts[2])
            b64 = parts[3] if len(parts) >= 4 else ''
        if status not in (0, 1, 2):
            return
        pcm = base64.b64decode(b64, validate=True) if b64 else b''
    except Exception as e:
        _log(f"行解析失败: {line[:60]!r} ({e})")
        return

    if not (APP_ID and API_KEY and API_SECRET):
        if legacy:
            send_to_car("ASR:(error)")
        else:
            send_to_car(f"ASR:FAIL {session_id} NO_CREDENTIALS")
        _log("缺少讯飞凭证，拒绝 ASR 会话")
        return

    with _lock:
        if status == 0:
            old = _cur
            if old is not None:
                old.cancel()
            _cur = _AsrSession(send_to_car, session_id=session_id, legacy=legacy)
        elif (_cur is None or not _cur._alive or _cur.legacy != legacy or
              (not legacy and _cur.session_id != session_id)):
            return
        current = _cur
        current.feed(pcm, status)

    if status == 0:
        _log(f"收到首帧 sid={session_id} (pcm={len(pcm)}B)")
    elif status == 2:
        _log(f"收到末帧 sid={session_id} (pcm={len(pcm)}B)")
