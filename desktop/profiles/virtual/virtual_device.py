#!/usr/bin/env python3
"""
虚拟被调对象 — 接入契约 v1 的参考实现(docs/protocol_contract_v1.md)。

扮演一个"固件":以 TCP 客户端身份连接桥的 8080(与真实拓扑一致:车连 PC),
按契约推 D 帧(ctl 50Hz / params 5Hz / stats 1Hz),处理 SET / RATE / GET / PING。

用法:
    python profiles/virtual/virtual_device.py [--host 127.0.0.1] [--port 8080]

容错语义:发送失败只计数不断连;桥断开就重连;命令不带 !<seq> 就静默。
"""

import argparse
import math
import random
import socket
import threading
import time

DEFAULT_PARAMS = {
    'kp': 1.0, 'ki': 0.0, 'kd': 0.1,
    'max_speed': 100.0, 'amp': 50.0, 'freq': 0.5, 'noise': 1.0,
}
DEFAULT_RATES = {'ctl': 50, 'params': 5, 'stats': 1}
TICK_S = 0.01


class VirtualDevice:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock = None
        self.lock = threading.Lock()
        self.params = dict(DEFAULT_PARAMS)
        self.rates = dict(DEFAULT_RATES)
        self.running = True
        self._rbuf = bytearray()
        self._t0 = time.monotonic()
        self._t_ms = 0
        self._frames_sent = 0
        self._frames_dropped = 0
        self._pos = 0.0
        self._err_prev = 0.0
        self._integ = 0.0
        self._next_due = {k: 0.0 for k in self.rates}

    # ---------------- 连接管理 ----------------
    def _connect_loop(self):
        while self.running:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1.0)
                s.connect((self.host, self.port))
                with self.lock:
                    self.sock = s
                print(f'[virtual] connected to {self.host}:{self.port}')
                self._rx_loop(s)
            except OSError:
                pass
            with self.lock:
                self.sock = None
            if self.running:
                print('[virtual] link lost, reconnecting in 1s ...')
                time.sleep(1.0)

    def _rx_loop(self, s):
        while self.running:
            try:
                data = s.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            self._rbuf.extend(data)
            while True:
                nl = self._rbuf.find(b'\n')
                if nl < 0:
                    break
                line = self._rbuf[:nl].decode('utf-8', 'replace').strip()
                del self._rbuf[:nl + 1]
                if line:
                    self.handle_line(line)

    # ---------------- 命令处理(契约 §2.1) ----------------
    def handle_line(self, line: str):
        seq = None
        if ' !' in line:
            body, _, tail = line.partition(' !')
            tail = tail.strip()
            if tail.isdigit():
                seq = int(tail)
                line = body
            else:
                line = body + ' !' + tail
        parts = line.split()
        if not parts:
            return
        verb = parts[0].upper()
        reply = None

        if verb == 'SET' and len(parts) >= 3:
            key = parts[1].lower()
            try:
                val = float(parts[2])
            except ValueError:
                reply = ('ERR', 'NAN')
            else:
                with self.lock:
                    if key in self.params:
                        self.params[key] = val
                        reply = ('OK', None)
                    else:
                        reply = ('ERR', 'UNKNOWN_KEY')
        elif verb == 'RATE' and len(parts) >= 3:
            chan = parts[1].lower()
            try:
                hz = float(parts[2])
            except ValueError:
                reply = ('ERR', 'NAN')
            else:
                if chan in self.rates:
                    with self.lock:
                        self.rates[chan] = max(0.0, min(1000.0, hz))
                    reply = ('OK', None)
                else:
                    reply = ('ERR', 'UNKNOWN_CHAN')
        elif verb == 'GET' and len(parts) >= 2:
            key = parts[1].lower()
            with self.lock:
                val = self.params.get(key)
            if val is not None:
                self.send_line(f'P,{key},{val:g}')
            else:
                reply = ('ERR', 'UNKNOWN_KEY')
        elif verb == 'PING':
            reply = ('OK', None)
        else:
            reply = ('ERR', 'UNKNOWN_COMMAND')

        if seq is None:
            return
        if reply is None:
            self.send_line(f'OK,{seq}')
        elif reply[1] is None:
            self.send_line(f'{reply[0]},{seq}')
        else:
            self.send_line(f'{reply[0]},{seq},{reply[1]}')

    def send_line(self, text: str):
        with self.lock:
            s = self.sock
        if s is None:
            self._frames_dropped += 1
            return
        try:
            s.sendall((text + '\n').encode('utf-8'))
            self._frames_sent += 1
        except OSError:
            self._frames_dropped += 1

    # ---------------- 仿真与通道调度(契约 §2.2) ----------------
    def _step_sim(self, dt: float):
        p = self.params
        t = time.monotonic() - self._t0
        setpoint = p['amp'] * math.sin(2 * math.pi * p['freq'] * t)
        err = setpoint - self._pos
        deriv = (err - self._err_prev) / dt if dt > 0 else 0.0
        self._integ += p['ki'] * err * dt
        self._integ = max(-100.0, min(100.0, self._integ))
        duty = max(-100.0, min(100.0,
                               p['kp'] * err + self._integ + p['kd'] * deriv))
        self._pos += (duty / 100.0) * p['max_speed'] * dt
        if p['noise'] > 0:
            self._pos += random.uniform(-1, 1) * p['noise'] * dt
        self._err_prev = err
        return setpoint, self._pos, err, duty

    def _tx_loop(self):
        last = time.monotonic()
        while self.running:
            now = time.monotonic()
            dt = min(0.05, now - last)
            last = now
            self._t_ms = int((now - self._t0) * 1000)
            setpoint, pos, err, duty = self._step_sim(dt)
            with self.lock:
                rates = dict(self.rates)
                params = dict(self.params)
            for chan, hz in rates.items():
                if hz <= 0:
                    continue
                due = self._next_due.get(chan, 0.0)
                if now >= due:
                    self._next_due[chan] = due + 1.0 / hz
                    self._emit(chan, setpoint, pos, err, duty, params)
            time.sleep(TICK_S)

    def _emit(self, chan, setpoint, pos, err, duty, params):
        t = self._t_ms
        if chan == 'ctl':
            self.send_line(f'D,ctl,{t},{setpoint:g},{pos:g},{err:g},{duty:g}')
        elif chan == 'params':
            self.send_line(
                f'D,params,{t},{params["kp"]:g},{params["ki"]:g},'
                f'{params["kd"]:g},{params["max_speed"]:g},{params["amp"]:g},'
                f'{params["freq"]:g},{params["noise"]:g}')
        elif chan == 'stats':
            self.send_line(
                f'D,stats,{t},{t / 1000.0:g},{self._frames_sent},{self._frames_dropped}')

    def run(self):
        tx = threading.Thread(target=self._tx_loop, daemon=True)
        tx.start()
        self._connect_loop()


def main():
    ap = argparse.ArgumentParser(description='契约 v1 虚拟被调对象')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8080)
    args = ap.parse_args()
    VirtualDevice(args.host, args.port).run()


if __name__ == '__main__':
    main()
