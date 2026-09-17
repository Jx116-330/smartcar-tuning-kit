import json
import struct
import tempfile
import unittest
from pathlib import Path

from camera_capture import (
    CAMERA_TARGET_MAGENTA,
    CAMERA_PAYLOAD_LEN,
    CameraCaptureManager,
    CameraFrame,
    CameraProtocolError,
    MixedCameraStreamParser,
    _draw_overlay,
    decode_rgb565,
    recognize_rgb565,
)


def make_frame(seq=7, timestamp_ms=123, pixel=0xF800):
    payload = struct.pack('<H', pixel) * (CAMERA_PAYLOAD_LEN // 2)
    header = b'CIMG' + struct.pack('<III', seq, timestamp_ms, len(payload))
    return header + payload, payload


def rgb565(r, g, b):
    return ((r * 31 // 255) << 11) | ((g * 63 // 255) << 5) | (b * 31 // 255)


def make_target_payload(with_bullseye=True, sparse_noise=False, small=False,
                        asymmetric_border=False, dark_patch=False,
                        vertical_scale=1):
    width, height = 160, 120
    background = rgb565(20, 120, 40)
    magenta = rgb565(230, 52, 148)
    white = rgb565(240, 240, 240)
    black = rgb565(10, 10, 10)
    pixels = [background] * (width * height)

    def fill_rect(x0, y0, x1, y1, pixel):
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                pixels[y * width + x] = pixel

    def fill_disc(cx, cy, radius_x, radius_y, pixel):
        radius_squared = radius_x * radius_x * radius_y * radius_y
        for y in range(cy - radius_y, cy + radius_y + 1):
            for x in range(cx - radius_x, cx + radius_x + 1):
                dx = x - cx
                dy = y - cy
                if (dx * dx * radius_y * radius_y +
                        dy * dy * radius_x * radius_x <= radius_squared):
                    pixels[y * width + x] = pixel

    if small:
        fill_rect(70, 55, 81, 66, magenta)
    else:
        fill_rect(40, 22, 119, 101, magenta)
        fill_rect(48, 30, 111, 93, white)
        if with_bullseye:
            fill_disc(79, 61, 25, max(1, 25 // vertical_scale), black)
            fill_disc(79, 61, 15, max(1, 15 // vertical_scale), white)
            fill_disc(79, 61, 7, max(1, 7 // vertical_scale), black)
        elif dark_patch:
            fill_rect(69, 51, 89, 71, black)
        if asymmetric_border:
            fill_rect(105, 35, 111, 88, magenta)
        if sparse_noise:
            fill_rect(4, 4, 7, 7, magenta)
    return b''.join(struct.pack('<H', pixel) for pixel in pixels)


def make_magenta_blocks(*blocks):
    width, height = 160, 120
    background = rgb565(20, 120, 40)
    magenta = rgb565(230, 52, 148)
    pixels = [background] * (width * height)
    for x0, y0, x1, y1 in blocks:
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                pixels[y * width + x] = magenta
    return b''.join(struct.pack('<H', pixel) for pixel in pixels)


def make_dual_color_payload(size=80, vertical_scale=1):
    width, height = 160, 120
    background = rgb565(20, 120, 40)
    magenta = rgb565(230, 52, 148)
    cyan = rgb565(0, 220, 230)
    pixels = [background] * (width * height)
    marker_h = max(4, size // vertical_scale)
    x0 = (width - size) // 2
    y0 = (height - marker_h) // 2
    for local_y in range(marker_h):
        source_y = local_y * 80 // marker_h
        for local_x in range(size):
            source_x = local_x * 80 // size
            pixel = (cyan if 14 <= source_x <= 65 and
                     14 <= source_y <= 65 else magenta)
            pixels[(y0 + local_y) * width + x0 + local_x] = pixel
    return b''.join(struct.pack('<H', pixel) for pixel in pixels)


def make_triple_color_payload(size=80, yellow_offset_x=0):
    width, height = 160, 120
    background = rgb565(20, 120, 40)
    magenta = rgb565(230, 52, 148)
    cyan = rgb565(0, 220, 230)
    yellow = rgb565(255, 212, 0)
    pixels = [background] * (width * height)
    x0 = (width - size) // 2
    y0 = (height - size) // 2
    for local_y in range(size):
        source_y = local_y * 80 // size
        for local_x in range(size):
            source_x = local_x * 80 // size
            pixel = magenta
            if 14 <= source_x <= 65 and 14 <= source_y <= 65:
                pixel = cyan
            shifted_x = source_x - yellow_offset_x
            if 26 <= shifted_x <= 53 and 26 <= source_y <= 53:
                pixel = yellow
            pixels[(y0 + local_y) * width + x0 + local_x] = pixel
    return b''.join(struct.pack('<H', pixel) for pixel in pixels)


def make_notched_red_payload():
    width, height = 160, 120
    pixels = [rgb565(0, 255, 0)] * (width * height)
    for y in range(50, 70):
        for x in range(70, 90):
            pixels[y * width + x] = rgb565(255, 0, 0)
    for y in range(56, 64):
        for x in range(84, 90):
            pixels[y * width + x] = rgb565(0, 0, 0)
    return b''.join(struct.pack('<H', pixel) for pixel in pixels)


def bullseye_meta(gray=1, score=5, area=100, lost=0):
    return {
        'alg': 1, 'color': 1, 'gray': gray, 'track': 1,
        'target': CAMERA_TARGET_MAGENTA,
        'u_min': 110, 'u_max': 170,
        'v_min': 165, 'v_max': 240,
        'y_min': 35, 'y_max': 210,
        'area': area, 'score': score, 'lost': lost,
    }


def triple_meta(**overrides):
    meta = bullseye_meta()
    meta['target'] = 2
    meta.update(overrides)
    return meta


class MixedStreamParserTests(unittest.TestCase):
    def test_fragmented_and_coalesced_text_binary_stream(self):
        frame_wire, payload = make_frame()
        wire = b'CAMSTAT,frame=7,state=1\r\n' + frame_wire + b'ACK,cmd=NEXT\r\n'
        split_points = [1, 3, 4, 7, 15, 16, 17, 31, len(wire) - 1]
        for split in split_points:
            parser = MixedCameraStreamParser()
            events = []
            events.extend(parser.feed(wire[:split], now=1.0))
            events.extend(parser.feed(wire[split:], now=1.1))
            self.assertEqual('line', events[0][0], split)
            self.assertEqual('CAMSTAT,frame=7,state=1', events[0][1], split)
            self.assertEqual('frame', events[1][0], split)
            self.assertEqual(7, events[1][1].frame_seq, split)
            self.assertEqual(payload, events[1][1].payload, split)
            self.assertEqual(('line', 'ACK,cmd=NEXT'), events[2], split)

    def test_invalid_length_is_fatal_and_counted(self):
        parser = MixedCameraStreamParser()
        with self.assertRaises(CameraProtocolError):
            parser.feed(b'CIMG' + struct.pack('<III', 1, 2, 99), now=1.0)
        self.assertEqual(1, parser.protocol_errors)

    def test_disconnect_and_timeout_mark_partial_frame(self):
        frame_wire, _ = make_frame()
        parser = MixedCameraStreamParser(frame_timeout_s=2.0)
        parser.feed(frame_wire[:100], now=1.0)
        self.assertTrue(parser.frame_timed_out(3.1))
        parser.disconnect()
        self.assertEqual(1, parser.partial_frames)
        self.assertEqual([], parser.feed(b'ACK,cmd=OK\r\n', now=4.0)[:0])

        partial_header = MixedCameraStreamParser(frame_timeout_s=2.0)
        partial_header.feed(b'CIMG\x01\x00', now=5.0)
        self.assertTrue(partial_header.frame_timed_out(7.1))


class RecognitionParityTests(unittest.TestCase):
    def test_rgb565_little_endian_primary_colors(self):
        payload = struct.pack('<HHH', 0xF800, 0x07E0, 0x001F)
        self.assertEqual(bytes((255, 0, 0, 0, 255, 0, 0, 0, 255)),
                         decode_rgb565(payload, width=3, height=1))

    def test_full_red_frame_matches_firmware_integer_math(self):
        _, payload = make_frame(pixel=0xF800)
        meta = {
            'alg': 1, 'color': 1, 'track': 1,
            'target': 1,
            'u_min': 0, 'u_max': 255,
            'v_min': 200, 'v_max': 255,
            'y_min': 0, 'y_max': 255,
            'area': 20, 'score': 50, 'lost': 3,
        }
        result = recognize_rgb565(payload, meta)
        self.assertEqual(1, result['state'])
        self.assertEqual(19200, result['area'])
        self.assertEqual(79, result['cx'])
        self.assertEqual(59, result['cy'])
        self.assertEqual(160, result['w'])
        self.assertEqual(120, result['h'])
        self.assertEqual(100, result['score'])

    def test_red_target_keeps_component_pixel_centroid(self):
        meta = {
            'alg': 1, 'color': 1, 'track': 1, 'target': 1,
            'u_min': 0, 'u_max': 140,
            'v_min': 160, 'v_max': 255,
            'y_min': 20, 'y_max': 240,
            'area': 20, 'score': 50, 'lost': 0,
        }
        result = recognize_rgb565(make_notched_red_payload(), meta)
        self.assertEqual(1, result['state'])
        self.assertEqual((78, 59), (result['cx'], result['cy']))

    def test_magenta_bullseye_tracks_with_soft_structure_score(self):
        result = recognize_rgb565(make_target_payload(), bullseye_meta())
        self.assertEqual(1, result['state'])
        self.assertEqual(79, result['cx'])
        self.assertEqual(61, result['cy'])
        self.assertEqual(80, result['w'])
        self.assertEqual(80, result['h'])
        self.assertEqual(2304, result['area'])
        self.assertGreater(result['gray'], 0)

    def test_asymmetric_magenta_border_uses_dark_circle_center(self):
        result = recognize_rgb565(
            make_target_payload(asymmetric_border=True),
            bullseye_meta())
        self.assertEqual(1, result['state'])
        self.assertEqual((79, 61), (result['cx'], result['cy']))

    def test_solid_magenta_frame_cannot_acquire(self):
        result = recognize_rgb565(make_target_payload(with_bullseye=False),
                                  bullseye_meta())
        self.assertEqual(2, result['state'])
        self.assertFalse(result['_instant_tracking'])

    def test_dark_patch_without_rings_cannot_acquire(self):
        result = recognize_rgb565(
            make_target_payload(with_bullseye=False, dark_patch=True),
            bullseye_meta())
        self.assertEqual(2, result['state'])
        self.assertGreaterEqual(result.get('_dark_ratio_q8', 0), 13)

    def test_gray_off_allows_dark_core_calibration(self):
        result = recognize_rgb565(
            make_target_payload(with_bullseye=False, dark_patch=True),
            bullseye_meta(gray=0))
        self.assertEqual(1, result['state'])

    def test_vertically_compressed_bullseye_acquires(self):
        result = recognize_rgb565(
            make_target_payload(vertical_scale=2), bullseye_meta())
        self.assertEqual(1, result['state'])

    def test_structure_identity_expires_on_fourth_ring_miss(self):
        meta = bullseye_meta(lost=0)
        result = recognize_rgb565(make_target_payload(), meta)
        self.assertEqual(1, result['state'])
        weak = make_target_payload(with_bullseye=False, dark_patch=True)
        for expected_age in (1, 2, 3):
            result = recognize_rgb565(weak, meta, result)
            self.assertEqual(1, result['state'])
            self.assertEqual(expected_age, result.get('_structure_age'))
        result = recognize_rgb565(weak, meta, result)
        self.assertEqual(2, result['state'])
        self.assertEqual(4, result.get('_structure_age'))

    def test_dual_color_marker_acquires_and_uses_cyan_centroid(self):
        result = recognize_rgb565(make_dual_color_payload(), bullseye_meta())
        self.assertEqual(1, result['state'])
        self.assertEqual((79, 59), (result['cx'], result['cy']))
        self.assertTrue(result.get('_cyan_valid', False))

    def test_dual_color_marker_reacquires_directly_from_lost(self):
        meta = bullseye_meta(lost=0)
        lost = recognize_rgb565(
            make_target_payload(with_bullseye=False), meta)
        self.assertEqual(2, lost['state'])
        result = recognize_rgb565(make_dual_color_payload(), meta, lost)
        self.assertEqual(1, result['state'])
        self.assertTrue(result.get('_instant_tracking', False))

    def test_small_dual_color_marker_acquires(self):
        result = recognize_rgb565(
            make_dual_color_payload(size=12, vertical_scale=1),
            bullseye_meta(area=1))
        self.assertEqual(1, result['state'])
        self.assertTrue(result.get('_cyan_valid', False))

    def test_dual_color_marker_does_not_refresh_bullseye_history(self):
        meta = bullseye_meta(lost=0)
        bullseye = recognize_rgb565(make_target_payload(), meta)
        dual_color = recognize_rgb565(make_dual_color_payload(), meta, bullseye)
        result = recognize_rgb565(
            make_target_payload(with_bullseye=False, dark_patch=True),
            meta, dual_color)
        self.assertEqual(2, result['state'])

    def test_triple_color_marker_acquires_and_uses_yellow_centroid(self):
        result = recognize_rgb565(
            make_triple_color_payload(yellow_offset_x=2), triple_meta())
        self.assertEqual(1, result['state'])
        self.assertTrue(result.get('_yellow_valid', False))
        self.assertEqual((81, 59), (result['cx'], result['cy']))

    def test_triple_mode_rejects_full_size_dual_marker(self):
        result = recognize_rgb565(make_dual_color_payload(), triple_meta())
        self.assertEqual(2, result['state'])

    def test_triple_marker_reacquires_directly_from_lost(self):
        meta = triple_meta(lost=0)
        lost = recognize_rgb565(make_magenta_blocks(), meta)
        self.assertEqual(2, lost['state'])
        result = recognize_rgb565(make_triple_color_payload(), meta, lost)
        self.assertEqual(1, result['state'])
        self.assertTrue(result.get('_instant_tracking', False))

    def test_triple_mode_allows_sub_12_pixel_cyan_fallback(self):
        result = recognize_rgb565(
            make_dual_color_payload(size=10), triple_meta(area=1))
        self.assertEqual(1, result['state'])
        self.assertFalse(result.get('_yellow_valid', False))

    def test_isolated_magenta_noise_does_not_expand_target(self):
        result = recognize_rgb565(make_target_payload(sparse_noise=True),
                                  bullseye_meta())
        self.assertEqual(1, result['state'])
        self.assertEqual((79, 61, 80, 80),
                         (result['cx'], result['cy'], result['w'], result['h']))

    def test_disconnected_magenta_areas_do_not_aggregate(self):
        result = recognize_rgb565(
            make_magenta_blocks((20, 20, 23, 23), (120, 80, 123, 83)),
            bullseye_meta(gray=0, area=20))
        self.assertEqual(2, result['state'])
        self.assertEqual(16, result['area'])

    def test_largest_magenta_component_wins(self):
        result = recognize_rgb565(
            make_magenta_blocks((70, 55, 81, 66), (120, 80, 127, 87)),
            bullseye_meta(gray=0, area=20))
        candidate = result['_candidate']
        self.assertEqual((144, 75, 60, 12, 12),
                         (result['area'], candidate['cx'], candidate['cy'],
                          candidate['w'], candidate['h']))

    def test_diagonal_magenta_regions_are_separate(self):
        result = recognize_rgb565(
            make_magenta_blocks((20, 20, 29, 29), (30, 30, 37, 37)),
            bullseye_meta(gray=0, area=20))
        candidate = result['_candidate']
        self.assertEqual((100, 24, 24, 10, 10),
                         (result['area'], candidate['cx'], candidate['cy'],
                          candidate['w'], candidate['h']))

    def test_color_only_small_target_and_area_gate(self):
        color_only = recognize_rgb565(
            make_target_payload(with_bullseye=False, dark_patch=True),
            bullseye_meta(gray=0))
        self.assertEqual(1, color_only['state'])
        self.assertEqual(0, color_only['gray'])
        below_area = recognize_rgb565(
            make_target_payload(with_bullseye=False, dark_patch=True),
            bullseye_meta(gray=0, area=3000))
        self.assertEqual(2, below_area['state'])
        self.assertEqual(2304, below_area['area'])

    def test_overlay_marks_soft_gray_samples(self):
        payload = make_target_payload()
        rgb = decode_rgb565(payload)
        result = recognize_rgb565(payload, bullseye_meta())
        overlay = _draw_overlay(rgb, result, 160, 120)
        offset = (result['cy'] * 160 + result['cx'] + 11) * 3
        self.assertNotEqual(rgb[offset:offset + 3], overlay[offset:offset + 3])

    def test_lost_hysteresis_keeps_overlay_candidate(self):
        meta = bullseye_meta(area=200, lost=3)
        tracked = recognize_rgb565(make_target_payload(), meta)
        held = recognize_rgb565(make_target_payload(small=True), meta, tracked)
        self.assertEqual(1, held['state'])
        self.assertTrue(held['_candidate']['valid'])
        self.assertTrue(held['_candidate']['held'])
        self.assertEqual((tracked['cx'], tracked['cy'], tracked['w'], tracked['h']),
                         (held['cx'], held['cy'], held['w'], held['h']))


class CaptureArtifactsTests(unittest.TestCase):
    def test_metadata_binding_artifacts_retention_and_save(self):
        frame_wire, payload = make_frame(seq=9, timestamp_ms=321)
        del frame_wire
        with tempfile.TemporaryDirectory() as td:
            manager = CameraCaptureManager(Path(td), start_worker=False,
                                           retention_s=10.0)
            manager.handle_line(
                'CAMMETA,rev=4,alg=1,color=1,track=1,target=1,'
                'u_min=0,u_max=255,v_min=200,v_max=255,y_min=0,y_max=255,'
                'area=20,score=50,lost=3,format=0')
            manager.handle_line(
                'CAMSTAT,frame=9,ms=321,drop=0,fps=30,proc_us=900,'
                'cx=79,cy=59,w=160,h=120,area=19200,score=100,state=1')
            self.assertTrue(manager.accept_frame(CameraFrame(9, 321, payload),
                                                 received_at=100.0))
            manager.process_pending_sync()

            live = Path(td) / 'camera' / 'live'
            self.assertTrue((live / 'latest.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertTrue((live / 'mask.png').exists())
            self.assertTrue((live / 'overlay.png').exists())
            latest = json.loads((live / 'latest.json').read_text(encoding='utf-8'))
            self.assertEqual(9, latest['frame_seq'])
            self.assertEqual(4, latest['meta']['rev'])
            self.assertFalse(latest['mismatch']['state'])
            diagnostics = latest['pc_diagnostics']
            self.assertEqual(
                {'center_black', 'white_ring', 'outer_black'},
                set(diagnostics['sample_means']))
            self.assertEqual(
                {'white_minus_center', 'white_minus_outer'},
                set(diagnostics['contrasts']))

            old = live / 'frames' / 'old.png'
            old.write_bytes(b'old')
            old.touch()
            manager.prune_frames(now=111.0, timestamp_for=lambda _: 100.0)
            self.assertFalse(old.exists())

            saved = manager.save_session('../bad label')
            self.assertEqual((Path(td) / 'camera' / 'saved'), saved.parent)
            self.assertTrue((saved / 'latest.json').exists())
            status = manager.status()
            self.assertEqual(1, status['received'])
            self.assertEqual(9, status['latest_sequence'])

    def test_mismatched_camstat_is_rejected(self):
        _, payload = make_frame(seq=10)
        with tempfile.TemporaryDirectory() as td:
            manager = CameraCaptureManager(Path(td), start_worker=False)
            manager.handle_line('CAMMETA,rev=1,alg=0,color=0,track=0')
            manager.handle_line('CAMSTAT,frame=9,state=0')
            self.assertFalse(manager.accept_frame(CameraFrame(10, 1, payload),
                                                  received_at=1.0))
            self.assertEqual(1, manager.status()['protocol_errors'])

    def test_recording_retains_frames_until_session_is_saved(self):
        with tempfile.TemporaryDirectory() as td:
            manager = CameraCaptureManager(Path(td), start_worker=False,
                                           retention_s=10.0)
            manager.handle_line(
                'CAMMETA,rev=1,alg=0,color=0,track=0')
            manager.handle_line(
                'CAMSTAT,frame=9,cx=0,cy=0,w=0,h=0,area=0,score=0,state=0')
            _, payload = make_frame(seq=9)
            self.assertTrue(manager.accept_frame(CameraFrame(9, 1, payload), 100.0))
            manager.process_pending_sync()
            old = Path(td) / 'camera' / 'live' / 'frames' / 'old.png'
            old.write_bytes(b'old')

            manager.handle_line('ACK,cmd=START_STREAM_CAMERA_FRAME,requested_ms=33,effective_ms=33')
            manager.prune_frames(now=111.0, timestamp_for=lambda _: 100.0)
            self.assertTrue(old.exists())

            manager.handle_line(
                'CAMSTAT,frame=10,cx=0,cy=0,w=0,h=0,area=0,score=0,state=0')
            _, current_payload = make_frame(seq=10)
            self.assertTrue(manager.accept_frame(
                CameraFrame(10, 2, current_payload), 105.0))
            manager.process_pending_sync()

            manager.handle_line('ACK,cmd=STOP_STREAM_CAMERA_FRAME,state=IDLE')
            manager.prune_frames(now=111.0, timestamp_for=lambda _: 100.0)
            self.assertTrue(old.exists())

            saved = manager.save_session('retention-test')
            saved_frames = sorted(path.name for path in (saved / 'frames').iterdir())
            self.assertEqual(
                ['0000000105000_0000000010.json',
                 '0000000105000_0000000010.png'],
                saved_frames)
            manager.prune_frames(now=111.0, timestamp_for=lambda _: 100.0)
            self.assertFalse(old.exists())

    def test_frame_gap_marks_lost_state_as_not_comparable(self):
        _, positive = make_frame(seq=9)
        _, negative = make_frame(seq=13, pixel=rgb565(0, 255, 0))
        with tempfile.TemporaryDirectory() as td:
            manager = CameraCaptureManager(Path(td), start_worker=False)
            manager.handle_line(
                'CAMMETA,rev=1,alg=1,color=1,gray=0,track=1,'
                'u_min=0,u_max=255,v_min=200,v_max=255,y_min=0,y_max=255,'
                'area=20,score=50,lost=3,format=0')
            manager.handle_line(
                'CAMSTAT,frame=9,cx=79,cy=59,w=160,h=120,'
                'area=19200,score=100,state=1')
            self.assertTrue(manager.accept_frame(CameraFrame(9, 1, positive), 1.0))
            manager.process_pending_sync()
            manager.handle_line(
                'CAMSTAT,frame=13,cx=0,cy=0,w=0,h=0,area=0,score=0,state=2')
            self.assertTrue(manager.accept_frame(CameraFrame(13, 2, negative), 2.0))
            manager.process_pending_sync()
            latest = json.loads(
                (Path(td) / 'camera' / 'live' / 'latest.json').read_text('utf-8'))
            self.assertFalse(latest['mismatch_comparable']['state'])
            self.assertFalse(latest['mismatch_comparable']['cx'])


if __name__ == '__main__':
    unittest.main()
