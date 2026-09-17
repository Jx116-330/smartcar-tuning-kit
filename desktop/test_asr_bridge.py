import struct
import threading
import unittest
from unittest import mock

import asr_bridge


def server_frame(payload: bytes, opcode: int = 0x1, fin: bool = True) -> bytes:
    first = (0x80 if fin else 0x00) | opcode
    n = len(payload)
    if n < 126:
        return bytes([first, n]) + payload
    if n < 65536:
        return bytes([first, 126]) + struct.pack('>H', n) + payload
    return bytes([first, 127]) + struct.pack('>Q', n) + payload


class WebSocketStreamTests(unittest.TestCase):
    def test_fragmented_and_coalesced_frames(self):
        wire = server_frame(b'{"a":1}') + server_frame(b'{"b":2}')
        buf = bytearray(wire[:5])
        self.assertEqual([], asr_bridge._extract_ws_frames(buf))
        self.assertEqual(5, len(buf))

        buf.extend(wire[5:])
        frames = asr_bridge._extract_ws_frames(buf)
        self.assertEqual([(True, 0x1, b'{"a":1}'),
                          (True, 0x1, b'{"b":2}')], frames)
        self.assertEqual(bytearray(), buf)

    def test_result_text_rejects_multiline_but_keeps_long_pc_side_text(self):
        self.assertEqual('前进10米', asr_bridge._result_text_for_wire(' 前进10米 '))
        self.assertEqual('', asr_bridge._result_text_for_wire('  '))
        self.assertIsNone(asr_bridge._result_text_for_wire('前进10米\n后退10米'))
        self.assertEqual('测' * 80, asr_bridge._result_text_for_wire('测' * 80))


class CompactSequenceReplyTests(unittest.TestCase):
    @staticmethod
    def make_session(session_id=42, legacy=False):
        sent = []
        session = object.__new__(asr_bridge._AsrSession)
        session.session_id = session_id
        session.legacy = legacy
        session._reply_once = sent.append
        session._reply_failure = lambda reason: sent.append(
            'ASR:(error)' if legacy else f'ASR:FAIL {session_id} {reason}'
        )
        return session, sent

    def test_result_emits_ordered_compact_ids(self):
        session, sent = self.make_session()
        session._reply_result('左转向灯，然后鸣笛一秒，再左转向灯')
        self.assertEqual(['ASR:42:SEQ,3,0,8,0'], sent)

    def test_any_unknown_fragment_rejects_the_whole_batch(self):
        session, sent = self.make_session()
        session._reply_result('左转向灯，然后打开车门，再鸣笛一秒')
        self.assertEqual(['ASR:FAIL 42 unknown'], sent)

    def test_sixteen_items_fit_and_seventeen_are_rejected(self):
        phrases = [
            '左转向灯', '右转向灯', '远光灯', '近光灯', '雾灯', '双闪灯',
            '车内照明灯', '雨刷器', '鸣笛一秒', '鸣笛两秒', '鸣笛三秒',
            '鸣笛两声', '鸣笛三声', '鸣笛四声', '长短鸣笛', '急促鸣笛',
            '警报鸣笛',
        ]
        session, sent = self.make_session()
        session._reply_result('然后'.join(phrases[:16]))
        self.assertEqual(['ASR:42:SEQ,16,' + ','.join(str(i) for i in range(16))], sent)

        session, sent = self.make_session()
        session._reply_result('然后'.join(phrases))
        self.assertEqual(['ASR:FAIL 42 too-many'], sent)

    def test_parser_exception_fails_closed(self):
        session, sent = self.make_session()
        with mock.patch.object(asr_bridge.asr_vocab, 'canonicalize_sequence',
                               side_effect=RuntimeError('boom')):
            session._reply_result('左转向灯')
        self.assertEqual(['ASR:FAIL 42 PARSER_ERROR'], sent)

    def test_empty_result_reports_empty_reason(self):
        session, sent = self.make_session()
        session._reply_result('   ')
        self.assertEqual(['ASR:FAIL 42 empty'], sent)

    def test_longest_sequence_fits_mcu_line_buffer(self):
        line = 'ASR:4294967295:SEQ,16,' + ','.join(str(i) for i in range(16))
        self.assertLess(len(line) + 1, 256)  # +1 for trailing newline

    def test_reply_once_suppresses_duplicate_results(self):
        sent = []
        session = object.__new__(asr_bridge._AsrSession)
        session._send_to_car = sent.append
        session._cancelled = threading.Event()
        session._reply_lock = threading.Lock()
        session._replied = False
        session._reply_once('ASR:42:SEQ,1,0')
        session._reply_once('ASR:42:SEQ,1,1')
        self.assertEqual(['ASR:42:SEQ,1,0'], sent)

    def test_cloud_session_covers_thirty_second_capture(self):
        self.assertGreaterEqual(asr_bridge.SESSION_MAX_S, 35.0)


