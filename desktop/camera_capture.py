"""Camera mixed-stream parsing, recognition parity, and capture artifacts."""

from __future__ import annotations

import json
import queue
import re
import shutil
import struct
import threading
import time
import zlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


CAMERA_WIDTH = 160
CAMERA_HEIGHT = 120
CAMERA_PAYLOAD_LEN = CAMERA_WIDTH * CAMERA_HEIGHT * 2
CAMERA_MAGIC = b'CIMG'
CAMERA_HEADER_LEN = 16
CAMERA_CORE_DARK_Y_MAX = 120
CAMERA_CORE_MIN_PIXELS = 5
CAMERA_CORE_MIN_RATIO_Q8 = 13
CAMERA_STRUCTURE_MAX_AGE = 4
CAMERA_RING_CORE_MAX_Q8 = 20
CAMERA_RING_WHITE_MIN_Q8 = 41
CAMERA_RING_WHITE_MAX_Q8 = 61
CAMERA_RING_OUTER_MIN_Q8 = 92
CAMERA_RING_OUTER_MAX_Q8 = 113
CAMERA_TARGET_MAGENTA = 0
CAMERA_TARGET_MAGENTA_TRIPLE = 2
CAMERA_CYAN_Y_MIN = 70
CAMERA_CYAN_U_MIN = 145
CAMERA_CYAN_U_MAX = 220
CAMERA_CYAN_V_MAX = 90
CAMERA_CYAN_MIN_PIXELS = 2
CAMERA_YELLOW_Y_MIN = 130
CAMERA_YELLOW_U_MAX = 120
CAMERA_YELLOW_V_MIN = 130
CAMERA_YELLOW_MIN_PIXELS = 2
CAMERA_TRIPLE_STRICT_MIN_SIZE = 12


class CameraProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class CameraFrame:
    frame_seq: int
    timestamp_ms: int
    payload: bytes


class MixedCameraStreamParser:
    """Incremental parser for CRLF text plus fixed-header CIMG records."""

    def __init__(self, frame_timeout_s: float = 5.0, max_line_bytes: int = 16384):
        self.buffer = bytearray()
        self.frame_timeout_s = frame_timeout_s
        self.max_line_bytes = max_line_bytes
        self.protocol_errors = 0
        self.partial_frames = 0
        self._frame_header = None
        self._frame_started_at = None

    def feed(self, data: bytes, now: Optional[float] = None):
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError('camera stream input must be bytes')
        now = time.monotonic() if now is None else now
        self.buffer.extend(data)
        events = []

        while True:
            if self._frame_header is not None:
                frame_seq, timestamp_ms, payload_len = self._frame_header
                if len(self.buffer) < payload_len:
                    break
                payload = bytes(self.buffer[:payload_len])
                del self.buffer[:payload_len]
                self._frame_header = None
                self._frame_started_at = None
                events.append(('frame', CameraFrame(frame_seq, timestamp_ms, payload)))
                continue

            if not self.buffer:
                break
            if len(self.buffer) < len(CAMERA_MAGIC) and CAMERA_MAGIC.startswith(self.buffer):
                if self._frame_started_at is None:
                    self._frame_started_at = now
                break
            if self.buffer.startswith(CAMERA_MAGIC):
                if len(self.buffer) < CAMERA_HEADER_LEN:
                    if self._frame_started_at is None:
                        self._frame_started_at = now
                    break
                _, frame_seq, timestamp_ms, payload_len = struct.unpack(
                    '<4sIII', self.buffer[:CAMERA_HEADER_LEN])
                del self.buffer[:CAMERA_HEADER_LEN]
                if payload_len != CAMERA_PAYLOAD_LEN:
                    self.protocol_errors += 1
                    self.buffer.clear()
                    raise CameraProtocolError(
                        f'invalid CIMG payload length {payload_len}, expected {CAMERA_PAYLOAD_LEN}')
                self._frame_header = (frame_seq, timestamp_ms, payload_len)
                if self._frame_started_at is None:
                    self._frame_started_at = now
                continue

            newline = self.buffer.find(b'\n')
            if newline < 0:
                if len(self.buffer) > self.max_line_bytes:
                    self.protocol_errors += 1
                    self.buffer.clear()
                    raise CameraProtocolError('text line exceeds receive limit')
                break
            raw = bytes(self.buffer[:newline])
            del self.buffer[:newline + 1]
            text = raw.rstrip(b'\r').decode('utf-8', errors='replace').strip()
            if text:
                events.append(('line', text))
        return events

    def frame_timed_out(self, now: Optional[float] = None) -> bool:
        if self._frame_started_at is None:
            return False
        now = time.monotonic() if now is None else now
        return (now - self._frame_started_at) > self.frame_timeout_s

    def disconnect(self):
        if (self._frame_started_at is not None or self._frame_header is not None or
                (self.buffer and CAMERA_MAGIC.startswith(self.buffer[:4]))):
            self.partial_frames += 1
        self.buffer.clear()
        self._frame_header = None
        self._frame_started_at = None


