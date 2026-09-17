# -*- coding: utf-8 -*-
"""P0 e2e:虚拟设备 + 桥(profile=virtual),验证桥内护栏强制 + 审计。"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:9898"
fails = []


def expect(name, cond, detail=""):
    if cond:
        print("PASS", name)
    else:
        print("FAIL", name, detail)
        fails.append(name)


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


def get(path, timeout=5):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read())


print("启动 virtual_device ...")
dev = subprocess.Popen([sys.executable, "profiles/virtual/virtual_device.py"],
                       cwd=ROOT, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
time.sleep(0.8)
print("启动 bridge --profile virtual --headless ...")
brg = subprocess.Popen([sys.executable, "tuning_tool.py", "--profile", "virtual",
                        "--headless"],
                       cwd=ROOT, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
brg_ok = False
try:
    for _ in range(30):
        try:
            st = get("/status")
            if st.get("connections"):
                brg_ok = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    expect("桥+设备上线", brg_ok)
    if brg_ok:
        # 等连接稳定(设备连接初期有重连窗口,静默命令可能落旧 socket)
        time.sleep(2.0)

    if brg_ok:
        # 1. 值域违规(virtual kp max=50)
        code, body = post("/batch", {"commands": [{"cmd": "SET kp 100",
                                                   "expect": "ACK"}]})
        expect("桥拒值域违规(kp=100>50)", code == 400
               and body and "guardrail" in body.get("error", ""), str(body))
        # 2. 合法设参放行 + ACK 送达(csv 模式自动附 !<seq>,设备回 OK,seq)
        code, body = post("/batch", {"commands": [{"cmd": "SET kp 5.0",
                                                   "expect": "ACK"}]})
        expect("合法设参放行且 ACK", code == 200
               and body["results"][0]["status"] == "ack", str(body))
        # params 通道 5Hz,轮询读回(最多 4s)
        kp_val = None
        for _ in range(8):
            time.sleep(0.5)
            kp = (get("/latest").get("packets") or {}).get("params") or {}
            kp_val = kp.get("kp")
            if kp_val is not None and abs(float(kp_val) - 5.0) < 1e-6:
                break
        expect("params 读回 kp=5.0", kp_val is not None
               and abs(float(kp_val) - 5.0) < 1e-6, str(kp))
        # 3. max_step: 当前值来自 params 读回(max_speed=100, limit=100)
        time.sleep(2.0)
        code, body = post("/batch", {"commands": [{"cmd": "SET max_speed 500",
                                                   "expect": "ACK"}]})
        expect("桥拒步长违规(max_speed 100->500 > 100)", code == 400, str(body))
        code, body = post("/batch", {"commands": [{"cmd": "SET max_speed 150"}]})
        expect("步长内变更放行(100->150 <= 100)", code == 200, str(body))
        # 4. 非设参命令放行
        code, body = post("/batch", {"commands": [{"cmd": "RATE ctl 50"}]})
        expect("非设参命令放行", code == 200, str(body))
        # 5. /command 端点同样强制
        code, body = post("/command", {"command": "SET kp 999"})
        expect("/command 同样强制", code == 400, str(body))
        # 6. 审计: 桥 rejected 记录存在
        audit_p = ROOT / "runtime" / "audit.jsonl"
        lines = audit_p.read_text(encoding="utf-8").splitlines()
        rej = [ln for ln in lines if "\"actor\": \"bridge\"" in ln
               and "rejected" in ln]
        expect("桥拒绝事件已审计", len(rej) >= 3, "found %d" % len(rej))
finally:
    brg.terminate()
    dev.terminate()
    time.sleep(0.3)
print()
if fails:
    print("E2E-FAIL", len(fails), fails)
    sys.exit(1)
print("E2E-OK 全 PASS")