class SessionRoutingTests(unittest.TestCase):
    def test_new_session_cancels_old_and_ids_route_frames(self):
        made = []

        class FakeSession:
            def __init__(self, send_to_car, session_id=None, legacy=False):
                self.session_id = session_id
                self.legacy = legacy
                self._alive = True
                self.cancelled = False
                self.frames = []
                made.append(self)

            def feed(self, pcm, status):
                self.frames.append((pcm, status))

            def cancel(self):
                self.cancelled = True
                self._alive = False

        sent = []
        with mock.patch.object(asr_bridge, '_AsrSession', FakeSession), \
             mock.patch.object(asr_bridge, 'APP_ID', 'a'), \
             mock.patch.object(asr_bridge, 'API_KEY', 'k'), \
             mock.patch.object(asr_bridge, 'API_SECRET', 's'):
            asr_bridge._cur = None
            asr_bridge.handle_asr_line('ASR 10 0 AQI=', sent.append)
            asr_bridge.handle_asr_line('ASR 10 1 AwQ=', sent.append)
            self.assertEqual([(b'\x01\x02', 0), (b'\x03\x04', 1)], made[0].frames)

            asr_bridge.handle_asr_line('ASR 11 0 BQY=', sent.append)
            self.assertTrue(made[0].cancelled)
            self.assertEqual(11, made[1].session_id)

            # tuning_tool 会 strip 行尾空格；空 payload 末帧仍必须按新协议路由，
            # 且旧 sid 的末帧不能喂给当前 sid=11 会话。
            asr_bridge.handle_asr_line('ASR 10 2', sent.append)
            self.assertEqual([(b'\x05\x06', 0)], made[1].frames)

            asr_bridge.handle_asr_line('ASR 11 2', sent.append)
            self.assertEqual([(b'\x05\x06', 0), (b'', 2)], made[1].frames)

            asr_bridge.handle_asr_line('ASR CANCEL 11', sent.append)
            self.assertTrue(made[1].cancelled)

    def test_hello_waits_for_cloud_probe_before_ready(self):
        replies = []
        done = threading.Event()

        def send(line):
            replies.append(line)
            done.set()

        with mock.patch.object(asr_bridge, 'APP_ID', 'a'), \
             mock.patch.object(asr_bridge, 'API_KEY', 'k'), \
             mock.patch.object(asr_bridge, 'API_SECRET', 's'), \
             mock.patch.object(asr_bridge, '_cloud_connectivity_check', return_value=(True, 'OK')):
            asr_bridge._probe_inflight = False
            asr_bridge._probe_waiters.clear()
            asr_bridge._cloud_ready_until = 0.0
            asr_bridge.handle_asr_line('ASR HELLO 77', send)
            self.assertTrue(done.wait(1.0))
            self.assertEqual(['ASR:READY 77'], replies)


class AudioPreprocTests(unittest.TestCase):
    """前处理不变量:样本数/字节数不变、int16 合法、异常回退原始 PCM(2026-07-20)。"""

    def test_length_and_type_preserved(self):
        import math
        pre = asr_bridge._AudioPreproc()
        n = 800
        pcm = struct.pack('<%dh' % n,
                          *[int(3000 * math.sin(2 * math.pi * 440 * i / 8000))
                            for i in range(n)])
        out = pre.process(pcm)
        self.assertEqual(len(pcm), len(out))
        vals = struct.unpack('<%dh' % n, out)   # 解包失败=非法 int16,直接抛
        self.assertEqual(n, len(vals))

    def test_hpf_kills_dc_and_lf(self):
        import math
        n = 1600
        # 增益归一化会把残留一起抬(SNR 改善来自相对衰减),故锁 gain=1 单测滤波
        with mock.patch.object(asr_bridge, 'GAIN_MAX', 1.0):
            pre = asr_bridge._AudioPreproc()
            # 大直流偏置 + 50Hz 低频嗡嗡,滤后能量应大幅衰减
            pcm = struct.pack('<%dh' % n,
                              *[int(8000 + 6000 * math.sin(2 * math.pi * 50 * i / 8000))
                                for i in range(n)])
            out = struct.unpack('<%dh' % n, pre.process(pcm))
            # 看后半段(滤波器已过瞬态):残留应远小于输入幅度
            tail = out[n // 2:]
            self.assertLess(max(abs(v) for v in tail), 3000)

    def test_lf_attenuated_much_more_than_voice(self):
        import math
        n = 1600
        with mock.patch.object(asr_bridge, 'GAIN_MAX', 1.0):
            def peak_after(freq):
                pre = asr_bridge._AudioPreproc()
                pcm = struct.pack('<%dh' % n,
                                  *[int(6000 * math.sin(2 * math.pi * freq * i / 8000))
                                    for i in range(n)])
                out = struct.unpack('<%dh' % n, pre.process(pcm))
                return max(abs(v) for v in out[n // 2:])
            # 50Hz 噪声的存活率必须显著低于 300Hz 人声(SNR 净增益)
            self.assertLess(peak_after(50) * 2, peak_after(300))

    def test_voice_band_survives_and_gains(self):
        import math
        pre = asr_bridge._AudioPreproc()
        n = 1600
        # 300Hz(人声基频域)小振幅信号:应存活且被增益抬升,不削顶
        pcm = struct.pack('<%dh' % n,
                          *[int(2000 * math.sin(2 * math.pi * 300 * i / 8000))
                            for i in range(n)])
        out = struct.unpack('<%dh' % n, pre.process(pcm))
        tail = out[n // 2:]
        peak = max(abs(v) for v in tail)
        self.assertGreater(peak, 2000)      # 增益生效
        self.assertLessEqual(peak, 32767)   # 无溢出

    def test_bad_input_falls_back_to_original(self):
        pre = asr_bridge._AudioPreproc()
        odd = b'\x01\x02\x03'   # 奇数字节:尾字节丢弃属可接受,但绝不能抛
        out = pre.process(odd)
        self.assertIsInstance(out, bytes)
        empty = pre.process(b'')
        self.assertEqual(b'', empty)

    def test_disabled_is_identity(self):
        pre = asr_bridge._AudioPreproc()
        pcm = struct.pack('<4h', 100, -200, 300, -400)
        with mock.patch.object(asr_bridge, 'PREPROC_ENABLE', False):
            self.assertEqual(pcm, pre.process(pcm))


if __name__ == '__main__':
    unittest.main()
