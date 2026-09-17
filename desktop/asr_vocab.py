# -*- coding: utf-8 -*-
"""卡丁快跑综合科目 ASR 动作序列规整器。

PC 端把讯飞整段最终文本解析成最多 16 个灯光/鸣笛动作 ID。解析只接受
白名单动作与白名单连接词；任一未知、歧义或超限都返回空序列，绝不下发半批。
"""

MAX_SEQUENCE_ITEMS = 16

ACTION_NAMES = (
    "左转向灯",
    "右转向灯",
    "远光灯",
    "近光灯",
    "雾灯",
    "双闪灯",
    "车内照明灯",
    "雨刷器",
    "鸣笛1秒",
    "鸣笛2秒",
    "鸣笛3秒",
    "鸣笛两声",
    "鸣笛三声",
    "鸣笛四声",
    "长短鸣笛",
    "急促鸣笛",
    "警报鸣笛",
)

_FULLWIDTH_DIGIT = {chr(0xFF10 + i): chr(ord("0") + i) for i in range(10)}


def _is_kept_char(ch):
    if ch.isascii():
        return ch.isalnum()
    codepoint = ord(ch)
    return 0x4E00 <= codepoint <= 0x9FFF


def normalize(text):
    """保留 ASCII 字母数字和 CJK，转换全角数字，移除空白与标点。"""
    if not isinstance(text, str) or not text:
        return ""
    out = []
    for ch in text:
        ch = _FULLWIDTH_DIGIT.get(ch, ch)
        if _is_kept_char(ch):
            out.append(ch.lower() if ch.isascii() else ch)
    return "".join(out)


_ALIASES = {
    0: (
        "打开左转向灯", "开启左转向灯", "左转向灯", "左转灯",
        "打开坐转向灯", "坐转向灯",
    ),
    1: (
        "打开右转向灯", "开启右转向灯", "右转向灯", "右转灯",
    ),
    2: (
        "打开远光灯", "开启远光灯", "远光灯", "打开远光登", "远光登",
    ),
    3: (
        "打开近光灯", "开启近光灯", "近光灯", "打开近光登", "近光登",
    ),
    4: (
        "打开雾灯", "开启雾灯", "雾灯", "打开物灯", "物灯",
    ),
    5: (
        "打开双闪灯", "开启双闪灯", "打开双闪", "双闪灯", "双闪", "危险报警灯",
    ),
    6: (
        "打开车内照明灯", "开启车内照明灯", "车内照明灯",
        "打开室内照明灯", "室内照明灯", "打开照明灯", "照明灯",
    ),
    7: (
        "打开雨刷器", "开启雨刷器", "雨刷器", "打开雨刷", "雨刷",
        "打开雨刮器", "开启雨刮器", "雨刮器",
    ),
    14: ("长短鸣笛", "长短名笛", "长短明帝"),
    15: ("急促鸣笛", "急促名笛", "急促明帝"),
    16: ("警报鸣笛", "警报名笛", "警报明帝"),
}

_HORN_PREFIXES = ("鸣笛", "名笛", "明帝")
_SECOND_WORDS = (
    (8, ("1秒", "1秒钟", "一秒", "一秒钟")),
    (9, ("2秒", "2秒钟", "二秒", "二秒钟", "两秒", "两秒钟")),
    (10, ("3秒", "3秒钟", "三秒", "三秒钟")),
)
_COUNT_WORDS = (
    (11, ("2声", "二声", "两声")),
    (12, ("3声", "三声")),
    (13, ("4声", "四声")),
)

for _action_id, _suffixes in _SECOND_WORDS + _COUNT_WORDS:
    _ALIASES[_action_id] = tuple(
        prefix + suffix for prefix in _HORN_PREFIXES for suffix in _suffixes
    )


def _build_alias_entries():
    owner = {}
    entries = []
    for action_id, aliases in _ALIASES.items():
        for alias in aliases:
            normalized = normalize(alias)
            if not normalized:
                raise AssertionError(f"empty alias for action {action_id}")
            previous = owner.get(normalized)
            if previous is not None and previous != action_id:
                raise AssertionError(
                    f"alias {normalized!r} maps to both {previous} and {action_id}"
                )
            owner[normalized] = action_id
    for alias, action_id in owner.items():
        entries.append((alias, action_id))
    entries.sort(key=lambda item: (-len(item[0]), item[0], item[1]))
    return tuple(entries)


_ALIAS_ENTRIES = _build_alias_entries()

_ALLOWED_GAP_TOKENS = tuple(sorted((
    "请按顺序", "请依次", "接下来", "最后再", "然后再", "再然后",
    "请你", "帮我", "给我", "按顺序", "依次", "然后", "接着", "随后",
    "并且", "最后", "同时", "再把", "先把", "开启", "打开", "执行",
    "请", "先", "再", "并", "和", "以及", "把", "即可", "就行", "谢谢",
), key=len, reverse=True))

_CHOICE_TOKENS = ("或者", "还是", "任选", "任意一个", "二选一")
_COMPACT_AMBIGUOUS = ("左右转向灯", "左右转灯", "远近光灯", "近远光灯")


def _gap_is_allowed(gap):
    """True only when the whole gap is composed of known harmless filler/connectors."""
    cursor = 0
    while cursor < len(gap):
        for token in _ALLOWED_GAP_TOKENS:
            if gap.startswith(token, cursor):
                cursor += len(token)
                break
        else:
            return False
    return True


def _next_action(text, cursor):
    """Return earliest longest whitelist action at/after cursor."""
    for start in range(cursor, len(text)):
        matches = [
            (alias, action_id)
            for alias, action_id in _ALIAS_ENTRIES
            if text.startswith(alias, start)
        ]
        if not matches:
            continue
        longest = len(matches[0][0])
        best = [(alias, action_id) for alias, action_id in matches if len(alias) == longest]
        ids = {action_id for _, action_id in best}
        if len(ids) != 1:
            return start, start, None
        alias, action_id = best[0]
        return start, start + len(alias), action_id
    return None


def canonicalize_sequence(text):
    """Return ``(tuple(ids), reason)``; any unsafe batch returns an empty tuple."""
    normalized = normalize(text)
    if not normalized:
        return (), "empty"
    if any(token in normalized for token in _CHOICE_TOKENS):
        return (), "ambiguous"
    if any(token in normalized for token in _COMPACT_AMBIGUOUS):
        return (), "ambiguous"

    action_ids = []
    cursor = 0
    while cursor < len(normalized):
        match = _next_action(normalized, cursor)
        if match is None:
            return (tuple(action_ids), "ok") if action_ids and _gap_is_allowed(normalized[cursor:]) else ((), "unknown")
        start, end, action_id = match
        if action_id is None:
            return (), "ambiguous"
        if not _gap_is_allowed(normalized[cursor:start]):
            return (), "unknown"
        action_ids.append(action_id)
        if len(action_ids) > MAX_SEQUENCE_ITEMS:
            return (), "too-many"
        cursor = end

    return (tuple(action_ids), "ok") if action_ids else ((), "unknown")


def _selfcheck():
    if len(ACTION_NAMES) != 17:
        raise AssertionError("action ID table must contain exactly 17 entries")
    if set(_ALIASES) != set(range(len(ACTION_NAMES))):
        raise AssertionError("every action ID must own at least one alias")
    _build_alias_entries()


_selfcheck()
