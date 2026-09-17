"""
track_raw_backup.py - 录制线字节精确备份/恢复("重灌免重录",2026-07-15;2026-07-18 全资产)

为什么存在
----------
比赛前一天打点后,录好的线是最贵资产。倒车段绑定 2026-07-15 起钉了前进线内容指纹
fwd_crc:**重灌一条"差一个字节"的前进线 = 全部档位倒车段 STALE、FULL RUN 拒发**。
旧的 TRACK DUMP/UPLOAD 文本链路(%.6f/%.4f + 只传 5 字段)物理上不可能按位还原。
本工具走 raw 命令族,备份↔恢复全程 20 字节/点原样。

2026-07-18 扩展(换核心板/flash 全丢也不丢线):12 个 flash 槽(科一前进槽0/倒车槽1、
科二门洞 2..11)+ 科一逐档倒车段 D-Flash 仓库(7 档)全覆盖:

    备份:  [TRACK LOAD <slot> | TRACK REVWH LOAD <gear>] → TRACK CRC RAM
           → 逐点 TRACK GETPT(命令-ACK 节奏,桥零改动)→ 本地 CRC 对账落 json
    恢复:  TRACK UPLOAD BEGIN → 逐点 PTX → END(前进线)/ END REV(倒车线,固件
           2026-07-18 新命令)→ TRACK SAVE <slot> → TRACK CRC <slot> 对账
    倒车档收口: …PTX…END REV → TRACK SAVE 1
           → TRACK REVBIND <g>(显式档位+同步写 D-Flash 仓库,ACK crc 对账)
           → TRACK REVWH LOAD <g> 终账(仓库里躺的就是备份那条)
    一把梭: backup-all / restore-all(manifest 驱动,逐项 CRC,汇总 PASS/FAIL)

用法
----
    # 单线
    python track_raw_backup.py backup  --label s1_final              # 车上 RAM 线
    python track_raw_backup.py backup  --slot 0 --label s1_fwd       # flash 槽(先 TRACK LOAD)
    python track_raw_backup.py backup  --gear 3 --label g3_rev       # 仓库档 3 的倒车段
    python track_raw_backup.py restore recordings/raw_backup_<ts>_<label>.json --save-slot 0
    python track_raw_backup.py restore recordings/raw_backup_<ts>_g3_rev.json --rebind-gear 3
    python track_raw_backup.py verify  recordings/raw_backup_<ts>_<label>.json --slot 0

    # 全套(比赛前一天收工冷备 / 换板重建)
    python track_raw_backup.py backup-all  --label race_eve
    python track_raw_backup.py restore-all recordings/raw_manifest_<ts>_race_eve.json --unlock

⚠ 操作须知
----------
- 恢复要 TRACK_LOCK 解锁;给 --unlock 由脚本代发 TRACK_LOCK 0 并在收尾**必回锁**,
  不给则遇锁明说、自己手动解/锁。
- 本脚本开场发 STOP ALL STREAMS(老坑:连 SEL 订阅一起杀)——收工后记得
  重订三流 + SUB SET。
- 倒车线方向自动识别(里程递减→END REV);恢复倒车档必须 --rebind-gear。
- TRACK LOAD 载入槽时固件会做录制起点伪点清理 → 槽备份的 crc 是"载入后 RAM 视图",
  与原槽 TRACK CRC <slot> 可能差一个点(脚本两个值都打印);restore 后 SAVE 回槽的
  内容 == 备份视图,对账闭环不受影响。仓库档(REVWH LOAD)不做清理,永远按位原样。
- 备份/一把梭跑完后车上 RAM 是最后处理的那条线 —— **发车前必 TRACK LOAD 0**。
- 换板重建完整顺序(线之外的都是板级件,备份不了):①烧固件 ②SET GEAR 脚本重灌
  七档参数 ③重跑 Turn AutoCal / Gyro Cal / ABSCAL ④本工具 restore-all ⑤低速首验。
- 旧 learn_dump_*.json(文本精度)也能 restore(--from-fields),但**不保证字节
  精确** → fwd_crc 大概率不匹配 → 倒车段照样 STALE,只当灾难兜底。
"""
from __future__ import annotations

import argparse
import binascii
import json
import struct
import sys
import time
from pathlib import Path

from track_dump import http_get, http_post, send_command  # noqa: F401 (send_command 供交互提示)
from track_upload import send_and_wait

DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "recordings"
POINT_STRUCT = struct.Struct("<fffff")      # follow_point_t: s/yaw/steer/x/y = 20B
POINT_FIELDS = ("s_m", "yaw_deg", "steer_deg", "x_m", "y_m")

FORWARD_SLOTS = (0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)   # 槽1 是唯一的倒车语义槽
ALL_SLOTS = tuple(range(12))
ALL_GEARS = tuple(range(1, 8))


