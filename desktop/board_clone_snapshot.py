#!/usr/bin/env python3
"""Capture a read-only board parameter snapshot over the vehicle TCP link."""

from __future__ import annotations

import argparse
import json
import socket
import time
from pathlib import Path


def parse_value(value: str):
    try:
        if any(ch in value for ch in ".eE"):
            return float(value)
        return int(value, 10)
    except ValueError:
        return value


def parse_line(line: str) -> dict:
    parts = line.split(",")
    parsed = {"_packet": parts[0]}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            parsed[key] = parse_value(value)
    return parsed


class LineSocket:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buffer = ""

    def send(self, command: str) -> None:
        self.sock.sendall((command.rstrip("\r\n") + "\r\n").encode("utf-8"))

    def receive_available(self, timeout_s: float, quiet_s: float = 0.35) -> list[str]:
        deadline = time.monotonic() + timeout_s
        last_rx = None
        lines: list[str] = []
        while time.monotonic() < deadline:
            if last_rx is not None and (time.monotonic() - last_rx) >= quiet_s:
                break
            self.sock.settimeout(min(0.15, max(0.01, deadline - time.monotonic())))
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("vehicle disconnected during snapshot")
            last_rx = time.monotonic()
            self.buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in self.buffer:
                raw, self.buffer = self.buffer.split("\n", 1)
                line = raw.rstrip("\r").strip()
                if line:
                    lines.append(line)
        return lines

    def command(self, command: str, timeout_s: float = 4.0) -> list[str]:
        self.send("HB")
        self.send(command)
        lines = self.receive_available(timeout_s)
        if not lines:
            raise TimeoutError(f"no response for {command}")
        return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--accept-timeout", type=float, default=30.0)
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent / "recordings"))
    parser.add_argument("--label", default="board_clone")
    args = parser.parse_args()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(1)
    server.settimeout(args.accept_timeout)
    print(f"[WAIT] vehicle on {args.host}:{args.port}", flush=True)

    sock, peer = server.accept()
    print(f"[OK] connected: {peer[0]}:{peer[1]}", flush=True)
    link = LineSocket(sock)
    captured: dict = {
        "meta": {
            "source": "board_clone_snapshot",
            "captured_at": time.strftime("%Y%m%d-%H%M%S"),
            "peer": f"{peer[0]}:{peer[1]}",
            "read_only": True,
        },
        "gear_summary": None,
        "gears": {"S1": {}, "S3": {}},
        "parameters": {},
        "raw_responses": {},
        "clone_notes": {
            "pedal_adc_cal": "NEW release=1000 full=4000; RAM-only boot default",
            "board_specific": ["GET_YAW", "GET_TURN_ABSCAL"],
        },
    }

    try:
        lines = link.command("GET GEAR")
        captured["raw_responses"]["GET GEAR"] = lines
        captured["gear_summary"] = next(
            (parse_line(line) for line in lines if line.startswith("ACK,cmd=GET_GEAR")), None
        )

        for subject in ("S1", "S3"):
            for gear in range(1, 8):
                command = f"GET GEAR {subject} {gear}"
                lines = link.command(command)
                parsed = [parse_line(line) for line in lines]
                summary = next(
                    (item for item in parsed
                     if item.get("_packet") == "ACK" and item.get("cmd") == "GET_GEAR"),
                    None,
                )
                fields: dict = {}
                for item in parsed:
                    if item.get("_packet") != "GEARFIELDS":
                        continue
                    for key, value in item.items():
                        if key not in ("_packet", "subject", "gear"):
                            fields[key] = value
                if summary is None or not fields:
                    raise RuntimeError(f"incomplete GEARFIELDS response for {subject} gear {gear}")
                captured["gears"][subject][str(gear)] = {
                    "summary": summary,
                    "fields": fields,
                }
                print(f"[OK] {subject} G{gear} crc={summary.get('crc')}", flush=True)

        commands = (
            "GET MISSION PARAMS",
            "GET ONCE STATS",
            "GET MISSION",
            "GET TURN PID",
            "GET DRIVE LEFT PID",
            "GET DRIVE RIGHT PID",
            "GET TURN ABSCAL",
            "GET YAW",
        )
        for command in commands:
            lines = link.command(command, timeout_s=5.0)
            captured["raw_responses"][command] = lines
            captured["parameters"][command] = [parse_line(line) for line in lines]
            print(f"[OK] {command}", flush=True)
    finally:
        try:
            sock.close()
        finally:
            server.close()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = captured["meta"]["captured_at"]
    out_path = out_dir / f"parameter_snapshot_{ts}_{args.label}.json"
    out_path.write_text(json.dumps(captured, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[SAVED] {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