def _parse_kv_line(text: str):
    fields = {}
    parts = text.split(',')
    for part in parts[1:]:
        if '=' not in part:
            continue
        key, value = part.split('=', 1)
        try:
            fields[key] = int(value, 10)
        except ValueError:
            fields[key] = value
    return fields


def decode_rgb565(payload: bytes, width: int = CAMERA_WIDTH,
                  height: int = CAMERA_HEIGHT) -> bytes:
    expected = width * height * 2
    if len(payload) != expected:
        raise CameraProtocolError(f'RGB565 payload is {len(payload)}B, expected {expected}B')
    rgb = bytearray(width * height * 3)
    out = 0
    for offset in range(0, len(payload), 2):
        pixel = payload[offset] | (payload[offset + 1] << 8)
        r = ((pixel >> 11) & 0x1F) * 255 // 31
        g = ((pixel >> 5) & 0x3F) * 255 // 63
        b = (pixel & 0x1F) * 255 // 31
        rgb[out:out + 3] = bytes((r, g, b))
        out += 3
    return bytes(rgb)


def recognize_rgb565(payload: bytes, meta: dict, previous: Optional[dict] = None,
                     width: int = CAMERA_WIDTH, height: int = CAMERA_HEIGHT):
    if len(payload) != width * height * 2:
        raise CameraProtocolError('recognition payload length mismatch')

    enabled = int(meta.get('alg', 0)) != 0 and int(meta.get('color', 0)) != 0
    track_enabled = int(meta.get('track', 0)) != 0
    u_min, u_max = int(meta.get('u_min', 0)), int(meta.get('u_max', 255))
    v_min, v_max = int(meta.get('v_min', 0)), int(meta.get('v_max', 255))
    y_min, y_max = int(meta.get('y_min', 0)), int(meta.get('y_max', 255))
    gray_enabled = int(meta.get('gray', 0)) != 0
    mask = bytearray(width * height)
    lumas = bytearray(width * height)

    if enabled:
        for index in range(width * height):
            offset = index * 2
            pixel = payload[offset] | (payload[offset + 1] << 8)
            r = ((pixel >> 11) & 0x1F) * 255 // 31
            g = ((pixel >> 5) & 0x3F) * 255 // 63
            b = (pixel & 0x1F) * 255 // 31
            yy = (77 * r + 150 * g + 29 * b) >> 8
            uu = ((-43 * r - 85 * g + 128 * b) >> 8) + 128
            vv = ((128 * r - 107 * g - 21 * b) >> 8) + 128
            yy = min(255, max(0, yy))
            uu = min(255, max(0, uu))
            vv = min(255, max(0, vv))
            lumas[index] = yy
            if u_min <= uu <= u_max and v_min <= vv <= v_max and y_min <= yy <= y_max:
                mask[index] = 255

    selected = None
    visited = bytearray(width * height)
    target = int(meta.get('target', CAMERA_TARGET_MAGENTA))
    magenta_family = target in (CAMERA_TARGET_MAGENTA,
                                CAMERA_TARGET_MAGENTA_TRIPLE)
    previous_compatible = bool(
        previous and int(previous.get('_target', target)) == target and
        bool(previous.get('_gray_enabled', gray_enabled)) == gray_enabled)
    previous_tracking = bool(
        previous_compatible and int(previous.get('state', 0)) == 1)
    previous_cx = int(previous.get('cx', 0)) if previous else 0
    previous_cy = int(previous.get('cy', 0)) if previous else 0
    for seed in range(width * height):
        if mask[seed] == 0 or visited[seed] != 0:
            continue
        pending = deque((seed,))
        visited[seed] = 1
        component = {
            'count': 0, 'sum_x': 0, 'sum_y': 0,
            'min_x': width, 'min_y': height, 'max_x': 0, 'max_y': 0,
            'last_index': seed,
        }
        while pending:
            index = pending.popleft()
            x, y = index % width, index // width
            component['count'] += 1
            component['sum_x'] += x
            component['sum_y'] += y
            component['min_x'] = min(component['min_x'], x)
            component['min_y'] = min(component['min_y'], y)
            component['max_x'] = max(component['max_x'], x)
            component['max_y'] = max(component['max_y'], y)
            component['last_index'] = max(component['last_index'], index)
            for neighbor in (index - 1, index + 1, index - width, index + width):
                if neighbor < 0 or neighbor >= width * height:
                    continue
                neighbor_x = neighbor % width
                if abs(neighbor_x - x) + abs(neighbor // width - y) != 1:
                    continue
                if mask[neighbor] != 0 and visited[neighbor] == 0:
                    visited[neighbor] = 1
                    pending.append(neighbor)
        replace = selected is None or component['count'] > selected['count']
        if (not replace and selected is not None and
                component['count'] == selected['count'] and previous_tracking):
            component_cx = component['sum_x'] // component['count']
            component_cy = component['sum_y'] // component['count']
            selected_cx = selected['sum_x'] // selected['count']
            selected_cy = selected['sum_y'] // selected['count']
            component_distance = ((component_cx - previous_cx) ** 2 +
                                  (component_cy - previous_cy) ** 2)
            selected_distance = ((selected_cx - previous_cx) ** 2 +
                                 (selected_cy - previous_cy) ** 2)
            replace = component_distance < selected_distance
        if replace:
            selected = component

    count = selected['count'] if selected else 0
    sum_x = selected['sum_x'] if selected else 0
    sum_y = selected['sum_y'] if selected else 0
    min_x = selected['min_x'] if selected else width
    min_y = selected['min_y'] if selected else height
    max_x = selected['max_x'] if selected else 0
    max_y = selected['max_y'] if selected else 0
    last_u = last_v = last_y = 0
    if selected:
        offset = selected['last_index'] * 2
        pixel = payload[offset] | (payload[offset + 1] << 8)
        r = ((pixel >> 11) & 0x1F) * 255 // 31
        g = ((pixel >> 5) & 0x3F) * 255 // 63
        b = (pixel & 0x1F) * 255 // 31
        last_y = min(255, max(0, (77 * r + 150 * g + 29 * b) >> 8))
        last_u = min(255, max(0, ((-43 * r - 85 * g + 128 * b) >> 8) + 128))
        last_v = min(255, max(0, ((128 * r - 107 * g - 21 * b) >> 8) + 128))

    candidate_valid = selected is not None
    box_w = max_x - min_x + 1 if candidate_valid else 0
    box_h = max_y - min_y + 1 if candidate_valid else 0
    cx = sum_x // count if candidate_valid else 0
    cy = sum_y // count if candidate_valid else 0
    dark_count = 0
    dark_ratio_q8 = 0
    core_valid = False
    roi_bounds = None
    cyan_count = 0
    cyan_score = 0
    cyan_valid = False
    yellow_count = 0
    yellow_valid = False
    if candidate_valid and magenta_family:
        cx = (min_x + max_x) // 2
        cy = (min_y + max_y) // 2
        cyan_margin_x = box_w // 5
        cyan_margin_y = box_h // 5
        cyan_min_x = min_x + cyan_margin_x
        cyan_max_x = max_x - cyan_margin_x
        cyan_min_y = min_y + cyan_margin_y
        cyan_max_y = max_y - cyan_margin_y
        cyan_sum_x = 0
        cyan_sum_y = 0
        if gray_enabled:
            for y in range(cyan_min_y, cyan_max_y + 1):
                for x in range(cyan_min_x, cyan_max_x + 1):
                    offset = (y * width + x) * 2
                    pixel = payload[offset] | (payload[offset + 1] << 8)
                    r = ((pixel >> 11) & 0x1F) * 255 // 31
                    g = ((pixel >> 5) & 0x3F) * 255 // 63
                    b = (pixel & 0x1F) * 255 // 31
                    yy = (77 * r + 150 * g + 29 * b) >> 8
                    uu = ((-43 * r - 85 * g + 128 * b) >> 8) + 128
                    vv = ((128 * r - 107 * g - 21 * b) >> 8) + 128
                    if (yy >= CAMERA_CYAN_Y_MIN and
                            CAMERA_CYAN_U_MIN <= uu <= CAMERA_CYAN_U_MAX and
                            vv <= CAMERA_CYAN_V_MAX):
                        cyan_count += 1
                        cyan_sum_x += x
                        cyan_sum_y += y
            cyan_roi_area = ((cyan_max_x - cyan_min_x + 1) *
                             (cyan_max_y - cyan_min_y + 1))
            cyan_score = min(100, cyan_count * 100 // max(1, cyan_roi_area))
            cyan_valid = (cyan_count >= CAMERA_CYAN_MIN_PIXELS and
                          cyan_score >= int(meta.get('score', 0)))
        if cyan_valid:
            cx = cyan_sum_x // cyan_count
            cy = cyan_sum_y // cyan_count

        if gray_enabled and target == CAMERA_TARGET_MAGENTA_TRIPLE:
            yellow_margin_x = box_w * 3 // 10
            yellow_margin_y = box_h * 3 // 10
            yellow_sum_x = 0
            yellow_sum_y = 0
            for y in range(min_y + yellow_margin_y,
                           max_y - yellow_margin_y + 1):
                for x in range(min_x + yellow_margin_x,
                               max_x - yellow_margin_x + 1):
                    offset = (y * width + x) * 2
                    pixel = payload[offset] | (payload[offset + 1] << 8)
                    r = ((pixel >> 11) & 0x1F) * 255 // 31
                    g = ((pixel >> 5) & 0x3F) * 255 // 63
                    b = (pixel & 0x1F) * 255 // 31
                    yy = (77 * r + 150 * g + 29 * b) >> 8
                    uu = ((-43 * r - 85 * g + 128 * b) >> 8) + 128
                    vv = ((128 * r - 107 * g - 21 * b) >> 8) + 128
                    if (yy >= CAMERA_YELLOW_Y_MIN and
                            uu <= CAMERA_YELLOW_U_MAX and
                            vv >= CAMERA_YELLOW_V_MIN):
                        yellow_count += 1
                        yellow_sum_x += x
                        yellow_sum_y += y
            yellow_valid = yellow_count >= CAMERA_YELLOW_MIN_PIXELS
            if yellow_valid:
                cx = yellow_sum_x // yellow_count
                cy = yellow_sum_y // yellow_count

        margin_x = box_w // 4
        margin_y = box_h // 4
        dark_sum_x = 0
        dark_sum_y = 0
        roi_min_x = min_x + margin_x
        roi_max_x = max_x - margin_x
        roi_min_y = min_y + margin_y
        roi_max_y = max_y - margin_y
        roi_bounds = (roi_min_x, roi_min_y, roi_max_x, roi_max_y)
        if (not cyan_valid and
                (target == CAMERA_TARGET_MAGENTA or not gray_enabled)):
            for y in range(roi_min_y, roi_max_y + 1):
                for x in range(roi_min_x, roi_max_x + 1):
                    index = y * width + x
                    if (lumas[index] <= CAMERA_CORE_DARK_Y_MAX and
                            mask[index] == 0):
                        dark_count += 1
                        dark_sum_x += x
                        dark_sum_y += y
        roi_area = ((roi_max_x - roi_min_x + 1) *
                    (roi_max_y - roi_min_y + 1))
        dark_ratio_q8 = dark_count * 256 // max(1, roi_area)
        core_valid = (dark_count >= CAMERA_CORE_MIN_PIXELS and
                      dark_ratio_q8 >= CAMERA_CORE_MIN_RATIO_Q8)
        if core_valid:
            cx = dark_sum_x // dark_count
            cy = dark_sum_y // dark_count

    bbox_area = box_w * box_h
    fill_score = min(100, count * 100 // bbox_area) if bbox_area else 0
    sample_means = {
        'center_black': 0,
        'white_ring': 0,
        'outer_black': 0,
    }
    contrasts = {
        'white_minus_center': 0,
        'white_minus_outer': 0,
    }
    gray_score = cyan_score if cyan_valid else 0
    structure_valid = False
    if (gray_enabled and target == CAMERA_TARGET_MAGENTA and
            not cyan_valid and core_valid and roi_bounds is not None):
        core_sum = white_sum = outer_sum = 0
        core_count = white_count = outer_count = 0
        roi_min_x, roi_min_y, roi_max_x, roi_max_y = roi_bounds
        core_limit2 = CAMERA_RING_CORE_MAX_Q8 ** 2
        white_min2 = CAMERA_RING_WHITE_MIN_Q8 ** 2
        white_max2 = CAMERA_RING_WHITE_MAX_Q8 ** 2
        outer_min2 = CAMERA_RING_OUTER_MIN_Q8 ** 2
        outer_max2 = CAMERA_RING_OUTER_MAX_Q8 ** 2
        for y in range(roi_min_y, roi_max_y + 1):
            for x in range(roi_min_x, roi_max_x + 1):
                nx = (abs(x - cx) * 512 + box_w // 2) // box_w
                ny = (abs(y - cy) * 512 + box_h // 2) // box_h
                radius2 = nx * nx + ny * ny
                luma = lumas[y * width + x]
                if radius2 < core_limit2:
                    core_sum += luma
                    core_count += 1
                elif white_min2 <= radius2 < white_max2:
                    white_sum += luma
                    white_count += 1
                elif outer_min2 <= radius2 < outer_max2:
                    outer_sum += luma
                    outer_count += 1
        if core_count and white_count and outer_count:
            center_mean = core_sum // core_count
            white_mean = white_sum // white_count
            outer_mean = outer_sum // outer_count
            contrasts = {
                'white_minus_center': white_mean - center_mean,
                'white_minus_outer': white_mean - outer_mean,
            }
            gray_score = min(100, max(0, min(contrasts.values())))
            structure_valid = gray_score >= int(meta.get('score', 0))
        else:
            center_mean = white_mean = outer_mean = 0
        sample_means = {
            'center_black': center_mean,
            'white_ring': white_mean,
            'outer_black': outer_mean,
        }
    previous_age = (int(previous.get('_structure_age', CAMERA_STRUCTURE_MAX_AGE))
                    if previous_compatible else CAMERA_STRUCTURE_MAX_AGE)
    if target == CAMERA_TARGET_MAGENTA_TRIPLE or cyan_valid:
        structure_age = CAMERA_STRUCTURE_MAX_AGE
    else:
        structure_age = 0 if structure_valid else min(
            CAMERA_STRUCTURE_MAX_AGE, previous_age + 1)
    candidate = {
        'valid': candidate_valid,
        'min_x': min_x if candidate_valid else 0,
        'min_y': min_y if candidate_valid else 0,
        'max_x': max_x if candidate_valid else 0,
        'max_y': max_y if candidate_valid else 0,
        'cx': cx, 'cy': cy, 'w': box_w, 'h': box_h,
    }
    result = {
        'state': 0, 'cx': 0, 'cy': 0, 'w': 0, 'h': 0,
        'area': min(count, 65535), 'score': fill_score,
        'u': last_u, 'v': last_v,
        'gray': gray_score if gray_enabled else 0,
        'min_x': 0, 'min_y': 0,
        '_missed': 0, '_mask': bytes(mask),
        '_candidate': candidate,
        '_sample_means': sample_means,
        '_contrasts': contrasts,
        '_gray_sampled': gray_enabled and core_valid,
        '_dark_count': dark_count,
        '_dark_ratio_q8': dark_ratio_q8,
        '_cyan_count': cyan_count,
        '_cyan_score': cyan_score,
        '_cyan_valid': cyan_valid,
        '_yellow_count': yellow_count,
        '_yellow_valid': yellow_valid,
        '_structure_valid': structure_valid,
        '_structure_age': structure_age,
        '_target': target,
        '_gray_enabled': gray_enabled,
        '_instant_tracking': False,
    }
    if not enabled or not track_enabled:
        return result

    area_ok = count >= int(meta.get('area', 1))
    if target == CAMERA_TARGET_MAGENTA:
        if gray_enabled:
            bullseye_valid = core_valid and (
                structure_valid or
                (previous_tracking and structure_age <= 3))
            instant_tracking = area_ok and (cyan_valid or bullseye_valid)
        else:
            instant_tracking = area_ok and core_valid
    elif target == CAMERA_TARGET_MAGENTA_TRIPLE:
        if gray_enabled:
            small_target = min(box_w, box_h) < CAMERA_TRIPLE_STRICT_MIN_SIZE
            instant_tracking = (area_ok and cyan_valid and
                                (yellow_valid or small_target))
        else:
            instant_tracking = area_ok and core_valid
    else:
        instant_tracking = area_ok
    if instant_tracking:
        result.update(state=1, cx=cx, cy=cy, w=box_w, h=box_h,
                      min_x=min_x, min_y=min_y)
        result['_instant_tracking'] = True
        return result

    lost_frames = int(meta.get('lost', 0))
    if previous_compatible and int(previous.get('state', 0)) == 1:
        missed = int(previous.get('_missed', 0))
        if missed < lost_frames:
            missed += 1
        if lost_frames != 0 and missed < lost_frames:
            for key in ('cx', 'cy', 'w', 'h', 'min_x', 'min_y'):
                result[key] = int(previous.get(key, 0))
            held_candidate = dict(previous.get('_candidate', {}))
            if not held_candidate.get('valid'):
                held_candidate = {
                    'valid': True,
                    'min_x': result['min_x'],
                    'min_y': result['min_y'],
                    'max_x': result['min_x'] + result['w'] - 1,
                    'max_y': result['min_y'] + result['h'] - 1,
                    'cx': result['cx'], 'cy': result['cy'],
                    'w': result['w'], 'h': result['h'],
                }
            held_candidate['held'] = True
            result['_candidate'] = held_candidate
            result['state'] = 1
            result['_missed'] = missed
            return result
    result['state'] = 2
    result['_missed'] = lost_frames
    return result


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)


def encode_png_rgb(width: int, height: int, rgb: bytes) -> bytes:
    if len(rgb) != width * height * 3:
        raise ValueError('RGB byte count does not match image dimensions')
    stride = width * 3
    raw = b''.join(b'\x00' + rgb[y * stride:(y + 1) * stride]
                   for y in range(height))
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + _png_chunk(b'IHDR', ihdr) +
            _png_chunk(b'IDAT', zlib.compress(raw, 6)) + _png_chunk(b'IEND', b''))


def _atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(data)
    temporary.replace(path)


def _atomic_json(path: Path, value: dict):
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2,
                                  sort_keys=True).encode('utf-8'))


def _draw_overlay(rgb: bytes, result: dict, width: int, height: int) -> bytes:
    overlay = bytearray(rgb)

    def set_pixel(x, y, color):
        if 0 <= x < width and 0 <= y < height:
            offset = (y * width + x) * 3
            overlay[offset:offset + 3] = bytes(color)

    def draw_marker(cx, cy, color):
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                set_pixel(cx + dx, cy + dy, color)

    candidate = result.get('_candidate', {})
    if candidate.get('valid'):
        x0 = int(candidate['min_x'])
        y0 = int(candidate['min_y'])
        x1 = int(candidate['max_x'])
        y1 = int(candidate['max_y'])
        box_color = (0, 255, 0) if int(result.get('state', 0)) == 1 else (255, 160, 0)
        for x in range(x0, x1 + 1):
            set_pixel(x, y0, box_color); set_pixel(x, y1, box_color)
        for y in range(y0, y1 + 1):
            set_pixel(x0, y, box_color); set_pixel(x1, y, box_color)
        cx, cy = int(candidate['cx']), int(candidate['cy'])
        for delta in range(-3, 4):
            set_pixel(cx + delta, cy, (255, 0, 0))
            set_pixel(cx, cy + delta, (255, 0, 0))
        if result.get('_gray_sampled', False):
            box_w = int(candidate['w'])
            box_h = int(candidate['h'])
            white_dx = max(1, (box_w * 36 + 128) >> 8)
            white_dy = max(1, (box_h * 36 + 128) >> 8)
            black_dx = max(1, (box_w * 64 + 128) >> 8)
            black_dy = max(1, (box_h * 64 + 128) >> 8)
            for x, y in ((cx + white_dx, cy), (cx - white_dx, cy),
                         (cx, cy + white_dy), (cx, cy - white_dy)):
                draw_marker(x, y, (0, 255, 255))
            for x, y in ((cx + black_dx, cy), (cx - black_dx, cy),
                         (cx, cy + black_dy), (cx, cy - black_dy)):
                draw_marker(x, y, (255, 255, 0))
    return bytes(overlay)


class CameraCaptureManager:
    def __init__(self, data_dir: Path, start_worker: bool = True,
                 retention_s: float = 10.0):
        self.root = Path(data_dir) / 'camera'
        self.live = self.root / 'live'
        self.frames = self.live / 'frames'
        self.saved = self.root / 'saved'
        self.frames.mkdir(parents=True, exist_ok=True)
        self.saved.mkdir(parents=True, exist_ok=True)
        self.retention_s = retention_s
        self._lock = threading.Lock()
        self._queue = queue.Queue(maxsize=8)
        self._sync_pending = deque()
        self._meta = None
        self._stat = None
        self._pc_previous = None
        self._worker_enabled = start_worker
        self._received_times = deque(maxlen=120)
        self._retention_hold = False
        self._session_frame_stems = None
        self._status = {
            'requested_period_ms': 0,
            'effective_period_ms': 0,
            'received': 0,
            'pc_skipped': 0,
            'firmware_skipped': 0,
            'partial': 0,
            'protocol_errors': 0,
            'processing_errors': 0,
            'latest_sequence': 0,
            'metadata_revision': 0,
            'subscribed': False,
        }
        if start_worker:
            threading.Thread(target=self._worker, daemon=True,
                             name='camera-capture').start()

    def handle_line(self, text: str):
        if text.startswith('CAMMETA,'):
            meta = _parse_kv_line(text)
            if 'rev' not in meta:
                self.record_protocol_error()
                return
            with self._lock:
                self._meta = meta
                self._status['metadata_revision'] = int(meta['rev'])
        elif text.startswith('CAMSTAT,'):
            with self._lock:
                self._stat = _parse_kv_line(text)
        elif text.startswith('ACK,cmd=START_STREAM_CAMERA_FRAME,'):
            values = _parse_kv_line(text)
            with self._lock:
                self._status['requested_period_ms'] = int(values.get('requested_ms', 0))
                self._status['effective_period_ms'] = int(values.get('effective_ms', 0))
                self._status['subscribed'] = True
                self._retention_hold = True
                self._session_frame_stems = []
        elif text.startswith('ACK,cmd=STOP_STREAM_CAMERA_FRAME,'):
            with self._lock:
                self._status['subscribed'] = False

    def accept_frame(self, frame: CameraFrame, received_at: Optional[float] = None) -> bool:
        received_at = time.time() if received_at is None else received_at
        with self._lock:
            meta = dict(self._meta) if self._meta else None
            stat = dict(self._stat) if self._stat else None
            self._stat = None
            if not meta or not stat or int(stat.get('frame', -1)) != frame.frame_seq:
                self._status['protocol_errors'] += 1
                return False
            self._status['received'] += 1
            self._status['latest_sequence'] = frame.frame_seq
            self._status['firmware_skipped'] = int(stat.get('tx_skip', 0))
            if 'requested_ms' in stat:
                self._status['requested_period_ms'] = int(stat['requested_ms'])
            if 'effective_ms' in stat:
                self._status['effective_period_ms'] = int(stat['effective_ms'])
            self._received_times.append(received_at)
            item = (frame, meta, stat, received_at)
            if not self._worker_enabled:
                self._sync_pending.append(item)
                return True
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._status['pc_skipped'] += 1
                except queue.Empty:
                    pass
                self._queue.put_nowait(item)
            return True

    def process_pending_sync(self):
        while self._sync_pending:
            self._process_item(self._sync_pending.popleft())

    def _worker(self):
        while True:
            item = self._queue.get()
            try:
                self._process_item(item)
            except Exception:
                with self._lock:
                    self._status['processing_errors'] += 1

    def _process_item(self, item):
        frame, meta, stat, received_at = item
        rgb = decode_rgb565(frame.payload)
        previous = self._pc_previous
        previous_seq = int(previous.get('_frame_seq', -1)) if previous else -1
        frame_gap = frame.frame_seq - previous_seq if previous_seq >= 0 else 0
        history_contiguous = bool(previous and
                                  previous.get('_history_valid', False) and
                                  frame_gap == 1)
        result = recognize_rgb565(frame.payload, meta,
                                  previous if history_contiguous else None)
        deterministic_state = (
            int(meta.get('alg', 0)) == 0 or
            int(meta.get('color', 0)) == 0 or
            int(meta.get('track', 0)) == 0 or
            int(meta.get('lost', 0)) == 0 or
            result['_instant_tracking'] or
            history_contiguous)
        result['_frame_seq'] = frame.frame_seq
        result['_frame_gap'] = frame_gap
        result['_history_valid'] = bool(deterministic_state)
        self._pc_previous = result
        mask_rgb = b''.join(bytes((value, value, value)) for value in result['_mask'])
        overlay = _draw_overlay(rgb, result, CAMERA_WIDTH, CAMERA_HEIGHT)
        pc_public = {k: v for k, v in result.items() if not k.startswith('_')}
        pc_diagnostics = {
            'candidate': result['_candidate'],
            'sample_means': result['_sample_means'],
            'contrasts': result['_contrasts'],
            'frame_gap': result['_frame_gap'],
            'history_valid': result['_history_valid'],
        }
        mismatch_comparable = {
            key: (result['_history_valid'] if key in ('state', 'cx', 'cy', 'w', 'h')
                  else True)
            for key in ('state', 'cx', 'cy', 'w', 'h', 'area', 'score')
        }
        mismatch = {
            key: (mismatch_comparable[key] and
                  int(stat.get(key, 0)) != int(pc_public.get(key, 0)))
            for key in mismatch_comparable
        }
        metadata = {
            'frame_seq': frame.frame_seq,
            'mcu_timestamp_ms': frame.timestamp_ms,
            'pc_receive_timestamp': received_at,
            'width': CAMERA_WIDTH,
            'height': CAMERA_HEIGHT,
            'format': 'RGB565_LE',
            'meta': meta,
            'mcu': stat,
            'pc': pc_public,
            'pc_diagnostics': pc_diagnostics,
            'mismatch': mismatch,
            'mismatch_comparable': mismatch_comparable,
        }
        latest_png = encode_png_rgb(CAMERA_WIDTH, CAMERA_HEIGHT, rgb)
        _atomic_write(self.live / 'latest.png', latest_png)
        _atomic_write(self.live / 'mask.png', encode_png_rgb(CAMERA_WIDTH, CAMERA_HEIGHT, mask_rgb))
        _atomic_write(self.live / 'overlay.png', encode_png_rgb(CAMERA_WIDTH, CAMERA_HEIGHT, overlay))
        _atomic_json(self.live / 'latest.json', metadata)
        stem = f'{int(received_at * 1000):013d}_{frame.frame_seq:010d}'
        _atomic_write(self.frames / f'{stem}.png', latest_png)
        _atomic_json(self.frames / f'{stem}.json', metadata)
        with self._lock:
            if self._session_frame_stems is not None:
                self._session_frame_stems.append(stem)
        self.prune_frames(time.time())

    def prune_frames(self, now: Optional[float] = None,
                     timestamp_for: Optional[Callable[[Path], float]] = None):
        with self._lock:
            if self._retention_hold:
                return
        now = time.time() if now is None else now
        timestamp_for = timestamp_for or (lambda path: path.stat().st_mtime)
        cutoff = now - self.retention_s
        for path in list(self.frames.iterdir()):
            try:
                if timestamp_for(path) < cutoff:
                    path.unlink()
            except OSError:
                pass

    def save_session(self, label: str = '') -> Path:
        safe = re.sub(r'[^A-Za-z0-9_-]+', '_', str(label)).strip('_')
        if not safe:
            safe = time.strftime('%Y%m%d_%H%M%S')
        destination = (self.saved / safe).resolve()
        if destination.parent != self.saved.resolve():
            raise ValueError('invalid camera session label')
        suffix = 1
        base = destination
        while destination.exists():
            destination = base.with_name(f'{base.name}_{suffix}')
            suffix += 1
        destination.mkdir(parents=True)
        with self._lock:
            session_frame_stems = (None if self._session_frame_stems is None
                                   else set(self._session_frame_stems))
        for name in ('latest.png', 'mask.png', 'overlay.png', 'latest.json'):
            source = self.live / name
            if source.is_file():
                shutil.copy2(source, destination / name)
        rolling = destination / 'frames'
        rolling.mkdir()
        for source in self.frames.iterdir():
            if (source.is_file() and
                    (session_frame_stems is None or
                     source.stem in session_frame_stems)):
                shutil.copy2(source, rolling / source.name)
        with self._lock:
            self._retention_hold = False
            self._session_frame_stems = None
        return destination

    def record_partial(self, count: int = 1):
        with self._lock:
            self._status['partial'] += count

    def record_protocol_error(self, count: int = 1):
        with self._lock:
            self._status['protocol_errors'] += count

    def status(self):
        with self._lock:
            status = dict(self._status)
            times = list(self._received_times)
        delivered_fps = 0.0
        if len(times) >= 2 and times[-1] > times[0]:
            delivered_fps = (len(times) - 1) / (times[-1] - times[0])
        requested = int(status['requested_period_ms'])
        effective = int(status['effective_period_ms'])
        status['skipped'] = int(status['pc_skipped']) + int(status['firmware_skipped'])
        status.update(
            requested_fps=(1000.0 / requested) if requested else 0.0,
            effective_fps=(1000.0 / effective) if effective else 0.0,
            delivered_fps=delivered_fps,
            latest_png=str((self.live / 'latest.png').resolve()),
            mask_png=str((self.live / 'mask.png').resolve()),
            overlay_png=str((self.live / 'overlay.png').resolve()),
            latest_json=str((self.live / 'latest.json').resolve()),
            rolling_dir=str(self.frames.resolve()),
            saved_dir=str(self.saved.resolve()),
        )
        return status