def crc32_hex(blob: bytes) -> str:
    return f"{binascii.crc32(blob) & 0xFFFFFFFF:08X}"


def hexval(v) -> str:
    """剥掉固件为防桥数字化而加的 'x' 前缀(见固件 raw 命令族注释),统一大写。"""
    s = str(v)
    if s[:1] in ("x", "X"):
        s = s[1:]
    return s.upper()


def decode_point(raw: bytes) -> dict:
    vals = POINT_STRUCT.unpack(raw)
    return dict(zip(POINT_FIELDS, vals))


def encode_point(fields: dict) -> bytes:
    return POINT_STRUCT.pack(*(fields[k] for k in POINT_FIELDS))


def direction_of_hexes(hexes: list[str]) -> str:
    """里程首尾定方向:递增=forward / 递减=reverse(倒车段)。相等=坏数据。"""
    first = decode_point(binascii.unhexlify(hexes[0]))["s_m"]
    last = decode_point(binascii.unhexlify(hexes[-1]))["s_m"]
    if last > first:
        return "forward"
    if last < first:
        return "reverse"
    raise ValueError("首尾里程相等,不是可恢复的线")


def bridge_ready(base: str) -> bool:
    st = http_get(f"{base}/status", timeout=2.0)
    if st is None:
        print(f"[ERR] YawTuningTool 不在 {base}(exe 开了吗?)")
        return False
    if int(st.get("connections") or 0) < 1:
        print("[ERR] 车没连上桥(connections=0)")
        return False
    return True


def session_prologue(base: str) -> None:
    send_command(base, "STOP ALL STREAMS")   # ⚠连 SEL 一起杀,收工重订
    time.sleep(0.3)


def send_retry_busy(base: str, command: str, cmd_name: str,
                    tries: int = 10, delay: float = 0.3,
                    timeout_s: float = 5.0, match: dict | None = None):
    """send_and_wait + TRACK_BUSY 自动重试(REVBIND/换档后的导出账下一拍才清)。"""
    cr = None
    for _ in range(tries):
        ok, cr = send_and_wait(base, command, cmd_name,
                               timeout_s=timeout_s, match=match, quiet=True)
        if ok:
            return True, cr
        reason = ((cr or {}).get("data") or {}).get("reason", "")
        if reason != "TRACK_BUSY":
            print(f"[ERR] {command} -> {reason or 'NO_ACK'}")
            return False, cr
        time.sleep(delay)
    print(f"[ERR] {command} 重试 {tries} 次仍 TRACK_BUSY")
    return False, cr


def set_track_lock(base: str, on: bool) -> bool:
    ok, _ = send_and_wait(base, f"SET MISSION TRACK_LOCK {1 if on else 0}",
                          "SET_MISSION_TRACK_LOCK", timeout_s=5.0)
    print(f"[{'OK' if ok else 'ERR'}] TRACK_LOCK {'上锁' if on else '解锁'}"
          + ("" if ok else "(失败,手动处理)"))
    return ok


def mission_idle(base: str) -> None:
    """LOAD/UPLOAD/REVWH/REVBIND 全要 mission IDLE;ABORT 态 LOAD 被拒是老坑。"""
    send_command(base, "MISSION IDLE")
    time.sleep(0.5)


# ============================================================================
# 取数(备份公共段):RAM → 点列表 + 本地 CRC 对账
# ============================================================================

def ram_crc_query(base: str, ack_timeout: float):
    ok, cr = send_and_wait(base, "TRACK CRC RAM", "TRACK_CRC",
                           timeout_s=ack_timeout, match={"slot": -1})
    if not ok:
        return None
    d = cr.get("data") or {}
    n = int(d.get("pts") or 0)
    crc = hexval(d.get("crc") or "")
    if n < 2 or len(crc) != 8:
        print(f"[ERR] CRC RAM 应答异常: pts={n} crc={crc!r}")
        return None
    return n, crc


def pull_ram_points(base: str, ack_timeout: float):
    """RAM 线逐点 GETPT 拉回。返回 (points, car_crc) 或 None(已打印错因)。"""
    q = ram_crc_query(base, ack_timeout)
    if q is None:
        print("[ERR] TRACK CRC RAM 失败(录制中?无点?旧固件没这条命令?)")
        return None
    n, car_crc = q
    print(f"[OK] 车上 RAM 线: {n} 点, crc={car_crc}")

    blob = bytearray()
    points = []
    t0 = time.time()
    for i in range(n):
        raw_hex = None
        for attempt in range(3):
            ok, cr = send_and_wait(base, f"TRACK GETPT {i}", "TRACK_GETPT",
                                   timeout_s=ack_timeout,
                                   match={"idx": i}, quiet=True)
            if ok:
                raw_hex = hexval((cr.get("data") or {}).get("d") or "")
                if len(raw_hex) == POINT_STRUCT.size * 2:
                    break
                raw_hex = None
            time.sleep(0.1 * (attempt + 1))
        if raw_hex is None:
            print(f"[ERR] pt{i} 三次拉取失败,备份中止(已收 {len(points)} 点,未保存)")
            return None
        raw = binascii.unhexlify(raw_hex)
        blob += raw
        pt = decode_point(raw)
        pt["raw_hex"] = raw_hex
        points.append(pt)
        if i % 50 == 0 or i == n - 1:
            rate = (i + 1) / max(time.time() - t0, 1e-3)
            print(f"  ...{i + 1}/{n}  ({rate:.0f} pt/s, 剩余 ~{(n - 1 - i) / max(rate, 1e-3):.0f}s)")

    local_crc = crc32_hex(bytes(blob))
    if local_crc != car_crc:
        print(f"[ERR] 本地 CRC {local_crc} != 车端 {car_crc},传输有损,重跑")
        return None
    return points, car_crc


