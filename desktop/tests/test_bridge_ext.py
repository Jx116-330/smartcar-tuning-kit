# -*- coding: utf-8 -*-
"""桥扩展机制 + headless 回归(tests/test_bridge_ext.py,2026-09-14 设计文档 §5)。

沙箱 + FakePeer 模式:临时目录拷最小文件集,`python tuning_tool.py --headless`
起桥,假固件连 TCP 8080 推行/帧,HTTP 9898 断言。

覆盖矩阵:
  1.  5000 点窗口算术(节奏受控分批,不测突发吞吐——队列 2000/泵 30 条
      每轮是现行为,突发丢帧属预期,不在本套件断言)
  2.  TELPT 到 SSE(按 SSE 空行组帧解析 data: 行,json.loads 断言)
  3.  相机 ACK 双路径(status subscribed + cmd_result 都要看到)
  4.  CIMG 帧管线(分片发送)
  5.  帧错误路径(坏长度/超时/断连 partial)
  6.  扩展加载失败(缺文件/语法错/EXT_API 不等 1)
  7.  旧 dist 升级模拟(缺键约定名加载 / 显式 null / 命名 profile 缺键)
  8.  源码禁用字符串门(tuning_tool.py 核无领域字符串)
  9.  无 GUI 导入门(毒化 tkinter/ttkbootstrap 后 import 成功,无相机 worker)
  10. headless 生命周期(/shutdown 退出码、日志文件、custom_state 副作用)
  11. 轨迹持久化往返 + 边界(坏 JSON/路径穿越/不存在)
"""
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:9898"
SANDBOX_FILES = ["tuning_tool.py", "config_loader.py", "guardrails.py",
                 "bridge_ext.py", "camera_capture.py", "asr_bridge.py",
                 "asr_vocab.py", "control_schema.json"]
fails = []

# 相机帧常量(camera_capture.py,测试数据用)
CAMERA_MAGIC = b'CIMG'
CAMERA_PAYLOAD_LEN = 160 * 120 * 2


def expect(name, cond, detail=""):
    if cond:
        print("PASS", name)
    else:
        print("FAIL", name, detail)
        fails.append(name)


def get(path, timeout=5):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read())


def get_code(path, timeout=5):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def post(path, payload, timeout=8):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None


