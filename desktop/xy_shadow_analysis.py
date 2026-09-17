"""Analyze the TC387 XY path-projection shadow controller.

The script is intentionally independent from sim_pure_pursuit.py. It mirrors
the bounded firmware projection and correction rules so a LOGF/TELSEL capture
can be checked against a separately recomputed result.

Examples:
    python xy_shadow_analysis.py --self-test
    python xy_shadow_analysis.py capture.log recordings/learn_dump.json \
        --ref-pose 12.34 -4.56 91.2
    python xy_shadow_analysis.py LOG0001.TXT track.json \
        --origin-offset 1.2 -0.3 15.0 --output-prefix reports/run_01
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


SEARCH_BACK_M = 0.50
SEARCH_FORWARD_M = 1.50
SCAN_MAX = 128
HEADING_DOT_MIN = -0.20
MAX_PROJECTION_DISTANCE_M = 0.75
MIN_SEGMENT_LENGTH_M = 0.001
DISTANCE_TIE_M = 0.02
EMA_ALPHA_100HZ = 0.09516258
XTE_GAIN = 1.20
SPEED_SOFTEN_MS = 0.50
YAW_CORRECTION_LIMIT_DEG = 12.0
REVERSE_SPEED_DEADBAND_MMS = 30
REVERSED_SPEED_NOISE_MMS = 150


@dataclass(frozen=True)
class TrackPoint:
    x_m: float
    y_m: float
    yaw_deg: float
    speed_mm_s: int = 0
    enc_dist_mm: Optional[int] = None


@dataclass
class Projection:
    segment_idx: int
    ratio: float
    projection_x_m: float
    projection_y_m: float
    distance_m: float
    xte_m: float
    cursor_gap: int
    distance_band: int
    cursor_arc_m: float
    forward_window: bool


def wrap_deg(degrees: float) -> float:
    """Mirror math_wrap_deg_180(): result is (-180, 180]."""
    if degrees > 180.0:
        if degrees <= 540.0:
            degrees -= 360.0
        else:
            degrees = math.fmod(degrees, 360.0)
            if degrees > 180.0:
                degrees -= 360.0
    elif degrees <= -180.0:
        if degrees > -540.0:
            degrees += 360.0
        else:
            degrees = math.fmod(degrees, 360.0)
            if degrees <= -180.0:
                degrees += 360.0
    return degrees


def clip(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def yaw_correction_deg(xte_filtered_m: float, center_speed_ms: float) -> float:
    correction = -math.degrees(
        math.atan2(XTE_GAIN * xte_filtered_m,
                   abs(center_speed_ms) + SPEED_SOFTEN_MS)
    )
    return clip(correction, -YAW_CORRECTION_LIMIT_DEG,
                YAW_CORRECTION_LIMIT_DEG)


def _point_value(source: Any, names: Sequence[str], default: Any = 0) -> Any:
    if isinstance(source, dict):
        for name in names:
            if name in source:
                return source[name]
        return default
    return default


def load_track(path: Path) -> tuple[list[TrackPoint], dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    meta: dict[str, Any] = {}
    if isinstance(data, dict):
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        raw_points = data.get("points")
    else:
        raw_points = data

    if not isinstance(raw_points, list) or len(raw_points) < 2:
        raise ValueError(f"{path}: track must contain at least two points")

    points: list[TrackPoint] = []
    for index, raw in enumerate(raw_points):
        if isinstance(raw, dict):
            x_m = float(_point_value(raw, ("px_m", "x_m", "x"), 0.0))
            y_m = float(_point_value(raw, ("py_m", "y_m", "y"), 0.0))
            yaw = float(_point_value(raw, ("yaw_deg", "yaw"), 0.0))
            speed = int(float(_point_value(
                raw, ("enc_spd_mm_s", "speed_mm_s", "spd"), 0)))
            distance_value = _point_value(
                raw, ("enc_dist_mm", "distance_mm", "edm"), None)
            enc_dist_mm = (int(float(distance_value))
                           if distance_value is not None else None)
        elif isinstance(raw, (list, tuple)) and len(raw) >= 3:
            x_m, y_m, yaw = float(raw[0]), float(raw[1]), float(raw[2])
            speed = int(float(raw[3])) if len(raw) >= 4 else 0
            enc_dist_mm = int(float(raw[4])) if len(raw) >= 5 else None
        else:
            raise ValueError(f"{path}: invalid point at index {index}")
        if not all(math.isfinite(value) for value in (x_m, y_m, yaw)):
            raise ValueError(f"{path}: non-finite point at index {index}")
        points.append(TrackPoint(
            x_m, y_m, wrap_deg(yaw), speed, enc_dist_mm))
    return points, meta


def transform_track(points: Sequence[TrackPoint], ref_x_m: float,
                    ref_y_m: float, ref_yaw_deg: float) -> list[TrackPoint]:
    origin = points[0]
    dyaw_deg = wrap_deg(ref_yaw_deg - origin.yaw_deg)
    dyaw_rad = math.radians(dyaw_deg)
    cos_dyaw = math.cos(dyaw_rad)
    sin_dyaw = math.sin(dyaw_rad)
    transformed: list[TrackPoint] = []
    for point in points:
        rel_x = point.x_m - origin.x_m
        rel_y = point.y_m - origin.y_m
        transformed.append(TrackPoint(
            ref_x_m + rel_x * cos_dyaw - rel_y * sin_dyaw,
            ref_y_m + rel_x * sin_dyaw + rel_y * cos_dyaw,
            wrap_deg(point.yaw_deg + dyaw_deg),
            point.speed_mm_s,
            point.enc_dist_mm,
        ))
    return transformed


def build_return_reversed(points: Sequence[TrackPoint]) -> list[TrackPoint]:
    """Mirror ins_record_build_return_reversed() for projection inputs."""
    if len(points) < 2:
        raise ValueError("--reverse-track requires at least two points")
    if any(point.speed_mm_s < -REVERSED_SPEED_NOISE_MMS for point in points):
        raise ValueError(
            "--reverse-track input contains speed below -150 mm/s; "
            "firmware would reject the return-track conversion")
    if any(point.enc_dist_mm is None for point in points):
        raise ValueError(
            "--reverse-track requires enc_dist_mm on every point to mirror "
            "the firmware monotonic-mileage gate")

    distances_mm = [int(point.enc_dist_mm) for point in points
                    if point.enc_dist_mm is not None]
    if any(current < previous for previous, current in
           zip(distances_mm, distances_mm[1:])):
        raise ValueError(
            "--reverse-track input mileage is not monotonic; firmware would "
            "reject the return-track conversion")
    total_distance_mm = distances_mm[-1]

    reversed_points: list[TrackPoint] = []
    for point in reversed(points):
        speed_mm_s = point.speed_mm_s
        if -REVERSED_SPEED_NOISE_MMS <= speed_mm_s < 0:
            speed_mm_s = 0
        reversed_points.append(TrackPoint(
            point.x_m,
            point.y_m,
            wrap_deg(point.yaw_deg + 180.0),
            speed_mm_s,
            total_distance_mm - int(point.enc_dist_mm),
        ))
    return reversed_points


def _cursor_gap(segment_idx: int, cursor_idx: int) -> int:
    if segment_idx >= cursor_idx:
        return segment_idx - cursor_idx
    return cursor_idx - (segment_idx + 1)


def _candidate_is_better(candidate: Projection,
                         best: Optional[Projection]) -> bool:
    if best is None:
        return True
    if candidate.distance_band != best.distance_band:
        return candidate.distance_band < best.distance_band
    if candidate.cursor_gap != best.cursor_gap:
        return candidate.cursor_gap < best.cursor_gap
    if candidate.forward_window != best.forward_window:
        return candidate.forward_window
    if candidate.cursor_arc_m != best.cursor_arc_m:
        return candidate.cursor_arc_m < best.cursor_arc_m
    return candidate.segment_idx < best.segment_idx


def _evaluate_segment(points: Sequence[TrackPoint], segment_idx: int,
                      cursor_idx: int, forward_window: bool,
                      near_arc_m: float, window_limit_m: float,
                      cur_x_m: float, cur_y_m: float,
                      heading_x: float, heading_y: float,
                      best: Optional[Projection]
                      ) -> tuple[str, float, Optional[Projection]]:
    p0 = points[segment_idx]
    p1 = points[segment_idx + 1]
    if (p0.speed_mm_s < -REVERSE_SPEED_DEADBAND_MMS or
            p1.speed_mm_s < -REVERSE_SPEED_DEADBAND_MMS):
        return "reverse_boundary", 0.0, best

    sx = p1.x_m - p0.x_m
    sy = p1.y_m - p0.y_m
    segment_length_sq = sx * sx + sy * sy
    if (not math.isfinite(segment_length_sq) or
            segment_length_sq <= MIN_SEGMENT_LENGTH_M * MIN_SEGMENT_LENGTH_M):
        return "ok", 0.0, best

    segment_length = math.sqrt(segment_length_sq)

    heading_dot = (sx * heading_x + sy * heading_y) / segment_length
    if not math.isfinite(heading_dot) or heading_dot < HEADING_DOT_MIN:
        return "ok", segment_length, best

    rx = cur_x_m - p0.x_m
    ry = cur_y_m - p0.y_m
    ratio_numerator = rx * sx + ry * sy
    if not all(math.isfinite(value) for value in (rx, ry, ratio_numerator)):
        return "ok", segment_length, best
    ratio = clip(ratio_numerator / segment_length_sq, 0.0, 1.0)
    cursor_arc_m = near_arc_m + (
        ratio if forward_window else (1.0 - ratio)
    ) * segment_length
    if not math.isfinite(cursor_arc_m) or cursor_arc_m > window_limit_m:
        return "ok", segment_length, best

    projection_x_m = p0.x_m + ratio * sx
    projection_y_m = p0.y_m + ratio * sy
    if not all(math.isfinite(value) for value in
               (projection_x_m, projection_y_m)):
        return "ok", segment_length, best
    distance_m = math.hypot(cur_x_m - projection_x_m,
                            cur_y_m - projection_y_m)
    if (not math.isfinite(distance_m) or
            distance_m > MAX_PROJECTION_DISTANCE_M):
        return "ok", segment_length, best

    xte_m = (sx * ry - sy * rx) / segment_length
    if not math.isfinite(xte_m):
        return "ok", segment_length, best

    candidate = Projection(
        segment_idx=segment_idx,
        ratio=ratio,
        projection_x_m=projection_x_m,
        projection_y_m=projection_y_m,
        distance_m=distance_m,
        xte_m=xte_m,
        cursor_gap=_cursor_gap(segment_idx, cursor_idx),
        distance_band=int(distance_m / DISTANCE_TIE_M),
        cursor_arc_m=cursor_arc_m,
        forward_window=forward_window,
    )
    if _candidate_is_better(candidate, best):
        best = candidate
    return "ok", segment_length, best


def project_xy(points: Sequence[TrackPoint], cursor_idx: int,
               cur_x_m: float, cur_y_m: float,
               cur_yaw_deg: float) -> Optional[Projection]:
    """Mirror ctrl_xy_find_projection() without touching any cursor state."""
    total = len(points)
    if (total < 2 or cursor_idx < 0 or cursor_idx >= total or
            not all(math.isfinite(value) for value in
                    (cur_x_m, cur_y_m, cur_yaw_deg))):
        return None

    yaw_rad = math.radians(cur_yaw_deg)
    heading_x = math.cos(yaw_rad)
    heading_y = math.sin(yaw_rad)
    forward_idx = cursor_idx
    backward_idx = cursor_idx - 1
    forward_arc_m = 0.0
    backward_arc_m = 0.0
    forward_open = forward_idx + 1 < total
    backward_open = backward_idx >= 0
    equal_next_forward = True
    scanned = 0
    best: Optional[Projection] = None

    while scanned < SCAN_MAX and (forward_open or backward_open):
        if not backward_open:
            use_forward = True
        elif not forward_open:
            use_forward = False
        else:
            forward_fraction = forward_arc_m / SEARCH_FORWARD_M
            backward_fraction = backward_arc_m / SEARCH_BACK_M
            if forward_fraction < backward_fraction:
                use_forward = True
            elif forward_fraction > backward_fraction:
                use_forward = False
            else:
                use_forward = equal_next_forward
                equal_next_forward = not equal_next_forward

        segment_idx = forward_idx if use_forward else backward_idx
        status, segment_length, best = _evaluate_segment(
            points,
            segment_idx,
            cursor_idx,
            use_forward,
            forward_arc_m if use_forward else backward_arc_m,
            SEARCH_FORWARD_M if use_forward else SEARCH_BACK_M,
            cur_x_m,
            cur_y_m,
            heading_x,
            heading_y,
            best,
        )
        scanned += 1

        if use_forward:
            if status == "reverse_boundary":
                forward_open = False
            else:
                forward_arc_m += segment_length
                forward_idx += 1
                forward_open = (forward_arc_m < SEARCH_FORWARD_M and
                                forward_idx + 1 < total)
        else:
            if status == "reverse_boundary":
                backward_open = False
            else:
                backward_arc_m += segment_length
                backward_idx -= 1
                backward_open = (backward_arc_m < SEARCH_BACK_M and
                                 backward_idx >= 0)
    return best


def _to_number(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return value
    text = value.strip()
    try:
        if any(ch in text for ch in ".eE"):
            return float(text)
        return int(text, 10)
    except ValueError:
        return text


def _parse_kv_line(line: str) -> Optional[dict[str, Any]]:
    line = line.strip()
    if not line:
        return None
    marker_positions = [position for position in
                        (line.find("TELSEL,"), line.find("LOGF,"))
                        if position >= 0]
    if marker_positions:
        line = line[min(marker_positions):]
    parts = line.split(",")
    packet = parts[0].strip().upper()
    if packet not in {"TELSEL", "LOGF"}:
        return None
    frame: dict[str, Any] = {"_packet": packet}
    for token in parts[1:]:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        frame[key.strip()] = _to_number(value)
    return frame


def _extract_json_frames(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        result: list[dict[str, Any]] = []
        for item in data:
            result.extend(_extract_json_frames(item))
        return result
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("frames"), list):
        return _extract_json_frames(data["frames"])
    if isinstance(data.get("parsed"), dict):
        return [dict(data["parsed"])]
    if isinstance(data.get("p"), dict):
        return [dict(data["p"])]
    if isinstance(data.get("packet"), dict):
        return [dict(data["packet"])]
    return [dict(data)]


def load_telemetry(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    frames: list[dict[str, Any]] = []
    if path.suffix.lower() == ".json":
        frames = _extract_json_frames(json.loads(text))
    else:
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("{"):
                try:
                    frames.extend(_extract_json_frames(json.loads(stripped)))
                    continue
                except json.JSONDecodeError:
                    pass
            frame = _parse_kv_line(stripped)
            if frame is not None:
                frames.append(frame)

    aliases = {
        "cpx": ("cpx", "m_cpx"),
        "cpy": ("cpy", "m_cpy"),
        "cyaw": ("cyaw", "m_cyaw"),
        "pidx": ("pidx", "m_idx"),
        "rev": ("rev", "m_rev"),
        "eng": ("eng", "m_eng"),
        "str": ("str", "m_str"),
        "spd": ("spd", "m_spd"),
        "tgy": ("tgy", "m_tgy"),
        "larec": ("larec", "m_larec"),
    }
    normalized: list[dict[str, Any]] = []
    for raw in frames:
        frame = {key: _to_number(value) for key, value in raw.items()}
        packet = str(frame.get("_packet", "")).upper()
        if packet and packet not in {"TELSEL", "LOGF"}:
            continue
        for canonical, choices in aliases.items():
            if canonical in frame:
                continue
            for choice in choices:
                if choice in frame:
                    frame[canonical] = frame[choice]
                    break
        if "ms" in frame and (packet or "xyv" in frame):
            normalized.append(frame)
    if not normalized:
        raise ValueError(f"{path}: no TELSEL/LOGF telemetry frames found")
    _unwrap_timestamps(normalized)
    return normalized


def _unwrap_timestamps(frames: Sequence[dict[str, Any]]) -> None:
    wrap_offset = 0
    previous_raw: Optional[int] = None
    previous_unwrapped: Optional[int] = None
    for frame in frames:
        raw_ms = int(float(frame["ms"])) & 0xFFFFFFFF
        if (previous_raw is not None and raw_ms < previous_raw and
                previous_raw - raw_ms > 0x80000000):
            wrap_offset += 1 << 32
        unwrapped = raw_ms + wrap_offset
        if previous_unwrapped is not None and unwrapped < previous_unwrapped:
            unwrapped = previous_unwrapped
        frame["_time_ms"] = unwrapped
        previous_raw = raw_ms
        previous_unwrapped = unwrapped


def _float(frame: dict[str, Any], key: str,
           default: Optional[float] = None) -> Optional[float]:
    value = frame.get(key, default)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _center_speed_ms(frame: dict[str, Any]) -> tuple[float, bool]:
    left = _float(frame, "spL")
    right = _float(frame, "spR")
    if left is None or right is None:
        raise ValueError(
            "spL/spR are required; command speed is not a valid replacement "
            "for the firmware's measured center speed")
    return (left + right) * 0.0005, True


def _required_fields(frames: Sequence[dict[str, Any]]) -> list[str]:
    required = (
        "ms", "eng", "cpx", "cpy", "cyaw", "pidx", "rev",
        "spL", "spR", "xyv",
    )
    optional = (
        "str", "xyseg", "xyr", "xypx", "xypy", "xypd", "xyxr",
        "xyxf", "xyc", "xyt", "xyer", "xystr",
    )
    invalid: list[str] = []
    for key in required:
        for frame in frames:
            value = _float(frame, key)
            if value is None or not math.isfinite(value):
                invalid.append(key)
                break
    for key in optional:
        if not any(key in frame for frame in frames):
            continue
        for frame in frames:
            value = _float(frame, key)
            if value is None or not math.isfinite(value):
                invalid.append(key)
                break
    for frame in frames:
        recorded_yaw = _float(frame, "larec")
        if recorded_yaw is None:
            recorded_yaw = _float(frame, "tgy")
        if recorded_yaw is None or not math.isfinite(recorded_yaw):
            invalid.append("larec|tgy")
            break
    return invalid


def analyze(frames: Sequence[dict[str, Any]],
            track: Sequence[TrackPoint], args: argparse.Namespace
            ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    missing = _required_fields(frames)
    if missing:
        raise ValueError(
            "telemetry has missing/non-finite required or selected fields in "
            "one or more "
            "frames: " + ", ".join(missing))

    rows: list[dict[str, Any]] = []
    last_rev: Optional[int] = None
    guard_remaining_ms = 0.0
    filter_valid = False
    filtered_xte_m = 0.0
    shadow_steer_deg = 0.0
    speed_from_wheels_count = 0
    engaged_field_count = 0

    for index, frame in enumerate(frames):
        time_ms = int(frame["_time_ms"])
        if index == 0:
            dt_ms = 10.0
        else:
            dt_ms = max(0.0, time_ms - int(frames[index - 1]["_time_ms"]))
            if dt_ms == 0.0:
                dt_ms = 10.0

        rev = int(round(_float(frame, "rev", 0.0) or 0.0))
        if last_rev is not None and rev != last_rev:
            guard_remaining_ms = float(args.dir_settle_ms)
        direction_guard = guard_remaining_ms > 0.0
        guard_remaining_ms = max(0.0, guard_remaining_ms - dt_ms)
        last_rev = rev

        cursor_value = _float(frame, "pidx")
        cursor_idx = int(round(cursor_value)) if cursor_value is not None else -1
        cur_x_m = _float(frame, "cpx", 0.0) or 0.0
        cur_y_m = _float(frame, "cpy", 0.0) or 0.0
        cur_yaw_deg = _float(frame, "cyaw", 0.0) or 0.0
        center_speed_ms, from_wheels = _center_speed_ms(frame)
        speed_from_wheels_count += int(from_wheels)
        firmware_valid = bool(int(round(_float(frame, "xyv", 0.0) or 0.0)))
        engaged_value = _float(frame, "eng")
        engaged_field_count += int(engaged_value is not None)
        playback_active = engaged_value is None or int(round(engaged_value)) != 0

        projection: Optional[Projection] = None
        if playback_active and rev == 0 and not direction_guard:
            projection = project_xy(track, cursor_idx, cur_x_m, cur_y_m,
                                    cur_yaw_deg)
        expected_valid = projection is not None

        pc_filtered: Optional[float] = None
        pc_correction: Optional[float] = None
        pc_target: Optional[float] = None
        pc_yaw_error: Optional[float] = None
        pc_shadow_steer: Optional[float] = None
        if projection is None:
            filter_valid = False
            filtered_xte_m = 0.0
            shadow_steer_deg = 0.0
        else:
            ticks = max(1, int(round(dt_ms / 10.0)))
            if not filter_valid:
                filtered_xte_m = projection.xte_m
                filter_valid = True
            else:
                effective_alpha = 1.0 - (1.0 - EMA_ALPHA_100HZ) ** ticks
                filtered_xte_m += effective_alpha * (
                    projection.xte_m - filtered_xte_m)
            pc_filtered = filtered_xte_m
            pc_correction = yaw_correction_deg(filtered_xte_m,
                                               center_speed_ms)
            recorded_yaw = _float(frame, "larec")
            if recorded_yaw is None:
                recorded_yaw = _float(frame, "tgy")
            if recorded_yaw is not None:
                pc_target = wrap_deg(recorded_yaw + pc_correction)
                pc_yaw_error = wrap_deg(pc_target - cur_yaw_deg)
                demand = clip(args.steer_kp * pc_yaw_error,
                              -args.steer_limit_deg,
                              args.steer_limit_deg)
                max_step = args.steer_slew_deg * ticks
                shadow_steer_deg += clip(demand - shadow_steer_deg,
                                         -max_step, max_step)
                pc_shadow_steer = shadow_steer_deg

        row: dict[str, Any] = {
            "time_ms": time_ms,
            "dt_ms": dt_ms,
            "rev": rev,
            "playback_active": playback_active,
            "direction_guard": direction_guard,
            "moving_forward": (playback_active and rev == 0 and
                               abs(center_speed_ms) > 0.05),
            "acceptance_eligible": (playback_active and rev == 0 and
                                    not direction_guard and
                                    abs(center_speed_ms) > 0.05 and
                                    0 < cursor_idx < (len(track) - 1)),
            "center_speed_ms": center_speed_ms,
            "firmware_valid": firmware_valid,
            "expected_valid": expected_valid,
            "pc_xte_filtered_m": pc_filtered,
            "pc_yaw_correction_deg": pc_correction,
            "pc_target_yaw_deg": pc_target,
            "pc_yaw_error_deg": pc_yaw_error,
            "pc_shadow_steer_deg": pc_shadow_steer,
            "projection": asdict(projection) if projection is not None else None,
            "frame": frame,
        }
        rows.append(row)

    metrics = build_metrics(rows)
    median_period_ms = metrics["timing"]["median_period_ms"]
    max_period_ms = metrics["timing"]["max_period_ms"]
    strict_100hz = (median_period_ms is not None and max_period_ms is not None and
                    9.0 <= median_period_ms <= 11.0 and max_period_ms <= 15.0)
    warnings: list[str] = []
    if engaged_field_count != len(frames):
        warnings.append(
            "eng is missing from some frames; playback validity scope is approximate")
    if not strict_100hz:
        warnings.append(
            "capture is not continuous 10 ms data; EMA/slew recomputation is approximate")
    metrics["input"] = {
        "frames": len(frames),
        "track_points": len(track),
        "wheel_speed_frame_fraction": speed_from_wheels_count / len(frames),
        "engaged_field_fraction": engaged_field_count / len(frames),
        "dir_settle_ms": args.dir_settle_ms,
        "steer_kp": args.steer_kp,
        "steer_limit_deg": args.steer_limit_deg,
        "steer_slew_deg_per_10ms": args.steer_slew_deg,
        "reverse_track_applied": bool(getattr(args, "reverse_track", False)),
    }
    metrics["constants"] = {
        "search_back_m": SEARCH_BACK_M,
        "search_forward_m": SEARCH_FORWARD_M,
        "scan_max_segments": SCAN_MAX,
        "heading_dot_min": HEADING_DOT_MIN,
        "max_projection_distance_m": MAX_PROJECTION_DISTANCE_M,
        "min_segment_length_m": MIN_SEGMENT_LENGTH_M,
        "distance_tie_m": DISTANCE_TIE_M,
        "ema_alpha_100hz": EMA_ALPHA_100HZ,
        "xte_gain": XTE_GAIN,
        "speed_soften_ms": SPEED_SOFTEN_MS,
        "yaw_correction_limit_deg": YAW_CORRECTION_LIMIT_DEG,
    }
    metrics["recompute"] = {
        "strict_100hz_filter_and_slew": strict_100hz,
        "warnings": warnings,
    }
    return rows, metrics


def _longest_invalid_ms(rows: Sequence[dict[str, Any]]) -> float:
    longest = 0.0
    current = 0.0
    for row in rows:
        if row["moving_forward"] and not row["firmware_valid"]:
            current += float(row["dt_ms"])
            longest = max(longest, current)
        else:
            current = 0.0
    return longest


def _longest_eligible_invalid_ms(rows: Sequence[dict[str, Any]]) -> float:
    longest = 0.0
    current = 0.0
    for row in rows:
        if row["acceptance_eligible"] and not row["firmware_valid"]:
            current += float(row["dt_ms"])
            longest = max(longest, current)
        else:
            current = 0.0
    return longest


def build_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    forward = [row for row in rows
               if row["playback_active"] and row["rev"] == 0]
    moving = [row for row in rows if row["moving_forward"]]
    eligible = [row for row in rows if row["acceptance_eligible"]]
    valid = [row for row in rows if row["firmware_valid"]]
    agreement = [row for row in rows
                 if row["firmware_valid"] == row["expected_valid"]]

    projection_distances = [
        _float(row["frame"], "xypd") for row in valid
        if _float(row["frame"], "xypd") is not None
    ]
    ratio_violations = 0
    nonfinite_values = 0
    sign_total = 0
    sign_ok = 0
    segment_total = 0
    segment_match = 0
    for row in rows:
        frame = row["frame"]
        for key in ("cpx", "cpy", "cyaw", "xyr", "xypx", "xypy",
                    "xypd", "xyxr", "xyxf", "xyc", "xyt", "xyer",
                    "xystr"):
            value = _float(frame, key)
            if value is not None and not math.isfinite(value):
                nonfinite_values += 1
        if row["firmware_valid"]:
            ratio = _float(frame, "xyr")
            if ratio is not None and not (-1e-6 <= ratio <= 1.0 + 1e-6):
                ratio_violations += 1
            xte = _float(frame, "xyxf")
            correction = _float(frame, "xyc")
            if (xte is not None and correction is not None and
                    abs(xte) > 5e-4):
                sign_total += 1
                if xte * correction < 0.0:
                    sign_ok += 1
            projection = row.get("projection")
            firmware_segment = _float(frame, "xyseg")
            if projection is not None and firmware_segment is not None:
                segment_total += 1
                if int(round(firmware_segment)) == projection["segment_idx"]:
                    segment_match += 1

    intervals = [float(row["dt_ms"]) for row in rows[1:]
                 if float(row["dt_ms"]) > 0.0]
    return {
        "validity": {
            "forward_rate": (sum(row["firmware_valid"] for row in forward) /
                             len(forward)) if forward else None,
            "moving_forward_rate": (sum(row["firmware_valid"] for row in moving) /
                                    len(moving)) if moving else None,
            "acceptance_rate": (sum(row["firmware_valid"] for row in eligible) /
                                len(eligible)) if eligible else None,
            "expected_agreement_rate": len(agreement) / len(rows) if rows else None,
            "longest_moving_forward_invalid_ms": _longest_invalid_ms(rows),
            "longest_acceptance_invalid_ms": _longest_eligible_invalid_ms(rows),
        },
        "projection": {
            "max_firmware_distance_m": max(projection_distances) if projection_distances else None,
            "ratio_violation_count": ratio_violations,
            "segment_match_rate": segment_match / segment_total if segment_total else None,
        },
        "sign": {
            "samples": sign_total,
            "opposite_rate": sign_ok / sign_total if sign_total else None,
        },
        "finite": {
            "nonfinite_value_count": nonfinite_values,
        },
        "timing": {
            "median_period_ms": statistics.median(intervals) if intervals else None,
            "max_period_ms": max(intervals) if intervals else None,
        },
        "errors": {
            "projection_x_m": _error_stats(rows, "xypx", "projection.projection_x_m"),
            "projection_y_m": _error_stats(rows, "xypy", "projection.projection_y_m"),
            "projection_distance_m": _error_stats(rows, "xypd", "projection.distance_m"),
            "segment_ratio": _error_stats(rows, "xyr", "projection.ratio"),
            "xte_raw_m": _error_stats(rows, "xyxr", "projection.xte_m"),
            "xte_filtered_m": _error_stats(rows, "xyxf", "pc_xte_filtered_m"),
            "yaw_correction_deg": _error_stats(rows, "xyc", "pc_yaw_correction_deg"),
            "target_yaw_deg": _error_stats(rows, "xyt", "pc_target_yaw_deg", True),
            "yaw_error_deg": _error_stats(rows, "xyer", "pc_yaw_error_deg", True),
            "shadow_steer_deg": _error_stats(rows, "xystr", "pc_shadow_steer_deg"),
        },
    }


def _row_value(row: dict[str, Any], key: str) -> Optional[float]:
    if key.startswith("projection."):
        projection = row.get("projection")
        if projection is None:
            return None
        value = projection.get(key.split(".", 1)[1])
    else:
        value = row.get(key)
    return float(value) if value is not None else None


def _error_stats(rows: Sequence[dict[str, Any]], firmware_key: str,
                 pc_key: str, wrapped: bool = False) -> dict[str, Any]:
    errors: list[float] = []
    for row in rows:
        if not row["firmware_valid"]:
            continue
        pc_value = _row_value(row, pc_key)
        firmware_value = _float(row["frame"], firmware_key)
        if pc_value is None or firmware_value is None:
            continue
        error = firmware_value - pc_value
        if wrapped:
            error = wrap_deg(error)
        errors.append(abs(error))
    if not errors:
        return {"count": 0, "mean_abs": None, "max_abs": None}
    return {
        "count": len(errors),
        "mean_abs": sum(errors) / len(errors),
        "max_abs": max(errors),
    }


def write_plot(rows: Sequence[dict[str, Any]], track: Sequence[TrackPoint],
               output_path: Path) -> None:
    start_ms = rows[0]["time_ms"]
    time_s = [(row["time_ms"] - start_ms) * 0.001 for row in rows]
    frames = [row["frame"] for row in rows]

    fig, axes = plt.subplots(2, 2, figsize=(14, 10), constrained_layout=True)
    ax_xy, ax_xte, ax_yaw, ax_steer = axes.flat

    ax_xy.plot([point.x_m for point in track],
               [point.y_m for point in track], color="black", linewidth=1.5,
               label="recorded path")
    ax_xy.plot([_float(frame, "cpx", 0.0) for frame in frames],
               [_float(frame, "cpy", 0.0) for frame in frames],
               color="#1677b8", linewidth=1.1, label="vehicle")
    valid_projection_x = [_float(row["frame"], "xypx") for row in rows
                          if row["firmware_valid"]]
    valid_projection_y = [_float(row["frame"], "xypy") for row in rows
                          if row["firmware_valid"]]
    if valid_projection_x and all(v is not None for v in valid_projection_x + valid_projection_y):
        ax_xy.scatter(valid_projection_x, valid_projection_y, s=8,
                      color="#d64541", alpha=0.55, label="firmware projection")
    python_projection_x = [row["projection"]["projection_x_m"] for row in rows
                           if row["projection"] is not None]
    python_projection_y = [row["projection"]["projection_y_m"] for row in rows
                           if row["projection"] is not None]
    if python_projection_x:
        ax_xy.scatter(python_projection_x, python_projection_y, s=14,
                      facecolors="none", edgecolors="#2b8c44", alpha=0.5,
                      label="Python projection")
    ax_xy.set_aspect("equal", adjustable="datalim")
    ax_xy.set_xlabel("X (m)")
    ax_xy.set_ylabel("Y (m)")
    ax_xy.set_title("Path, vehicle and projection")
    ax_xy.grid(True, alpha=0.25)
    ax_xy.legend(loc="best")

    ax_xte.plot(time_s, [_float(frame, "xyxr") for frame in frames],
                label="firmware raw", linewidth=1.0, alpha=0.75)
    ax_xte.plot(time_s, [_float(frame, "xyxf") for frame in frames],
                label="firmware filtered", linewidth=1.4)
    ax_xte.plot(time_s, [row["pc_xte_filtered_m"] for row in rows],
                label="Python filtered", linewidth=1.0, linestyle="--")
    ax_xte.axhline(0.0, color="black", linewidth=0.7)
    ax_xte.set_xlabel("Time (s)")
    ax_xte.set_ylabel("XTE (m)")
    ax_xte.set_title("Cross-track error")
    ax_xte.grid(True, alpha=0.25)
    ax_xte.legend(loc="best")

    ax_yaw.plot(time_s, [_float(frame, "cyaw") for frame in frames],
                label="current yaw", linewidth=1.0)
    ax_yaw.plot(time_s, [_float(frame, "larec", _float(frame, "tgy")) for frame in frames],
                label="recorded lookahead yaw", linewidth=1.0)
    ax_yaw.plot(time_s, [_float(frame, "xyt") for frame in frames],
                label="shadow target yaw", linewidth=1.2)
    ax_yaw.set_xlabel("Time (s)")
    ax_yaw.set_ylabel("Yaw (deg)")
    ax_yaw.set_title("Recorded, current and shadow yaw")
    ax_yaw.grid(True, alpha=0.25)
    ax_yaw.legend(loc="best")

    ax_steer.plot(time_s, [_float(frame, "str") for frame in frames],
                  label="actual steer", linewidth=1.1)
    ax_steer.plot(time_s, [_float(frame, "xystr") for frame in frames],
                  label="firmware shadow steer", linewidth=1.2)
    ax_steer.plot(time_s, [row["pc_shadow_steer_deg"] for row in rows],
                  label="Python shadow steer", linewidth=1.0, linestyle="--")
    ax_steer.set_xlabel("Time (s)")
    ax_steer.set_ylabel("Steer (deg)")
    ax_steer.set_title("Actual versus shadow steering")
    ax_steer.grid(True, alpha=0.25)
    ax_steer.legend(loc="best")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def _format_metric(value: Any, scale: float = 1.0,
                   suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * scale:.6g}{suffix}"


def print_summary(metrics: dict[str, Any], report_path: Path,
                  plot_path: Optional[Path]) -> None:
    validity = metrics["validity"]
    projection = metrics["projection"]
    sign = metrics["sign"]
    errors = metrics["errors"]
    print("XY shadow analysis")
    print(f"  acceptance valid rate: "
          f"{_format_metric(validity['acceptance_rate'], 100.0, '%')}")
    print(f"  moving-forward valid rate: "
          f"{_format_metric(validity['moving_forward_rate'], 100.0, '%')}")
    print(f"  max projection distance: "
          f"{_format_metric(projection['max_firmware_distance_m'], 1000.0, ' mm')}")
    print(f"  longest invalid run: "
          f"{_format_metric(validity['longest_acceptance_invalid_ms'], 1.0, ' ms')}")
    print(f"  correction sign agreement: "
          f"{_format_metric(sign['opposite_rate'], 100.0, '%')}")
    print(f"  max raw XTE error: "
          f"{_format_metric(errors['xte_raw_m']['max_abs'], 1000.0, ' mm')}")
    print(f"  max filtered XTE error: "
          f"{_format_metric(errors['xte_filtered_m']['max_abs'], 1000.0, ' mm')}")
    print(f"  max yaw-correction error: "
          f"{_format_metric(errors['yaw_correction_deg']['max_abs'], 1.0, ' deg')}")
    for warning in metrics["recompute"]["warnings"]:
        print(f"  warning: {warning}")
    print(f"  report: {report_path}")
    if plot_path is not None:
        print(f"  plot:   {plot_path}")


def _assert_close(actual: float, expected: float, tolerance: float,
                  message: str) -> None:
    if abs(actual - expected) > tolerance:
        raise AssertionError(
            f"{message}: actual={actual}, expected={expected}, tol={tolerance}")


def run_self_tests() -> None:
    straight = [TrackPoint(float(i), 0.0, 0.0, 500) for i in range(4)]

    left = project_xy(straight, 1, 1.4, 0.20, 0.0)
    assert left is not None
    assert left.xte_m > 0.0
    assert yaw_correction_deg(left.xte_m, 1.0) < 0.0

    right = project_xy(straight, 1, 1.4, -0.20, 0.0)
    assert right is not None
    assert right.xte_m < 0.0
    assert yaw_correction_deg(right.xte_m, 1.0) > 0.0

    _assert_close(wrap_deg(181.0), -179.0, 1e-9, "+180 wrap")
    _assert_close(wrap_deg(-181.0), 179.0, 1e-9, "-180 wrap")
    _assert_close(wrap_deg(-180.0), 180.0, 1e-9, "exact -180 wrap")

    zero_length = [
        TrackPoint(0.0, 0.0, 0.0, 500),
        TrackPoint(0.0, 0.0, 0.0, 500),
        TrackPoint(1.0, 0.0, 0.0, 500),
    ]
    zero_projection = project_xy(zero_length, 0, 0.5, 0.1, 0.0)
    assert zero_projection is not None and zero_projection.segment_idx == 1

    one_mm_segment = [
        TrackPoint(0.0, 0.0, 0.0, 500),
        TrackPoint(0.001, 0.0, 0.0, 500),
    ]
    assert project_xy(one_mm_segment, 0, 0.0005, 0.0, 0.0) is None
    just_over_one_mm = [
        TrackPoint(0.0, 0.0, 0.0, 500),
        TrackPoint(0.001001, 0.0, 0.0, 500),
    ]
    assert project_xy(just_over_one_mm, 0, 0.0005, 0.0, 0.0) is not None

    crossing = [
        TrackPoint(-0.5, 0.0, 0.0, 500),
        TrackPoint(0.5, 0.0, 0.0, 500),
        TrackPoint(0.5, 0.1, 90.0, 500),
        TrackPoint(-0.5, 0.1, 180.0, 500),
    ]
    crossing_projection = project_xy(crossing, 1, 0.0, 0.1, 0.0)
    assert crossing_projection is not None
    assert crossing_projection.segment_idx == 0, "heading gate chose opposite branch"

    gate_y = math.sqrt(1.0 - HEADING_DOT_MIN * HEADING_DOT_MIN)
    heading_at_gate = [
        TrackPoint(0.0, 0.0, 0.0, 500),
        TrackPoint(HEADING_DOT_MIN, gate_y, 0.0, 500),
    ]
    assert project_xy(heading_at_gate, 0, 0.0, 0.0, 0.0) is not None
    below_gate_x = HEADING_DOT_MIN - 0.0001
    heading_below_gate = [
        TrackPoint(0.0, 0.0, 0.0, 500),
        TrackPoint(below_gate_x, math.sqrt(1.0 - below_gate_x ** 2),
                   0.0, 500),
    ]
    assert project_xy(heading_below_gate, 0, 0.0, 0.0, 0.0) is None

    assert project_xy(straight, 1, 1.4, 0.75, 0.0) is not None
    assert project_xy(straight, 1, 1.4, 0.80, 0.0) is None

    extreme = [
        TrackPoint(-1.0e308, 0.0, 0.0, 500),
        TrackPoint(+1.0e308, 0.0, 0.0, 500),
    ]
    assert project_xy(extreme, 0, 0.0, 0.0, 0.0) is None

    reverse = [TrackPoint(float(i), 0.0, 0.0, -500) for i in range(4)]
    assert project_xy(reverse, 1, 1.4, 0.1, 0.0) is None

    start = project_xy(straight, 0, -0.1, 0.1, 0.0)
    end = project_xy(straight, len(straight) - 1, 3.1, 0.1, 0.0)
    assert start is not None and start.ratio == 0.0
    assert end is not None and end.ratio == 1.0

    jump_path = [TrackPoint(i * 0.25, 0.0, 0.0, 500) for i in range(24)]
    before_jump = project_xy(jump_path, 3, 0.9, 0.05, 0.0)
    after_jump = project_xy(jump_path, 17, 4.4, 0.05, 0.0)
    assert before_jump is not None and after_jump is not None
    assert before_jump.segment_idx <= 4
    assert after_jump.segment_idx >= 16

    def candidate(distance_m: float, cursor_gap: int) -> Projection:
        return Projection(
            segment_idx=cursor_gap,
            ratio=0.5,
            projection_x_m=0.0,
            projection_y_m=0.0,
            distance_m=distance_m,
            xte_m=distance_m,
            cursor_gap=cursor_gap,
            distance_band=int(distance_m / DISTANCE_TIE_M),
            cursor_arc_m=float(cursor_gap),
            forward_window=True,
        )

    best: Optional[Projection] = None
    for item in (candidate(0.000, 10), candidate(0.019, 5),
                 candidate(0.038, 0)):
        if _candidate_is_better(item, best):
            best = item
    assert best is not None and best.distance_m == 0.019, (
        "fixed distance bands must not drift through a 0/19/38 mm chain")
    assert int((DISTANCE_TIE_M - 1e-9) / DISTANCE_TIE_M) == 0
    assert int(DISTANCE_TIE_M / DISTANCE_TIE_M) == 1

    zero_starvation = [
        TrackPoint(0.0, 0.0, 0.0, 500),
        TrackPoint(1.0, 0.0, 0.0, 500),
    ] + [TrackPoint(1.0, 0.0, 0.0, 500) for _ in range(SCAN_MAX + 1)]
    backward_projection = project_xy(zero_starvation, 1, 0.8, 0.1, 0.0)
    assert backward_projection is not None
    assert backward_projection.segment_idx == 0, (
        "forward zero-length segments must not starve the backward scan")

    reverse_source = [
        TrackPoint(0.0, 0.0, 0.0, -20, 0),
        TrackPoint(1.0, 0.0, 10.0, -150, 1000),
        TrackPoint(2.0, 0.0, 20.0, 0, 2000),
        TrackPoint(3.0, 0.0, 30.0, 500, 3000),
    ]
    reversed_track = build_return_reversed(reverse_source)
    assert [point.speed_mm_s for point in reversed_track] == [500, 0, 0, 0]
    assert [point.enc_dist_mm for point in reversed_track] == [0, 1000, 2000, 3000]
    _assert_close(reversed_track[0].yaw_deg, -150.0, 1e-9,
                  "reversed yaw")
    try:
        build_return_reversed([
            TrackPoint(0.0, 0.0, 0.0, -151, 0),
            TrackPoint(1.0, 0.0, 0.0, 0, 1000),
        ])
    except ValueError:
        pass
    else:
        raise AssertionError("reverse conversion must reject speed below -150 mm/s")
    try:
        build_return_reversed([
            TrackPoint(0.0, 0.0, 0.0, 0, 1000),
            TrackPoint(1.0, 0.0, 0.0, 0, 900),
        ])
    except ValueError:
        pass
    else:
        raise AssertionError("reverse conversion must reject decreasing mileage")
    try:
        build_return_reversed([
            TrackPoint(0.0, 0.0, 0.0, 0),
            TrackPoint(1.0, 0.0, 0.0, 0),
        ])
    except ValueError:
        pass
    else:
        raise AssertionError("reverse conversion must require mileage data")

    parsed = _parse_kv_line(
        "TELSEL,ms=100,pidx=0,rev=0,cpx=0.2,cpy=0.1,cyaw=0,xyv=1")
    assert parsed is not None and parsed["pidx"] == 0
    history_frame = _extract_json_frames({"parsed": parsed})
    assert len(history_frame) == 1 and history_frame[0]["_packet"] == "TELSEL"

    frames: list[dict[str, Any]] = []
    filtered = 0.0
    filter_valid = False
    shadow_steer = 0.0
    for index in range(6):
        cur_x = 0.20 + index * 0.01
        projection = project_xy(straight, 0, cur_x, 0.10, 0.0)
        assert projection is not None
        if not filter_valid:
            filtered = projection.xte_m
            filter_valid = True
        else:
            filtered += EMA_ALPHA_100HZ * (projection.xte_m - filtered)
        correction = yaw_correction_deg(filtered, 0.5)
        target = wrap_deg(correction)
        yaw_error = target
        demand = clip(-2.0 * yaw_error, -30.0, 30.0)
        shadow_steer += clip(demand - shadow_steer, -8.0, 8.0)
        frames.append({
            "ms": 100 + index * 10,
            "_time_ms": 100 + index * 10,
            "pidx": 0,
            "rev": 0,
            "eng": 1,
            "cpx": cur_x,
            "cpy": 0.10,
            "cyaw": 0.0,
            "spL": 500,
            "spR": 500,
            "larec": 0.0,
            "xyv": 1,
            "xyseg": projection.segment_idx,
            "xyr": projection.ratio,
            "xypx": projection.projection_x_m,
            "xypy": projection.projection_y_m,
            "xypd": projection.distance_m,
            "xyxr": projection.xte_m,
            "xyxf": filtered,
            "xyc": correction,
            "xyt": target,
            "xyer": yaw_error,
            "xystr": shadow_steer,
            "str": 0.0,
        })
    test_args = argparse.Namespace(
        dir_settle_ms=500.0,
        steer_kp=-2.0,
        steer_limit_deg=30.0,
        steer_slew_deg=8.0,
        reverse_track=False,
    )
    rows, metrics = analyze(frames, straight, test_args)
    assert len(rows) == len(frames)
    assert metrics["validity"]["moving_forward_rate"] == 1.0
    assert metrics["errors"]["xte_raw_m"]["max_abs"] < 1e-9

    independent_frames = [dict(frame) for frame in frames[:2]]
    independent_frames[0]["xyv"] = 0
    independent_rows, _ = analyze(independent_frames, straight, test_args)
    assert independent_rows[0]["pc_xte_filtered_m"] is not None, (
        "Python recomputation must not depend on firmware xyv")

    incomplete_frames = [dict(frame) for frame in frames[:2]]
    del incomplete_frames[1]["cpx"]
    try:
        analyze(incomplete_frames, straight, test_args)
    except ValueError:
        pass
    else:
        raise AssertionError("partial required fields must reject the capture")

    no_wheel_speed_frames = [dict(frame) for frame in frames[:2]]
    del no_wheel_speed_frames[0]["spR"]
    try:
        analyze(no_wheel_speed_frames, straight, test_args)
    except ValueError:
        pass
    else:
        raise AssertionError("missing measured wheel speed must reject the capture")

    subset_frames = [dict(frame) for frame in frames[:2]]
    optional_fields = (
        "str", "xyseg", "xyr", "xypx", "xypy", "xypd", "xyxr",
        "xyxf", "xyc", "xyt", "xyer", "xystr",
    )
    for frame in subset_frames:
        for key in optional_fields:
            frame.pop(key, None)
    subset_rows, _ = analyze(subset_frames, straight, test_args)
    assert len(subset_rows) == len(subset_frames)

    partial_optional_frames = [dict(frame) for frame in frames[:2]]
    del partial_optional_frames[1]["xyseg"]
    try:
        analyze(partial_optional_frames, straight, test_args)
    except ValueError:
        pass
    else:
        raise AssertionError("partial optional fields must reject the capture")

    print("xy_shadow_analysis self-test: 21 requirement groups + pipeline passed")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TC387 XY projection shadow telemetry analyzer")
    parser.add_argument("telemetry", nargs="?", type=Path,
                        help="TELSEL/LOGF text, JSON or JSONL capture")
    parser.add_argument("track", nargs="?", type=Path,
                        help="recorded trajectory JSON")
    pose_group = parser.add_mutually_exclusive_group()
    pose_group.add_argument(
        "--ref-pose", nargs=3, type=float, metavar=("X_M", "Y_M", "YAW_DEG"),
        help="playback reference pose; recorded point 0 is mapped here")
    pose_group.add_argument(
        "--origin-offset", nargs=3, type=float,
        metavar=("DX_M", "DY_M", "DYAW_DEG"),
        help="firmware origin offset: ref pose minus recorded point-0 pose")
    parser.add_argument("--output-prefix", type=Path,
                        help="output path without extension")
    parser.add_argument("--dir-settle-ms", type=float, default=500.0,
                        help="forward/reverse transition guard, default 500")
    parser.add_argument("--steer-kp", type=float, default=-2.0)
    parser.add_argument("--steer-limit-deg", type=float, default=30.0)
    parser.add_argument("--steer-slew-deg", type=float, default=8.0,
                        help="shadow slew limit per 10 ms tick")
    parser.add_argument(
        "--reverse-track", action="store_true",
        help=("mirror firmware return-track conversion before reference "
              "transform: reverse points, yaw +180, filter [-150,0) mm/s, "
              "reject lower speeds; requires monotonic enc_dist_mm"))
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        run_self_tests()
        return 0
    if args.telemetry is None or args.track is None:
        parser.error("telemetry and track are required unless --self-test is used")

    try:
        recorded_track, track_meta = load_track(args.track)
        if args.reverse_track:
            recorded_track = build_return_reversed(recorded_track)
        origin = recorded_track[0]
        if args.ref_pose is not None:
            ref_x_m, ref_y_m, ref_yaw_deg = args.ref_pose
            reference_source = "ref_pose"
        elif args.origin_offset is not None:
            dx_m, dy_m, dyaw_deg = args.origin_offset
            ref_x_m = origin.x_m + dx_m
            ref_y_m = origin.y_m + dy_m
            ref_yaw_deg = wrap_deg(origin.yaw_deg + dyaw_deg)
            reference_source = "origin_offset"
        else:
            ref_x_m, ref_y_m, ref_yaw_deg = (
                origin.x_m, origin.y_m, origin.yaw_deg)
            reference_source = "identity"
            print("[WARN] no reference pose supplied; using identity transform",
                  file=sys.stderr)

        track = transform_track(recorded_track, ref_x_m, ref_y_m, ref_yaw_deg)
        frames = load_telemetry(args.telemetry)
        rows, metrics = analyze(frames, track, args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.output_prefix is not None:
        output_prefix = args.output_prefix
    else:
        output_prefix = args.telemetry.with_name(
            args.telemetry.stem + "_xy_shadow")
    report_path = output_prefix.with_suffix(".json")
    plot_path = None if args.no_plot else output_prefix.with_suffix(".png")

    metrics["reference"] = {
        "source": reference_source,
        "ref_x_m": ref_x_m,
        "ref_y_m": ref_y_m,
        "ref_yaw_deg": ref_yaw_deg,
        "track_meta": track_meta,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    if plot_path is not None:
        write_plot(rows, track, plot_path)
    print_summary(metrics, report_path, plot_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