def write_backup_file(out_dir: Path, ts: str, label: str, points: list, crc: str,
                      source: dict, direction: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"raw_backup_{ts}_{label}.json"
    payload = {
        "meta": {
            "source": "track_raw_backup",
            "captured_at": ts,
            "label": label,
            "count": len(points),
            "crc": crc,
            "schema": "ins_record_point_t",
            "schema_version": 3,
            "raw": True,
            "direction": direction,          # forward / reverse(决定恢复走 END 还是 END REV)
            "origin": source,                # {"kind": ram|flash_slot|rev_warehouse, slot/gear, slot_crc}
            "schema_doc": ("5 个 ZUST follow_point_t 字段:s/yaw/steer/x/y;"
                           "raw_hex=20B 原样,restore 唯一权威源"),
        },
        "points": points,
    }
    out_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"[SAVED] {out_path}  ({out_path.stat().st_size} bytes)  crc={crc}  dir={direction}")
    return out_path


def slot_crc_query(base: str, slot: int, ack_timeout: float):
    ok, cr = send_and_wait(base, f"TRACK CRC {slot}", "TRACK_CRC",
                           timeout_s=ack_timeout, match={"slot": slot})
    if not ok:
        return None
    d = cr.get("data") or {}
    return int(d.get("pts") or 0), hexval(d.get("crc") or "")


def backup_one(base: str, out_dir: Path, ts: str, label: str,
               slot: int | None, gear: int | None, ack_timeout: float):
    """单线备份主体(RAM / flash 槽 / 仓库档)。返回 manifest entry 或 None。"""
    source: dict = {"kind": "ram", "slot": None, "gear": None}
    expect_crc = None

    if slot is not None:
        sq = slot_crc_query(base, slot, ack_timeout)
        ok, cr = send_and_wait(base, f"TRACK LOAD {slot}", "TRACK_LOAD",
                               timeout_s=ack_timeout, match={"slot": slot}, quiet=True)
        if not ok:
            reason = ((cr or {}).get("data") or {}).get("reason", "NO_ACK")
            print(f"[SKIP] 槽{slot}: TRACK LOAD 被拒({reason};空槽/非 IDLE)")
            return None
        source = {"kind": "flash_slot", "slot": slot, "gear": None,
                  "slot_crc": (sq[1] if sq else None)}
        if sq:
            print(f"[OK] 槽{slot} 原槽 crc={sq[1]}({sq[0]}点);载入视图若差一个点=伪点清理,正常")
    elif gear is not None:
        ok, cr = send_retry_busy(base, f"TRACK REVWH LOAD {gear}", "TRACK_REVWH_LOAD",
                                 timeout_s=ack_timeout, match={"gear": gear})
        if not ok:
            reason = ((cr or {}).get("data") or {}).get("reason", "NO_ACK")
            print(f"[SKIP] 仓库档{gear}: {reason}(该档没绑过倒车段=正常空)")
            return None
        d = cr.get("data") or {}
        expect_crc = hexval(d.get("crc") or "")
        print(f"[OK] 仓库档{gear} → RAM: {d.get('pts')}点, 文件 crc={expect_crc}")
        source = {"kind": "rev_warehouse", "slot": None, "gear": gear}

    got = pull_ram_points(base, ack_timeout)
    if got is None:
        return None
    points, crc = got
    if expect_crc and crc != expect_crc:
        print(f"[ERR] 拉回 crc={crc} != 仓库文件 crc={expect_crc},备份作废")
        return None
    direction = direction_of_hexes([p["raw_hex"] for p in points])
    if gear is not None and direction != "reverse":
        print("[ERR] 仓库档拉回的不是倒车语义线?数据异常,备份作废")
        return None
    out_path = write_backup_file(out_dir, ts, label, points, crc, source, direction)
    return {"file": out_path.name, "kind": source["kind"], "slot": slot,
            "gear": gear, "pts": len(points), "crc": crc, "direction": direction}


# ============================================================================
# backup(单线)
# ============================================================================