class FakePeer:
    """假固件:连桥 8080,后台收行记日志,测试按需回 ACK/推数据。"""

    def __init__(self):
        self.sock = socket.create_connection(("127.0.0.1", 8080), timeout=5)
        self.sock.settimeout(1.0)
        self.rx_lines = []
        self.rx_raw = bytearray()
        self.closed = False
        self._t = threading.Thread(target=self._recv_loop, daemon=True)
        self._t.start()

    def _recv_loop(self):
        buf = bytearray()
        while True:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                self.closed = True
                break
            self.rx_raw.extend(data)
            buf.extend(data)
            while b'\n' in buf:
                ln, _, rest = buf.partition(b'\n')
                buf = bytearray(rest)
                text = ln.rstrip(b'\r').decode('utf-8', errors='replace').strip()
                if text:
                    self.rx_lines.append(text)

    def send_line(self, text):
        self.sock.sendall((text + '\r\n').encode('utf-8'))

    def send_raw(self, data):
        self.sock.sendall(data)

    def wait_cmd(self, fragment, timeout=5.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if any(fragment in ln for ln in self.rx_lines):
                return True
            time.sleep(0.05)
        return False

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def make_sandbox(config: dict) -> Path:
    sb = Path(tempfile.mkdtemp(prefix="tt_ext_"))
    for f in SANDBOX_FILES:
        shutil.copy2(ROOT / f, sb / f)
    (sb / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return sb


def base_config(**over):
    cfg = {
        "app_title": "ext-suite",
        "protocol": {"telemetry_prefixes": ["TELPT", "TELPTDUMP"],
                     "response_prefixes": ["ACK", "ERR"]},
        "custom_state_file": "latest_tuning_state.json",
        "auto_open_browser": False,
        "auto_start_tcp": True,
        "tcp_host": "127.0.0.1",
        "tcp_port": "8080",
        "heartbeat": {"enabled": False},
        "bridge_extension": "bridge_ext.py",
    }
    cfg.update(over)
    return cfg


def start_bridge(sandbox: Path, extra_args=()):
    p = subprocess.Popen(
        [sys.executable, "tuning_tool.py", "--headless", *extra_args],
        cwd=sandbox, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        try:
            get("/status", timeout=2)
            return p
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("bridge not up")


def stop_bridge(p):
    try:
        post("/shutdown", {})
    except Exception:
        pass
    try:
        rc = p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        rc = None
    time.sleep(0.5)   # 端口释放窗口
    return rc


def traj_count():
    return get("/trajectory?since=0")["count"]


# ===========================================================================
# 8. 源码禁用字符串门(先跑:核洁净是其它一切的前提)
# ===========================================================================
FORBIDDEN = ["TELPT", "TELPTDUMP", "CAMMETA", "CAMSTAT", "CIMG",
             "CAMERA_MAGIC", "START_STREAM_CAMERA_FRAME",
             "STOP_STREAM_CAMERA_FRAME", "ASR ", "trajectory", "camera",
             "CameraCapture", "MixedCameraStream"]
src = (ROOT / "tuning_tool.py").read_text(encoding="utf-8")
hits = sorted({tok for tok in FORBIDDEN if tok in src})
expect("8. 核源码无领域字符串", not hits, str(hits))

# ===========================================================================
# 9. 无 GUI 导入门
# ===========================================================================
code = ("import sys\n"
        "for m in ('tkinter', 'tkinter.messagebox', 'ttkbootstrap', "
        "'ttkbootstrap.constants'):\n"
        "    sys.modules[m] = None\n"
        "import tuning_tool\n"
        "names = [t.name for t in __import__('threading').enumerate()]\n"
        "assert 'camera-capture' not in names, names\n"
        "print('IMPORT-OK')\n")
r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                   capture_output=True, text=True, timeout=60)
expect("9. 毒化 GUI 库后 import 成功且无相机 worker",
       "IMPORT-OK" in r.stdout, r.stdout[-200:] + r.stderr[-300:])

# 插件 close() 属于不可信扩展代码；即使它抛错，核也必须继续关闭 socket、
# 移除连接并广播链路状态。
import queue as _queue
sys.path.insert(0, str(ROOT))
import tuning_tool as _core


class _BadCloseParser:
    def close(self):
        raise RuntimeError("close boom")


left, right = socket.socketpair()
fake_app = object.__new__(_core.TuningToolApp)
fake_app.running = False
fake_app.server_connections = {1: (left, ("local", 0))}
fake_app.log_queue = _queue.Queue()
fake_app._new_stream_parser = lambda: _BadCloseParser()
fake_app._sse_broadcast_link = lambda: None
fake_app.handle_client(1, left, ("local", 0))
right.settimeout(1.0)
try:
    peer_eof = right.recv(1) == b""
except OSError:
    peer_eof = True
right.close()
expect("9b. 扩展 parser.close 抛错仍释放连接",
       1 not in fake_app.server_connections and peer_eof)

# shutdown 的单步失败必须继续后续步骤，且 windowed headless 无 stderr 时
# 诊断仍直接落 bridge_headless.log。
with tempfile.TemporaryDirectory(prefix="tt_cleanup_") as td:
    cleanup_app = object.__new__(_core.TuningToolApp)
    cleanup_app._cleaned = False
    cleanup_app.headless = True
    cleanup_app._pump_stop = threading.Event()
    cleanup_app.running = True
    cleanup_app.sim_running = True
    cleanup_app.http_server = None
    cleanup_app.bridge_ext = None
    cleanup_app._headless_log_path = Path(td) / "bridge_headless.log"
    cleanup_app.close_all_sockets = lambda: (_ for _ in ()).throw(
        RuntimeError("tcp cleanup boom"))
    cleanup_app._cleanup_steps()
    cleanup_log = cleanup_app._headless_log_path.read_text(encoding="utf-8")
    expect("9c. shutdown 单步失败继续清理并落 headless 日志",
           cleanup_app._pump_stop.is_set() and not cleanup_app.running
           and not cleanup_app.sim_running and "tcp cleanup boom" in cleanup_log,
           cleanup_log)

# ===========================================================================
# 主沙箱:旧车形状配置(显式声明 bridge_extension)
# ===========================================================================
sb = make_sandbox(base_config())
brg = start_bridge(sb)
try:
    # 10a. 扩展已加载 + snapshot 暴露状态
    ext = get("/snapshot")["bridge"]["ext"]
    expect("10a. 扩展加载且 snapshot 可见", ext.get("state") == "loaded",
           str(ext))

    peer = FakePeer()

    # ---- 1. 5000 点窗口算术(分批 + 轮询确认) ---------------------------
    BATCH, TARGET = 500, 5010
    ok_flow = True
    sent = 0
    while sent < TARGET:
        n = min(BATCH, TARGET - sent)
        payload = ''.join(f"TELPT,px={sent + i},py=0.5\r\n" for i in range(n))
        peer.send_raw(payload.encode())
        sent += n
        t0 = time.time()
        while time.time() - t0 < 15:
            try:
                if traj_count() >= sent:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if traj_count() < sent:
            ok_flow = False
            break
    expect("1a. 5010 点分批全部入窗(节奏受控)", ok_flow
           and traj_count() == TARGET, f"count={traj_count()}")

    d = get("/trajectory?since=0")
    expect("1b. since=0: count=5010/窗口 5000/首点序=10",
           d["count"] == 5010 and len(d["points"]) == 5000
           and d["points"][0]["px"] == 10, f"count={d['count']} n={len(d['points'])} px0={d['points'][0]['px'] if d['points'] else None}")
    d = get("/trajectory?since=5")
    expect("1c. since=5(低于窗起点)= 全窗 5000",
           len(d["points"]) == 5000 and d["points"][0]["px"] == 10,
           f"n={len(d['points'])}")
    d = get("/trajectory?since=4995")
    expect("1d. since=4995 = 末 15 点且首点 px=4995",
           len(d["points"]) == 15 and d["points"][0]["px"] == 4995,
           f"n={len(d['points'])}")
    code_, body = post("/trajectory/clear", {})
    expect("1e. clear 复位", code_ == 200 and body["status"] == "cleared")
    d0 = get("/trajectory?since=0")
    d5010 = get("/trajectory?since=5010")
    expect("1f. clear 后新旧游标均空", d0["count"] == 0 and d0["points"] == []
           and d5010["points"] == [],
           f"since0={len(d0['points'])} since5010={len(d5010['points'])}")
    code_, body = post("/trajectory/save", {})
    expect("1g. clear 后无点可存 → 400", code_ == 400,
           f"code={code_} body={body}")

    # ---- 2. TELPT 到 SSE(协议级解析,不匹配紧凑字符串) -------------------
    sse = socket.create_connection(("127.0.0.1", 9898), timeout=5)
    sse.sendall(b"GET /events HTTP/1.1\r\nHost: x\r\n"
                b"Accept: text/event-stream\r\n\r\n")
    time.sleep(0.3)
    peer.send_line("TELPT,px=999,py=1.5")
    sse_buf = bytearray()
    telpt_frame = None
    t0 = time.time()
    while time.time() - t0 < 8 and telpt_frame is None:
        try:
            chunk = sse.recv(8192)
        except socket.timeout:
            continue
        if not chunk:
            break
        sse_buf.extend(chunk)
        text = sse_buf.decode('utf-8', errors='replace')
        # SSE 事件以空行组帧;逐事件提取 data: 行
        while '\n\n' in text:
            event, _, text = text.partition('\n\n')
            data_lines = [ln[5:].lstrip(' ') for ln in event.split('\n')
                          if ln.startswith('data:')]
            if not data_lines:
                continue
            try:
                obj = json.loads('\n'.join(data_lines))
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "TELPT":
                telpt_frame = obj
                break
        sse_buf = bytearray(text.encode('utf-8'))
    sse.close()
    expect("2. TELPT 实际到达 SSE(空行组帧+json 解析)",
           telpt_frame is not None
           and telpt_frame.get("fields", {}).get("px") == 999,
           str(telpt_frame)[:200])

    # ---- 3. 相机 ACK 双路径 -----------------------------------------------
    code_, body = post("/command", {"command": "START STREAM CAMERA_FRAME 100"})
    expect("3a. 订阅命令受理", code_ == 200, str(body))
    arrived = peer.wait_cmd("START STREAM CAMERA_FRAME")
    expect("3b. 命令到达假固件", arrived, str(peer.rx_lines[-3:]))
    peer.send_line("ACK,cmd=START_STREAM_CAMERA_FRAME,requested_ms=100,effective_ms=100")
    sub = ok3 = False
    cmd_res = {}
    t0 = time.time()
    while time.time() - t0 < 5:
        st = get("/camera/status")
        cmd_res = (get("/latest").get("cmd_result") or {})
        sub = bool(st.get("subscribed")) and st.get("requested_period_ms") == 100
        ok3 = cmd_res.get("cmd") == "START_STREAM_CAMERA_FRAME" \
            and cmd_res.get("ack") == "ACK"
        if sub and ok3:
            break
        time.sleep(0.2)
    expect("3c. ACK 更新相机订阅状态(on_line 未吞)", sub, str(st))
    expect("3d. ACK 同时进 cmd_result(正常 dispatch 保留)", ok3, str(cmd_res))

    # ---- 4. CIMG 帧管线(分片) ---------------------------------------------
    peer.send_line("CAMMETA,rev=2")
    peer.send_line("CAMSTAT,frame=7,tx_skip=0")
    frame = CAMERA_MAGIC + struct.pack('<III', 7, 12345, CAMERA_PAYLOAD_LEN) \
        + b'\x00' * CAMERA_PAYLOAD_LEN
    third = len(frame) // 3
    peer.send_raw(frame[:third])
    time.sleep(0.2)
    peer.send_raw(frame[third:2 * third])
    time.sleep(0.2)
    peer.send_raw(frame[2 * third:])
    recv7 = False
    t0 = time.time()
    while time.time() - t0 < 6:
        st = get("/camera/status")
        if st.get("received", 0) >= 1 and st.get("latest_sequence") == 7:
            recv7 = True
            break
        time.sleep(0.2)
    expect("4. 分片 CIMG 帧被接收(seq=7)", recv7, str(st))

    # ---- 5a. 坏 payload_len → 协议错误 + 关连接 + 桥存活 -------------------
    err0 = get("/camera/status").get("protocol_errors", 0)
    peer.send_raw(CAMERA_MAGIC + struct.pack('<III', 8, 1, 12345) + b'x' * 16)
    closed = False
    t0 = time.time()
    while time.time() - t0 < 5:
        if peer.closed:
            closed = True
            break
        time.sleep(0.1)
    expect("5a. 坏长度帧关连接", closed)
    err1 = get("/camera/status").get("protocol_errors", 0)
    expect("5a2. protocol_errors 计数前进", err1 >= err0 + 1,
           f"{err0} -> {err1}")
    expect("5a3. 桥存活(可再连)", get_code("/status") == 200)

    # ---- 5b. 断连半帧 → partial 计数 --------------------------------------
    part0 = get("/camera/status").get("partial", 0)
    p2 = FakePeer()
    p2.send_raw(CAMERA_MAGIC + b'\x01\x02')   # 半个头
    time.sleep(0.3)
    p2.close()
    time.sleep(0.8)
    part1 = get("/camera/status").get("partial", 0)
    expect("5b. 断连半帧 partial 计数", part1 >= part0 + 1,
           f"{part0} -> {part1}")

    # ---- 5c. 帧超时(>5s 无后续)→ 关连接 ---------------------------------
    p3 = FakePeer()
    p3.send_raw(CAMERA_MAGIC + struct.pack('<III', 9, 1, CAMERA_PAYLOAD_LEN)
                + b'\x00' * 100)   # 帧头 + 少量 payload,不完整
    closed = False
    t0 = time.time()
    while time.time() - t0 < 9:
        if p3.closed:
            closed = True
            break
        time.sleep(0.3)
    expect("5c. 半帧超时(>5s)关连接", closed)
    p3.close()

    # ---- 11. 持久化往返 + 边界 --------------------------------------------
    peer.send_line("TELPT,px=1,py=2")
    t0 = time.time()
    while time.time() - t0 < 5 and traj_count() < 1:
        time.sleep(0.2)
    code_, body = post("/trajectory/save", {"name": "round trip"})
    expect("11a. save(活点)", code_ == 200 and body.get("count") == 1, str(body))
    fname = body.get("filename", "")
    lst = get("/trajectory/list")
    expect("11b. list 可见", any(f["filename"] == fname for f in lst["files"]),
           str(lst))
    code_, loaded = post("/trajectory/load", {"filename": fname})
    expect("11c. load 回读", code_ == 200 and loaded.get("name") == "round trip"
           and loaded.get("points"), str(loaded)[:150])
    code_, body = post("/trajectory/load", {"filename": "../config.json"})
    expect("11e. 路径穿越拒绝 400", code_ == 400, str(body))
    code_, body = post("/trajectory/load", {"filename": "nope.json"})
    expect("11f. 不存在文件 404", code_ == 404, str(body))
    code_, body = post("/trajectory/delete", {"filename": "nope.json"})
    expect("11g. delete 不存在 404", code_ == 404, str(body))
    # 坏 JSON:直接落一个垃圾文件再 load → 500
    traj_dir = sb / "runtime" / "trajectories"
    (traj_dir / "bad.json").write_text("{not json", encoding="utf-8")
    code_, body = post("/trajectory/load", {"filename": "bad.json"})
    expect("11h. 坏 JSON load → 500", code_ == 500, str(code_))
    code_, camera_saved = post("/camera/save", {"label": "suite"})
    camera_path = Path(camera_saved.get("path", "")) if camera_saved else Path()
    expect("11i. /camera/save 成功并创建保存目录",
           code_ == 200 and camera_saved.get("status") == "saved"
           and camera_path.is_dir(),
           f"code={code_} body={camera_saved}")
    expect("11i2. 相机保存目录含已接收帧产物",
           camera_path.is_dir()
           and (camera_path / "latest.json").is_file()
           and (camera_path / "frames").is_dir()
           and any((camera_path / "frames").iterdir()),
           str(camera_path))
    code_, body = post("/trajectory/delete", {"filename": fname})
    expect("11j. delete 常规", code_ == 200, str(body))

    # ---- 10b/c/d. headless 生命周期 ---------------------------------------
    csf = sb / "runtime" / "latest_tuning_state.json"
    expect("10b. custom_state 文件已写(GUI 副作用保序)",
           csf.exists() and csf.stat().st_size > 0)
    peer.close()
    rc = stop_bridge(brg)
    logf = sb / "runtime" / "bridge_headless.log"
    expect("10c. /shutdown 5s 内退出码 0", rc == 0, f"rc={rc}")
    expect("10d. headless 日志文件非空(无控制台契约)",
           logf.exists() and logf.stat().st_size > 0)
except Exception as e:
    import traceback
    traceback.print_exc()
    expect("主沙箱无异常", False, repr(e))
    try:
        brg.kill()
    except Exception:
        pass

# ===========================================================================
# 6/7. 配置三态矩阵(同一沙箱,重写 config 重启桥)
# ===========================================================================
def relaunch(cfg_dict, extra_args=(), mutate=None):
    (sb / "config.json").write_text(
        json.dumps(cfg_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    if mutate:
        mutate()
    p = start_bridge(sb, extra_args)
    ext = get("/snapshot")["bridge"]["ext"]
    code = get_code("/trajectory")
    rc = stop_bridge(p)
    return ext, code, rc

# 7a. 旧 dist 升级:缺键(UNSET)+ 根布局 + bridge_ext.py 在 → 约定名加载
cfg = base_config()
del cfg["bridge_extension"]
ext, code, rc = relaunch(cfg)
expect("7a. 缺键根布局 → 约定名加载(旧 dist 升级)",
       ext.get("state") == "loaded" and ext.get("source") == "convention"
       and code == 200, f"ext={ext} code={code}")

# 7b. 显式 null → 干净核
ext, code, rc = relaunch(base_config(bridge_extension=None))
expect("7b. 显式 null → 无扩展 404",
       ext.get("state") == "off" and "explicitly" in str(ext.get("reason", ""))
       and code == 404, f"ext={ext} code={code}")

# 7c. 命名 profile 缺键 → 无扩展(兼容范围只限根布局)
prof = sb / "profiles" / "testp"
prof.mkdir(parents=True, exist_ok=True)
pcfg = base_config()
del pcfg["bridge_extension"]
(prof / "config.json").write_text(
    json.dumps(pcfg, ensure_ascii=False, indent=2), encoding="utf-8")
shutil.copy2(ROOT / "control_schema.json", prof / "control_schema.json")
ext, code, rc = relaunch(base_config(), extra_args=("--profile", "testp"))
expect("7c. 命名 profile 缺键 → 无扩展 404",
       ext.get("state") == "off" and "named profile" in str(ext.get("reason", ""))
       and code == 404, f"ext={ext} code={code}")

# 6a. 声明但文件不存在
ext, code, rc = relaunch(base_config(bridge_extension="missing_ext.py"))
expect("6a. 缺文件 → failed + 桥存活 404",
       ext.get("state") == "failed" and "not found" in str(ext.get("reason", ""))
       and code == 404 and rc == 0, f"ext={ext} code={code} rc={rc}")

# 6b. 语法坏文件
(sb / "broken_ext.py").write_text("def oops(:\n", encoding="utf-8")
ext, code, rc = relaunch(base_config(bridge_extension="broken_ext.py"))
expect("6b. 语法错 → failed + 桥存活 404",
       ext.get("state") == "failed" and "SyntaxError" in str(ext.get("reason", ""))
       and code == 404 and rc == 0, f"ext={ext}")

# 6c. EXT_API 不等 1
(sb / "badapi_ext.py").write_text(
    "EXT_API = 2\n", encoding="utf-8")
ext, code, rc = relaunch(base_config(bridge_extension="badapi_ext.py"))
expect("6c. EXT_API != 1 → failed(等值校验)",
       ext.get("state") == "failed" and "EXT_API" in str(ext.get("reason", ""))
       and code == 404 and rc == 0, f"ext={ext}")

# 6d. init 抛异常 → 降级不炸桥
(sb / "badinit_ext.py").write_text(
    "EXT_API = 1\n\ndef init(app):\n    raise RuntimeError('boom')\n",
    encoding="utf-8")
ext, code, rc = relaunch(base_config(bridge_extension="badinit_ext.py"))
expect("6d. init 抛异常 → failed 降级 + 桥存活",
       ext.get("state") == "failed" and "init failed" in str(ext.get("reason", ""))
       and rc == 0, f"ext={ext} rc={rc}")

shutil.rmtree(sb, ignore_errors=True)

print()
if fails:
    print("BRIDGE-EXT-FAIL", len(fails), fails)
    sys.exit(1)
print("BRIDGE-EXT-OK 全 PASS")
