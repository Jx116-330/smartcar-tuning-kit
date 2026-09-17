# -*- coding: utf-8 -*-
"""冻结态 headless 验收(tests/test_frozen_headless.py,2026-09-14 设计文档 §5)。

对两个 PyInstaller 产物各跑一遍 headless 契约:
  - YawTuningTool.exe(console=False,主产物——无 stdout/stderr 场景才是
    本套件的核心目标:日志必须落文件,/shutdown 必须能退出)
  - YawTuningToolConsole.exe(console=True,服务场景推荐形态;额外断言
    stdout 有内容)

产物新鲜度:exe 不得早于对应源码/spec，dist/bridge_ext.py 必须与源码
逐字节一致；运行后还要核对 /snapshot 的 bridge.version。任一不符都必须
跳过/要求重建，发布门下直接失败。

日常回归:缺产物 → 打印 FROZEN-SKIP 清单,退出 0(runner 据此分级汇总)。
发布验收:python tests/test_frozen_headless.py --require(或 RELEASE=1)
  → 任一产物缺失/过期即 FAIL,退出 1。流程:build.bat 后立刻跑本命令。
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BASE = "http://127.0.0.1:9898"
FRESHNESS = DIST / "bridge_ext.py"
EXPECTED_BRIDGE_VERSION = "2026-09-14.bridge-ext-v1"
EXES = [("YawTuningTool.exe", False),      # (名称, 断言 stdout 有内容)
        ("YawTuningToolConsole.exe", True)]
require = ("--require" in sys.argv) or bool(os.environ.get("RELEASE"))
fails = []
skipped = []


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
        return e.code, None


class FakePeer:
    def __init__(self):
        self.sock = socket.create_connection(("127.0.0.1", 8080), timeout=5)
        self.sock.settimeout(1.0)
        self.rx_lines = []
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
                break
            buf.extend(data)
            while b'\n' in buf:
                ln, _, rest = buf.partition(b'\n')
                buf = bytearray(rest)
                text = ln.rstrip(b'\r').decode('utf-8', errors='replace').strip()
                if text:
                    self.rx_lines.append(text)

    def send_line(self, text):
        self.sock.sendall((text + '\r\n').encode('utf-8'))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def stale_reasons(exe: Path, spec_name: str):
    reasons = []
    required = [ROOT / "tuning_tool.py", ROOT / "config_loader.py",
                ROOT / spec_name]
    if not FRESHNESS.is_file():
        reasons.append("dist/bridge_ext.py missing")
    elif FRESHNESS.read_bytes() != (ROOT / "bridge_ext.py").read_bytes():
        reasons.append("dist/bridge_ext.py differs from source")
    if exe.is_file():
        newest_input = max(p.stat().st_mtime for p in required)
        if exe.stat().st_mtime < newest_input:
            reasons.append("exe older than source/spec")
    return reasons


def check_exe(name, want_stdout):
    exe = DIST / name
    spec_name = "smartcar_console.spec" if want_stdout else "smartcar.spec"
    stale = [] if not exe.is_file() else stale_reasons(exe, spec_name)
    if not exe.is_file() or stale:
        skipped.append(name)
        why = "missing" if not exe.is_file() else ", ".join(stale)
        print(f"FROZEN-SKIP {name}(产物缺失或过期:{why};先 build.bat 重建)")
        return
    print(f"== 冻结验收 {name} ==")
    p = subprocess.Popen([str(exe), "--headless"], cwd=DIST,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    peer = None
    try:
        up = False
        for _ in range(40):
            try:
                get("/status", timeout=2)
                up = True
                break
            except Exception:
                time.sleep(0.5)
        expect(f"[{name}] headless 启动(HTTP 可用)", up)
        if not up:
            return

        # 约定名兼容是冻结态的默认路径:dist/config.json 若为旧版无键,
        # bridge_ext.py 在 exe 旁 → 约定加载,轨迹端点必须活着
        ext = get("/snapshot")["bridge"]["ext"]
        expect(f"[{name}] 扩展加载(dist 旧配置走约定名)",
               ext.get("state") == "loaded", str(ext))
        version = get("/snapshot")["bridge"].get("version")
        expect(f"[{name}] 核版本与本次发布一致",
               version == EXPECTED_BRIDGE_VERSION, str(version))

        peer = FakePeer()
        for i in range(5):
            peer.send_line(f"TELPT,px={i},py=0.25")
        n = None
        t0 = time.time()
        while time.time() - t0 < 8:
            try:
                n = get("/trajectory?since=0")
                if n["count"] >= 5:
                    break
            except Exception:
                pass
            time.sleep(0.3)
        expect(f"[{name}] TELPT 入窗且 /trajectory 200",
               n is not None and n["count"] == 5 and len(n["points"]) == 5,
               str(n))

        code_, _ = post("/shutdown", {})
        try:
            rc = p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
            rc = None
        expect(f"[{name}] /shutdown 退出码 0", code_ == 200 and rc == 0,
               f"http={code_} rc={rc}")

        logf = DIST / "runtime" / "bridge_headless.log"
        expect(f"[{name}] 日志文件契约(无控制台也落盘)",
               logf.exists() and logf.stat().st_size > 0)

        if want_stdout:
            out = (p.stdout.read() or b'').decode('utf-8', errors='replace')
            expect(f"[{name}] console 构建 stdout 有内容", len(out.strip()) > 0,
                   out[:120])
        else:
            p.stdout.close()
    finally:
        if peer:
            peer.close()
        if p.poll() is None:
            p.kill()
        time.sleep(1.0)   # 端口释放窗口


for exe_name, want_stdout in EXES:
    check_exe(exe_name, want_stdout)

print()
if require and skipped:
    print("FROZEN-FAIL 发布门:产物缺失/过期 ->", skipped)
    print("FROZEN-RESULT: fail skipped=%s" % json.dumps(skipped))
    sys.exit(1)
if fails:
    print("FROZEN-FAIL", len(fails), fails)
    print("FROZEN-RESULT: fail skipped=%s" % json.dumps(skipped))
    sys.exit(1)
if skipped:
    # 日常模式:SKIP 必须显式传播,runner 不得报成 PASS
    print("FROZEN-RESULT: skip skipped=%s" % json.dumps(skipped))
    sys.exit(0)
print("FROZEN-RESULT: ok skipped=[]")
print("FROZEN-OK 双 exe 全 PASS")