def backup(args) -> int:
    base = f"http://{args.http_host}:{args.http_port}"
    if args.slot is not None and args.gear is not None:
        print("[ERR] --slot 与 --gear 只能给一个")
        return 2
    if not bridge_ready(base):
        return 3
    session_prologue(base)
    if args.slot is not None or args.gear is not None:
        mission_idle(base)

    ts = time.strftime("%Y%m%d-%H%M%S")
    label = args.label
    if args.slot is not None:
        label = f"{label}_slot{args.slot}"
    if args.gear is not None:
        label = f"{label}_gear{args.gear}"
    entry = backup_one(base, Path(args.out_dir), ts, label,
                       args.slot, args.gear, args.ack_timeout)
    if entry is None:
        return 5
    print("[提醒] STOP ALL STREAMS 连 SEL 一起杀了——继续调车先重订三流 + SUB SET;"
          "RAM 现在是这条备份线,发车前 TRACK LOAD 0")
    # 归档登记只做前进主线(RAM/槽0);倒车段/门洞线登记会把 active line 指乱
    if entry["kind"] == "ram" or (entry["kind"] == "flash_slot" and entry["slot"] == 0):
        try:
            from archive import register_line, set_active_line
            raw = json.loads((Path(args.out_dir) / entry["file"]).read_text(encoding="utf-8"))
            _fp = register_line(raw["points"], args.label, "raw_backup",
                                origin_file=entry["file"])
            set_active_line(_fp, args.label, entry["pts"], "raw_backup")
        except Exception as e:  # noqa: BLE001 - 归档失败不能挡备份
            print(f"[WARN] 归档登记失败(不影响备份本身): {e}")
    return 0


# ============================================================================
# restore(单线)
# ============================================================================

