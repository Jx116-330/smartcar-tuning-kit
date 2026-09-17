# -*- coding: utf-8 -*-
"""卡丁快跑综合科目语音动作序列解析回归测试。

运行: python -m unittest test_asr_vocab -v
安全纪律: 任一片段未知、歧义或超过 16 项时整批拒绝，不发送部分动作。
"""
import unittest

import asr_vocab as V


def parse(text):
    return V.canonicalize_sequence(text)


ACTION_PHRASES = [
    "左转向灯",
    "右转向灯",
    "远光灯",
    "近光灯",
    "雾灯",
    "双闪灯",
    "车内照明灯",
    "雨刷器",
    "鸣笛一秒",
    "鸣笛两秒",
    "鸣笛三秒",
    "鸣笛两声",
    "鸣笛三声",
    "鸣笛四声",
    "长短鸣笛",
    "急促鸣笛",
    "警报鸣笛",
]


class NormalizeTests(unittest.TestCase):
    def test_fullwidth_digits_and_punctuation(self):
        self.assertEqual("鸣笛2秒然后雾灯", V.normalize("鸣笛２秒，然后雾灯。"))
        self.assertEqual("", V.normalize("  ，。！？ "))

    def test_public_action_table_is_stable(self):
        self.assertEqual(17, len(V.ACTION_NAMES))
        self.assertEqual("左转向灯", V.ACTION_NAMES[0])
        self.assertEqual("警报鸣笛", V.ACTION_NAMES[16])
        V._selfcheck()


class SequenceSuccessTests(unittest.TestCase):
    def test_single_action(self):
        self.assertEqual(((0,), "ok"), parse("打开左转向灯"))

    def test_order_and_repeat_are_preserved(self):
        self.assertEqual(
            ((0, 8, 0), "ok"),
            parse("打开左转向灯，然后鸣笛一秒，再打开左转向灯"),
        )

    def test_all_supported_connectors_and_punctuation(self):
        self.assertEqual(
            ((0, 1, 2, 3, 4), "ok"),
            parse("请依次打开左转向灯、右转向灯，接着打开远光灯；再打开近光灯并且打开雾灯。"),
        )

    def test_action_phrases_can_be_adjacent(self):
        self.assertEqual(((0, 8, 2), "ok"), parse("左转向灯鸣笛一秒远光灯"))

    def test_sixteen_item_limit_is_accepted(self):
        sentence = "然后".join(ACTION_PHRASES[:16])
        self.assertEqual((tuple(range(16)), "ok"), parse(sentence))

    def test_common_asr_variants(self):
        self.assertEqual(((4, 2, 7), "ok"), parse("打开物灯，然后远光登，再打开雨刮器"))
        self.assertEqual(((12, 9), "ok"), parse("明帝三声接着名笛两秒"))
        self.assertEqual(((0,), "ok"), parse("坐转向灯"))

    def test_numeric_variants_do_not_change_action(self):
        self.assertEqual(((8, 9, 10), "ok"), parse("鸣笛１秒、鸣笛二秒、鸣笛3秒钟"))
        self.assertEqual(((11, 12, 13), "ok"), parse("鸣笛二声、鸣笛3声、鸣笛四声"))


class WholeBatchRejectTests(unittest.TestCase):
    def test_seventeenth_item_rejects_everything(self):
        sentence = "然后".join(ACTION_PHRASES)
        self.assertEqual(((), "too-many"), parse(sentence))

    def test_unknown_middle_fragment_rejects_everything(self):
        self.assertEqual(((), "unknown"), parse("左转向灯，然后打开车门，再鸣笛一秒"))

    def test_choice_word_is_ambiguous(self):
        self.assertEqual(((), "ambiguous"), parse("打开左转向灯或者右转向灯"))
        self.assertEqual(((), "ambiguous"), parse("远光灯还是近光灯"))

    def test_conflicting_polarity_is_ambiguous(self):
        self.assertEqual(((), "ambiguous"), parse("打开左右转向灯"))

    def test_empty_or_noise_rejects(self):
        self.assertEqual(((), "empty"), parse(" 。。。 "))
        self.assertEqual(((), "unknown"), parse("今天天气不错"))

    def test_unsupported_old_motion_and_gate_commands_reject(self):
        for text in ("前进10米", "蛇形后退10米", "通过门洞2", "门洞3返回"):
            with self.subTest(text=text):
                self.assertEqual(((), "unknown"), parse(text))

    def test_bad_number_never_fuzzy_matches(self):
        self.assertEqual(((), "unknown"), parse("鸣笛5声"))

    def test_never_raises_on_bad_input(self):
        for value in (None, "", "1234567890", "😀左转向灯", object()):
            with self.subTest(value=value):
                try:
                    result = parse(value)
                except Exception as exc:  # pragma: no cover - failure path only
                    self.fail(f"canonicalize_sequence({value!r}) raised {exc!r}")
                self.assertEqual(2, len(result))


if __name__ == "__main__":
    unittest.main(verbosity=2)
