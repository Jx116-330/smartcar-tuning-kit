#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_selfcheck_font.py — 生成 IPS200 自检页所需的 16x16 中文点阵字库。

输出格式严格对齐固件 libraries/zf_device/zf_device_ips200.c 的 ips200_show_chinese:
  每字 16 行 × 2 字节 = 32 字节，行主序(row-major)，每字节 MSB 优先(bit7=最左像素),
  bit=1 -> 前景色, bit=0 -> 背景色。多字连续存放(字 G 在偏移 G*32)。
  这就是 PCtoLCD2002 的「阴码、逐行式、顺向」，与 zf_common_font.c 的 chinese_test[][16] 同格式。

用法:
  python gen_selfcheck_font.py            # 生成 .c/.h 到固件仓 code/menu/
  python gen_selfcheck_font.py --preview  # 额外打印几个字的点阵预览便于肉眼校对

短语按「名字 -> 中文串」定义;每个短语生成一段连续字模数组 SC_CN_<NAME>[] + #define SC_CN_<NAME>_N。
菜单端 ips200_show_chinese(x,y,16, SC_CN_<NAME>, SC_CN_<NAME>_N, color) 直接渲染,零运行期解码。
"""
import os
import sys
import argparse
from PIL import Image, ImageFont, ImageDraw

# 输出落点:固件仓 code/menu/(已在 ADS 编译目录里,刷新工程即纳入构建)
REPO = r"E:\ads\Seekfree_TC387_Opensource_Library"
OUT_C = os.path.join(REPO, "code", "menu", "selfcheck_font_cn.c")
OUT_H = os.path.join(REPO, "code", "menu", "selfcheck_font_cn.h")

# 字体候选:黑体(粗、小字号更清晰)优先,SimSun 兜底
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
]
GLYPH_SIZE = 16
FONT_PX    = 16     # 字号(em);16 基本填满 16px 盒
THRESHOLD  = 100    # 灰度二值化阈值,越低笔画越粗(小字号偏粗更易读)

# ---- 短语表:名字 -> 中文 ----
PHRASES = {
    # 标题 / 总结
    "TITLE":      "整车自检",
    "SUM_ALLOK":  "全部正常",
    "SUM_REQBAD": "必要异常",
    "SUM_OPTBAD": "非必要异常",
    "SUM_WAITACT":"待主动自检",
    # 状态词
    "ST_OK":   "正常",
    "ST_FAIL": "异常",
    "ST_SKIP": "跳过",
    "ST_WARN": "警告",
    "ST_WAIT": "待测",
    # 检查项标签
    "L_IMU":   "惯导",
    "L_BAT":   "电池",
    "L_THR":   "油门",
    "L_BRK":   "刹车",
    "L_PARAM": "参数",
    "L_TRACK": "轨迹",
    "L_MIC":   "硅麦",
    "L_FENC":  "前编码",
    "L_WIFI":  "无线",
    "L_SD":    "存储卡",
    "L_MOTL":  "左后轮",
    "L_MOTR":  "右后轮",
    "L_STEER": "转向",
    "L_KEY":   "按键",
    "L_HORN":  "喇叭",
    # 确认页 / 主动页 / 向导
    "CF_TITLE":  "离地确认",
    "CF_WARN":   "车轮离地",
    "CF_MOTOR":  "电机会转",
    "CF_LONG":   "长按",
    "CF_CONFIRM":"确认",
    "CF_SHORT":  "短按",
    "CF_CANCEL": "取消",
    "RUN_TITLE": "主动自检",
    "WZ_PRESS":  "请按",
    "WZ_THR":    "踩油门",
    "WZ_BRK":    "踩刹车",
    "WZ_DONE":   "完成",
    "FT_ACTIVE": "自检",
    "FT_RETEST": "重测",
    "FT_BACK":   "返回",
    "MISC_TESTING": "检测中",
    "MISC_ABORT":   "刹车中止",
}


def load_font():
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, FONT_PX), path
    raise SystemExit("找不到可用中文字体: " + ", ".join(FONT_CANDIDATES))


def glyph_bitmap(ch, font):
    """渲染单字为 16x16 的 0/1 像素网格(list of 16 rows, each 16 ints)。"""
    img = Image.new("L", (GLYPH_SIZE, GLYPH_SIZE), 0)
    d = ImageDraw.Draw(img)
    # anchor="mm" 以字形中心对齐盒中心;个别字号偏移微调 y-0
    d.text((GLYPH_SIZE / 2, GLYPH_SIZE / 2 - 0), ch, font=font, fill=255, anchor="mm")
    px = img.load()
    grid = []
    for r in range(GLYPH_SIZE):
        row = []
        for c in range(GLYPH_SIZE):
            row.append(1 if px[c, r] >= THRESHOLD else 0)
        grid.append(row)
    return grid


def grid_to_bytes(grid):
    """16x16 网格 -> 32 字节, 行主序 MSB 优先(与 ips200_show_chinese 一致)。"""
    out = bytearray()
    for r in range(GLYPH_SIZE):
        for cbyte in range(GLYPH_SIZE // 8):   # 左字节, 右字节
            b = 0
            for bit in range(8):               # MSB 先: bit0 -> 0x80
                x = cbyte * 8 + bit
                if grid[r][x]:
                    b |= (0x80 >> bit)
            out.append(b)
    return bytes(out)


def decode_bytes(buf):
    """按固件 ips200_show_chinese 的读法反解 -> 网格, 用于往返自检。"""
    grid = [[0] * GLYPH_SIZE for _ in range(GLYPH_SIZE)]
    for r in range(GLYPH_SIZE):
        for cbyte in range(GLYPH_SIZE // 8):
            b = buf[r * (GLYPH_SIZE // 8) + cbyte]
            for j in range(8, 0, -1):          # 与固件 (*p >> (j-1)) & 1 一致, MSB 先
                on = (b >> (j - 1)) & 0x01
                x = cbyte * 8 + (8 - j)
                grid[r][x] = on
    return grid


def preview(ch, grid):
    print(f"--- {ch} ---")
    for r in range(GLYPH_SIZE):
        print("".join("##" if grid[r][c] else "  " for c in range(GLYPH_SIZE)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true")
    args = ap.parse_args()

    font, font_path = load_font()
    print(f"[font] {font_path} @ {FONT_PX}px, threshold={THRESHOLD}")

    # 逐短语生成字节 + 往返格式自检
    phrase_bytes = {}
    n_fmt_fail = 0
    prev_chars = []
    for name, s in PHRASES.items():
        blob = bytearray()
        for ch in s:
            grid = glyph_bitmap(ch, font)
            gb = grid_to_bytes(grid)
            assert len(gb) == 32, f"{ch} 字模长度 != 32"
            # 往返格式自检:重新解码必须与源网格逐像素一致
            if decode_bytes(gb) != grid:
                print(f"[FORMAT-FAIL] 往返不一致: {ch}")
                n_fmt_fail += 1
            blob += gb
            if len(prev_chars) < 4:
                prev_chars.append((ch, grid))
        phrase_bytes[name] = (bytes(blob), len(s))

    if n_fmt_fail:
        raise SystemExit(f"字模格式往返自检失败 {n_fmt_fail} 处,已中止(不写文件)")
    print(f"[ok] {len(PHRASES)} 短语字模格式往返自检全部通过")

    if args.preview:
        for ch, grid in prev_chars:
            preview(ch, grid)

    # 唯一字统计(仅供参考)
    uniq = set("".join(PHRASES.values()))
    total_glyphs = sum(n for (_b, n) in phrase_bytes.values())
    print(f"[stat] 唯一字 {len(uniq)} 个, 短语总字数 {total_glyphs}, "
          f"字库 ~{total_glyphs*32} 字节")

    # ---- 生成 .h ----
    h = []
    h.append("/* 本文件由 tcp_tool/gen_selfcheck_font.py 自动生成 —— 请勿手改。 */")
    h.append("/* IPS200 16x16 中文点阵(阴码/逐行/顺向),供 menu_selfcheck.c 的 ips200_show_chinese 直接渲染。 */")
    h.append("#ifndef __SELFCHECK_FONT_CN_H__")
    h.append("#define __SELFCHECK_FONT_CN_H__")
    h.append("")
    h.append('#include "zf_common_typedef.h"')
    h.append("")
    for name, (blob, n) in phrase_bytes.items():
        s = PHRASES[name]
        h.append(f"extern const uint8 SC_CN_{name}[{len(blob)}];  /* {s} */")
        h.append(f"#define SC_CN_{name}_N  {n}u")
    h.append("")
    h.append("#endif /* __SELFCHECK_FONT_CN_H__ */")
    h.append("")
    with open(OUT_H, "w", encoding="utf-8") as f:
        f.write("\n".join(h))

    # ---- 生成 .c ----
    c = []
    c.append("/* 本文件由 tcp_tool/gen_selfcheck_font.py 自动生成 —— 请勿手改。 */")
    c.append('#include "selfcheck_font_cn.h"')
    c.append("")
    for name, (blob, n) in phrase_bytes.items():
        s = PHRASES[name]
        c.append(f"/* {s} ({n} 字) */")
        c.append(f"const uint8 SC_CN_{name}[{len(blob)}] =")
        c.append("{")
        # 每字 32 字节一行,便于核对
        for gi in range(n):
            g = blob[gi*32:(gi+1)*32]
            hexs = ", ".join(f"0x{b:02X}" for b in g)
            c.append(f"    {hexs},   /* {s[gi]} */")
        c.append("};")
        c.append("")
    with open(OUT_C, "w", encoding="utf-8") as f:
        f.write("\n".join(c))

    print(f"[write] {OUT_H}")
    print(f"[write] {OUT_C}")


if __name__ == "__main__":
    main()