def load_backup(path: str, allow_fields: bool):
    """返回 (逐点 hex 列表, 备份 crc 或 None, direction)。自检:点区 CRC + 方向一致。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    pts = raw["points"] if isinstance(raw, dict) else raw
    meta = (raw.get("meta") or {}) if isinstance(raw, dict) else {}
    hexes = []
    from_fields = 0
    for i, p in enumerate(pts):
        h = p.get("raw_hex")
        if isinstance(h, str) and len(h) == POINT_STRUCT.size * 2:
            hexes.append(h.upper())
            continue
        if not allow_fields:
            actual = len(h) // 2 if isinstance(h, str) else 0
            raise ValueError(f"pt{i} raw_hex={actual}B,当前固件要求 20B follow_point_t。"
                             "旧备份请加 --from-fields 做有损迁移")
        if all(k in p for k in POINT_FIELDS):
            fields = {k: float(p[k]) for k in POINT_FIELDS}
        elif all(k in p for k in ("enc_dist_mm", "yaw_deg", "px_m", "py_m")):
            fields = {
                "s_m": float(p["enc_dist_mm"]) / 1000.0,
                "yaw_deg": float(p["yaw_deg"]),
                "steer_deg": 0.0,
                "x_m": float(p["px_m"]),
                "y_m": float(p["py_m"]),
            }
        else:
            raise ValueError(f"pt{i} 缺少 follow_point_t 字段，也无法从旧轨迹字段迁移")
        hexes.append(binascii.hexlify(encode_point(fields)).decode().upper())
        from_fields += 1
    if from_fields:
        print(f"[WARN] {from_fields} 点由文本字段重打包(非按位原样)——恢复后 fwd_crc "
              "大概率不匹配,倒车段将 STALE,需重录(这是兜底路径的已知代价)")
    direction = direction_of_hexes(hexes)
    meta_dir = meta.get("direction")
    if meta_dir and meta_dir != direction:
        raise ValueError(f"meta.direction={meta_dir} 与点区实际方向 {direction} 矛盾(文件被改过?)")
    crc = meta.get("crc")
    return hexes, (str(crc).upper() if crc else None), direction


def push_points(base: str, hexes: list[str], direction: str, ack_timeout: float):
    """BEGIN → 逐点 PTX → END/END REV → RAM CRC 对账。返回 local_crc 或 None。"""
    n = len(hexes)
    local_crc = crc32_hex(binascii.unhexlify("".join(hexes)))

    ok, cr = send_retry_busy(base, f"TRACK UPLOAD BEGIN {n}",
                             "TRACK_UPLOAD_BEGIN", match={"count": n})
    if not ok:
        reason = ((cr or {}).get("data") or {}).get("reason", "")
        if reason == "TRACK_LOCKED":
            print("[ERR] 轨迹写保护锁着。先 `SET MISSION TRACK_LOCK 0`(或 --unlock/"
                  "菜单 Track Tools→7),恢复完**记得重新上锁**")
        return None

    t0 = time.time()
    for i, h in enumerate(hexes):
        ok, _ = send_and_wait(base, f"TRACK UPLOAD PTX {i} {h}",
                              "TRACK_UPLOAD_PTX", timeout_s=ack_timeout,
                              match={"idx": i}, quiet=True)
        if not ok:
            print(f"[ERR] pt{i} 失败 → 固件已整次作废(轨迹'无点'),重跑即可")
            send_command(base, "TRACK UPLOAD ABORT")
            return None
        if i % 50 == 0 or i == n - 1:
            rate = (i + 1) / max(time.time() - t0, 1e-3)
            print(f"  ...{i + 1}/{n}  ({rate:.0f} pt/s, 剩余 ~{(n - 1 - i) / max(rate, 1e-3):.0f}s)")

    end_cmd = "TRACK UPLOAD END REV" if direction == "reverse" else "TRACK UPLOAD END"
    end_name = "TRACK_UPLOAD_END_REV" if direction == "reverse" else "TRACK_UPLOAD_END"
    ok, _ = send_and_wait(base, end_cmd, end_name, match={"pts": n})
    if not ok:
        return None
    print(f"[OK] {end_cmd} 落账 {n} 点, 用时 {time.time() - t0:.0f}s")

    q = ram_crc_query(base, ack_timeout)
    if q is None or q[1] != local_crc:
        got = q[1] if q else "N/A"
        print(f"[ERR] RAM crc={got} != 备份 {local_crc},对账失败,别 SAVE,排查后重跑")
        return None
    print(f"[verify] RAM crc={local_crc} == 备份 ✓")
    return local_crc


def save_and_verify_slot(base: str, slot: int, n: int, local_crc: str,
                         ack_timeout: float) -> bool:
    ok, cr = send_retry_busy(base, f"TRACK SAVE {slot}", "TRACK_SAVE",
                             timeout_s=ack_timeout, match={"slot": slot})
    if not ok:
        print(f"[ERR] SAVE 槽{slot} 失败(锁着? mission 非 IDLE?)")
        return False
    saved = int((cr.get("data") or {}).get("pts") or 0)
    if saved != n:
        print(f"[FAIL] 槽{slot} 只存了 {saved}/{n} 点(超槽容量被裁切?)——非完整恢复")
        return False
    sq = slot_crc_query(base, slot, ack_timeout)
    if sq and sq[1] == local_crc:
        print(f"[PASS] 槽{slot} crc={sq[1]} == 备份 —— 字节精确恢复成立")
        return True
    print(f"[FAIL] 槽{slot} crc={sq[1] if sq else 'N/A'} != 备份 {local_crc}")
    return False


def rebind_and_verify_gear(base: str, gear: int, n: int, local_crc: str,
                           ack_timeout: float) -> bool:
    """SAVE 1 → REVBIND <gear>(写仓库) → REVWH LOAD 终账。"""
    if not save_and_verify_slot(base, 1, n, local_crc, ack_timeout):
        return False
    ok, cr = send_retry_busy(base, f"TRACK REVBIND {gear}", "TRACK_REVBIND",
                             timeout_s=ack_timeout, match={"gear": gear})
    if not ok:
        print("[ERR] TRACK REVBIND 被拒(档位越界?槽1 语义没过?)")
        return False
    d = cr.get("data") or {}
    bind_crc = hexval(d.get("crc") or "")
    if bind_crc != local_crc:
        print(f"[FAIL] REVBIND crc={bind_crc} != 备份 {local_crc}")
        return False
    print(f"[OK] REVBIND: 档{d.get('gear')} {d.get('pts')}点 crc={bind_crc} 绑定+仓库导出")
    # 终账:仓库真躺着这条(REVWH LOAD 会再顶一次 RAM,内容相同无害)
    ok, cr = send_retry_busy(base, f"TRACK REVWH LOAD {gear}", "TRACK_REVWH_LOAD",
                             timeout_s=ack_timeout, match={"gear": gear})
    if not ok:
        print("[FAIL] 仓库终账读不出(导出没落?)")
        return False
    wh_crc = hexval((cr.get("data") or {}).get("crc") or "")
    if wh_crc != local_crc:
        print(f"[FAIL] 仓库档{gear} crc={wh_crc} != 备份 {local_crc}")
        return False
    print(f"[PASS] 仓库档{gear} crc={wh_crc} == 备份 —— 倒车段字节精确恢复+绑定成立")
    return True


def restore(args) -> int:
    base = f"http://{args.http_host}:{args.http_port}"
    try:
        hexes, want_crc, direction = load_backup(args.input_json, args.from_fields)
        local_crc = crc32_hex(binascii.unhexlify("".join(hexes)))
    except (ValueError, KeyError, json.JSONDecodeError, binascii.Error) as e:
        print(f"[ERR] 读备份失败: {e}")
        return 2
    n = len(hexes)
    if want_crc and local_crc != want_crc:
        print(f"[ERR] 备份文件自检失败: 点区 CRC {local_crc} != meta.crc {want_crc}(文件被改过?)")
        return 2
    print(f"[OK] 备份自检通过: {n} 点, crc={local_crc}, 方向={direction}")

    # 目的地与方向的匹配在碰车之前就断掉,别把错误留给固件 ACK
    if args.rebind_gear is not None and direction != "reverse":
        print("[ERR] --rebind-gear 只用于倒车线,这份备份是前进线")
        return 2
    if args.save_slot is not None:
        if direction == "reverse" and args.save_slot != 1:
            print("[ERR] 倒车线只能 --save-slot 1(或 --rebind-gear 走档位仓库)")
            return 2
        if direction == "forward" and args.save_slot == 1:
            print("[ERR] 前进线不能存倒车槽1")
            return 2

    if not bridge_ready(base):
        return 3
    session_prologue(base)
    mission_idle(base)

    unlocked = False
    try:
        if args.unlock:
            unlocked = set_track_lock(base, False)
        if push_points(base, hexes, direction, args.ack_timeout) is None:
            return 5
        ok = True
        if args.rebind_gear is not None:
            ok = rebind_and_verify_gear(base, args.rebind_gear, n, local_crc,
                                        args.ack_timeout)
        elif args.save_slot is not None:
            ok = save_and_verify_slot(base, args.save_slot, n, local_crc,
                                      args.ack_timeout)
            if ok and args.save_slot == 1:
                print("[NOTE] 槽1 直存不动绑定;要挂档跑 FULL RUN 用 --rebind-gear")
        if not ok:
            return 6
    finally:
        if unlocked:
            set_track_lock(base, True)
    print("[提醒] ①锁(没用 --unlock 的话记得手动回锁) ②重订三流+SUB SET "
          "③发车前 TRACK LOAD 0 ④第一次复现低速有人盯")
    return 0


# ============================================================================
# verify(只读对账;--gear 例外:REVWH LOAD 会顶 RAM,内容即备份线,无损失)
# ============================================================================

def verify(args) -> int:
    base = f"http://{args.http_host}:{args.http_port}"
    try:
        hexes, want_crc, direction = load_backup(args.input_json, allow_fields=False)
        local_crc = crc32_hex(binascii.unhexlify("".join(hexes)))
    except (ValueError, KeyError, json.JSONDecodeError, binascii.Error) as e:
        print(f"[ERR] 读备份失败: {e}")
        return 2
    print(f"[OK] 备份 {len(hexes)} 点 crc={local_crc} 方向={direction}"
          + (f"(meta.crc={want_crc})" if want_crc else ""))
    if not bridge_ready(base):
        return 3
    if args.gear is not None:
        print("[NOTE] 仓库对账走 REVWH LOAD,会把该档线载入 RAM(顶掉现 RAM 线)")
        ok, cr = send_retry_busy(base, f"TRACK REVWH LOAD {args.gear}",
                                 "TRACK_REVWH_LOAD", match={"gear": args.gear})
        if not ok:
            return 4
        d = cr.get("data") or {}
        got = hexval(d.get("crc") or "")
        where = f"仓库档{args.gear}"
        pts = d.get("pts")
    else:
        cmd = "TRACK CRC RAM" if args.slot is None else f"TRACK CRC {args.slot}"
        match = {"slot": -1 if args.slot is None else args.slot}
        ok, cr = send_and_wait(base, cmd, "TRACK_CRC", match=match)
        if not ok:
            return 4
        d = cr.get("data") or {}
        got = hexval(d.get("crc") or "")
        where = "RAM" if args.slot is None else f"槽{args.slot}"
        pts = d.get("pts")
    if got == local_crc:
        print(f"[PASS] 车上{where}({pts}点) crc={got} == 备份,同一条线")
    else:
        print(f"[DIFF] 车上{where}({pts}点) crc={got} != 备份 {local_crc},不是同一条线")
    return 0


# ============================================================================
# backup-all / restore-all(manifest 驱动的一把梭)
# ============================================================================

def parse_int_list(csv: str, valid: tuple, what: str) -> list[int]:
    out = []
    for tok in csv.split(","):
        tok = tok.strip()
        if not tok:
            continue
        v = int(tok)
        if v not in valid:
            raise ValueError(f"{what} {v} 越界(合法: {valid})")
        out.append(v)
    return out


def backup_all(args) -> int:
    base = f"http://{args.http_host}:{args.http_port}"
    try:
        slots = parse_int_list(args.slots, ALL_SLOTS, "槽")
        gears = parse_int_list(args.gears, ALL_GEARS, "档")
    except ValueError as e:
        print(f"[ERR] {e}")
        return 2
    if not bridge_ready(base):
        return 3
    session_prologue(base)
    mission_idle(base)

    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir)
    entries, skipped = [], []
    for slot in slots:
        print(f"\n===== 槽 {slot} =====")
        e = backup_one(base, out_dir, ts, f"{args.label}_slot{slot}",
                       slot, None, args.ack_timeout)
        (entries if e else skipped).append(e or f"slot{slot}")
    for gear in gears:
        print(f"\n===== 仓库档 {gear} =====")
        e = backup_one(base, out_dir, ts, f"{args.label}_gear{gear}",
                       None, gear, args.ack_timeout)
        (entries if e else skipped).append(e or f"gear{gear}")

    manifest = {
        "meta": {"source": "track_raw_backup", "mode": "backup-all",
                 "captured_at": ts, "label": args.label},
        "entries": entries,
    }
    mpath = out_dir / f"raw_manifest_{ts}_{args.label}.json"
    mpath.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"\n[SAVED] manifest: {mpath}")
    print(f"[SUMMARY] 备份 {len(entries)} 条;跳过 {len(skipped)} 项: {skipped or '无'}")
    print("[提醒] ①RAM 现在是最后备份那条线,发车前 TRACK LOAD 0 ②重订三流+SUB SET"
          " ③把 recordings/ 目录再拷一份到第二台电脑/网盘才算冷备")
    return 0 if entries else 5


def restore_all(args) -> int:
    base = f"http://{args.http_host}:{args.http_port}"
    try:
        manifest = json.loads(Path(args.manifest_json).read_text(encoding="utf-8"))
        entries = manifest["entries"]
        mdir = Path(args.manifest_json).resolve().parent
    except (OSError, KeyError, json.JSONDecodeError) as e:
        print(f"[ERR] 读 manifest 失败: {e}")
        return 2
    fwd = [e for e in entries if e["direction"] == "forward"]
    gears = sorted((e for e in entries if e.get("gear")), key=lambda e: e["gear"])
    slot1 = [e for e in entries if e.get("slot") == 1]
    selected_gear = (manifest.get("meta") or {}).get("selected_gear")

    if not bridge_ready(base):
        return 3
    session_prologue(base)
    mission_idle(base)

    results = []
    unlocked = False
    try:
        if args.unlock:
            unlocked = set_track_lock(base, False)

        # ① 前进类槽(0,2..11):槽0 必须最先——倒车段指纹钉的是它
        for e in sorted(fwd, key=lambda x: x["slot"]):
            print(f"\n===== 恢复 槽{e['slot']} ({e['file']}) =====")
            ok = restore_entry_to_slot(base, mdir / e["file"], e, args.ack_timeout)
            results.append((f"槽{e['slot']}", ok))

        # ② 倒车档仓库:逐档 PTX→END REV→SAVE 1→REVBIND <gear>→仓库终账
        for e in gears:
            g = e["gear"]
            print(f"\n===== 恢复 仓库档{g} ({e['file']}) =====")
            ok = restore_entry_to_gear(base, mdir / e["file"], e, g, args.ack_timeout)
            results.append((f"仓库档{g}", ok))

        # ③ 槽1 单独条目:有逐档仓库时无需重复恢复。旧 manifest 若记录了
        # selected_gear，则将其仅作为显式 REVBIND 的目标档位使用。
        for e in slot1:
            if gears:
                print(f"\n[SKIP] 槽1 条目 {e['file']}:逐档仓库已恢复")
                results.append(("槽1(跳过,逐档仓库已恢复)", True))
            elif selected_gear:
                print(f"\n===== 恢复 槽1并绑定 S1 g{selected_gear} ({e['file']}) =====")
                ok = restore_entry_to_gear(base, mdir / e["file"], e,
                                           selected_gear, args.ack_timeout)
                results.append((f"槽1→仓库档{selected_gear}", ok))
            else:
                print(f"\n===== 恢复 槽1 ({e['file']}) =====")
                ok = restore_entry_to_slot(base, mdir / e["file"], e, args.ack_timeout)
                results.append(("槽1(未记录源板选档,无法绑定)", ok))

    finally:
        if unlocked:
            set_track_lock(base, True)

    print("\n========== restore-all 汇总 ==========")
    fails = 0
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        fails += 0 if ok else 1
    print(f"共 {len(results)} 项, 失败 {fails}")
    print("[提醒] ①重订三流+SUB SET ②发车前 TRACK LOAD 0 ③第一次复现低速有人盯"
          " ④换板场景先确认七档参数/AutoCal/Gyro/ABSCAL 已重建")
    return 0 if 0 == fails else 6


def restore_entry_to_slot(base: str, path: Path, entry: dict, ack_timeout: float) -> bool:
    try:
        hexes, want_crc, direction = load_backup(str(path), allow_fields=False)
        local_crc = crc32_hex(binascii.unhexlify("".join(hexes)))
    except (ValueError, KeyError, OSError, json.JSONDecodeError, binascii.Error) as e:
        print(f"[ERR] 读备份失败: {e}")
        return False
    if (want_crc and local_crc != want_crc) or local_crc != entry["crc"]:
        print(f"[ERR] 备份自检失败: 点区 {local_crc} vs meta {want_crc} vs manifest {entry['crc']}")
        return False
    if push_points(base, hexes, direction, ack_timeout) is None:
        return False
    return save_and_verify_slot(base, entry["slot"], len(hexes), local_crc, ack_timeout)


def restore_entry_to_gear(base: str, path: Path, entry: dict, gear: int,
                          ack_timeout: float) -> bool:
    try:
        hexes, want_crc, direction = load_backup(str(path), allow_fields=False)
        local_crc = crc32_hex(binascii.unhexlify("".join(hexes)))
    except (ValueError, KeyError, OSError, json.JSONDecodeError, binascii.Error) as e:
        print(f"[ERR] 读备份失败: {e}")
        return False
    if direction != "reverse":
        print("[ERR] 档位条目不是倒车线?manifest 异常")
        return False
    if (want_crc and local_crc != want_crc) or local_crc != entry["crc"]:
        print(f"[ERR] 备份自检失败: 点区 {local_crc} vs meta {want_crc} vs manifest {entry['crc']}")
        return False
    if push_points(base, hexes, direction, ack_timeout) is None:
        return False
    return rebind_and_verify_gear(base, gear, len(hexes), local_crc, ack_timeout)


# ============================================================================
# main
# ============================================================================

def main(argv) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--http-host", default="127.0.0.1")
    ap.add_argument("--http-port", type=int, default=9898)
    sub = ap.add_subparsers(dest="mode", required=True)

    b = sub.add_parser("backup", help="RAM/flash 槽/仓库档 → raw_backup json(字节精确)")
    b.add_argument("--label", default="run")
    b.add_argument("--slot", type=int, default=None, choices=list(ALL_SLOTS),
                   help="先 TRACK LOAD 该槽再备份(要 mission IDLE)")
    b.add_argument("--gear", type=int, default=None, choices=list(ALL_GEARS),
                   help="备份逐档倒车仓库该档(TRACK REVWH LOAD,不动槽1/绑定/选档)")
    b.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    b.add_argument("--ack-timeout", type=float, default=5.0)

    r = sub.add_parser("restore", help="raw_backup json → 车(PTX 字节精确;方向自动识别)")
    r.add_argument("input_json")
    r.add_argument("--save-slot", type=int, default=None, choices=list(ALL_SLOTS),
                   help="END 后 TRACK SAVE 进 flash 槽并 CRC 对账"
                        "(前进线=0/2..11;倒车线=1,槽1 直存不动绑定)")
    r.add_argument("--rebind-gear", type=int, default=None, choices=list(ALL_GEARS),
                   help="倒车线完整恢复:灌线→SAVE 1→REVBIND <gear>→仓库终账")
    r.add_argument("--unlock", action="store_true",
                   help="脚本代发 TRACK_LOCK 0,收尾必回锁")
    r.add_argument("--ack-timeout", type=float, default=5.0)
    r.add_argument("--from-fields", action="store_true",
                   help="允许无 raw_hex 的旧 learn_dump(非按位,恢复后倒车段 STALE)")

    v = sub.add_parser("verify", help="对账:备份 crc vs 车上 RAM/槽/仓库档")
    v.add_argument("input_json")
    v.add_argument("--slot", type=int, default=None, choices=list(ALL_SLOTS),
                   help="对账 flash 槽号(缺省对 RAM;只读)")
    v.add_argument("--gear", type=int, default=None, choices=list(ALL_GEARS),
                   help="对账仓库档(REVWH LOAD,会把该档线载入 RAM)")

    ba = sub.add_parser("backup-all", help="12 槽+7 档仓库一把梭冷备,产出 manifest")
    ba.add_argument("--label", default="race_eve")
    ba.add_argument("--slots", default=",".join(str(s) for s in ALL_SLOTS),
                    help="要备份的槽号 CSV(默认全部;空槽自动跳过)")
    ba.add_argument("--gears", default=",".join(str(g) for g in ALL_GEARS),
                    help="要备份的仓库档 CSV(默认全部;空档自动跳过)")
    ba.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ba.add_argument("--ack-timeout", type=float, default=5.0)

    ra = sub.add_parser("restore-all", help="按 manifest 一把梭恢复(槽0 最先→门洞→逐档仓库)")
    ra.add_argument("manifest_json")
    ra.add_argument("--unlock", action="store_true",
                    help="脚本代发 TRACK_LOCK 0,收尾必回锁")
    ra.add_argument("--ack-timeout", type=float, default=5.0)

    args = ap.parse_args(argv)
    if args.mode == "backup":
        return backup(args)
    if args.mode == "restore":
        return restore(args)
    if args.mode == "backup-all":
        return backup_all(args)
    if args.mode == "restore-all":
        return restore_all(args)
    return verify(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
