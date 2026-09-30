#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 PWA 图标 —— 铜线圈造型 192x192 + 512x512 + 64 favicon

造型说明：三圈同心椭圆 + 一道引出线，抽象表达「绕好的铜线盘」。
不用位图素材：矢量绘制无版权/水印问题，体积也只有几 KB。
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 铜色渐变的两端
CU_LIGHT = (240, 176, 114)
CU_MID = (194, 112, 61)
CU_DEEP = (122, 63, 24)
BG_DEEP = (10, 15, 30)


def mix(c1, c2, t):
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c1, c2))


def draw_copper_coil(size):
    """在透明背景上画「铜线圈 + 底色圆」图标，返回 RGBA Image。"""
    SS = 4  # 超采样倍数，画完再缩回来，得到平滑边缘（PIL 本身不做抗锯齿）
    S = size * SS
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    cx = cy = S / 2
    r_bg = S * 0.44

    # ---- 底圆：深底 + 中心铜色泛光（用同心圆近似径向渐变）----
    steps = 60
    for i in range(steps, 0, -1):
        r = r_bg * i / steps
        t = i / steps                     # 1 = 最外圈
        # 中心偏铜色、边缘回到深底
        col = mix(CU_DEEP, BG_DEEP, min(1.0, t * 1.35))
        alpha = 255
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col + (alpha,))

    # ---- 线圈：三圈同心椭圆，从粗到细、从亮到暗 ----
    rings = [
        (0.70, 0.30, 0.075, CU_LIGHT),
        (0.52, 0.225, 0.062, CU_MID),
        (0.34, 0.148, 0.050, CU_LIGHT),
    ]
    for rx_f, ry_f, w_f, base_col in rings:
        rx, ry = r_bg * rx_f, r_bg * ry_f
        w = max(1, int(r_bg * w_f))
        # 每圈拆成小段画，让颜色沿椭圆缓慢由亮到暗（模拟金属受光）
        seg = 72
        for k in range(seg):
            a0 = 2 * math.pi * k / seg
            a1 = 2 * math.pi * (k + 1) / seg
            p0 = (cx + rx * math.cos(a0), cy + ry * math.sin(a0))
            p1 = (cx + rx * math.cos(a1), cy + ry * math.sin(a1))
            # 左上受光、右下背光
            lum = 0.5 + 0.5 * math.cos(a0 + math.pi * 0.75)
            col = mix(CU_DEEP, base_col, lum)
            d.line([p0, p1], fill=col + (255,), width=w)
            # 圆头收尾，避免段与段之间出现缺口
            rr = w / 2
            d.ellipse([p1[0] - rr, p1[1] - rr, p1[0] + rr, p1[1] + rr], fill=col + (255,))

    # ---- 引出线：从最外圈向右下引出一段，暗示「这是线不是环」----
    lw = max(1, int(r_bg * 0.055))
    x0 = cx + r_bg * 0.70 * math.cos(math.pi * 0.25)
    y0 = cy + r_bg * 0.30 * math.sin(math.pi * 0.25)
    x1 = cx + r_bg * 1.02
    y1 = cy + r_bg * 0.60
    d.line([(x0, y0), (x1, y1)], fill=CU_LIGHT + (255,), width=lw)
    for p in [(x0, y0), (x1, y1)]:
        rr = lw / 2
        d.ellipse([p[0] - rr, p[1] - rr, p[0] + rr, p[1] + rr], fill=CU_LIGHT + (255,))

    return img.resize((size, size), Image.LANCZOS)


def main():
    try:
        for size in (192, 512):
            img = draw_copper_coil(size)
            out = PROJECT_ROOT / f'icon-{size}x{size}.png'
            img.save(out, 'PNG')
            print(f'  [OK] {out.name} ({size}x{size}, {out.stat().st_size // 1024} KB)')

        fav = draw_copper_coil(64)
        fav_path = PROJECT_ROOT / 'favicon.png'
        fav.save(fav_path, 'PNG')
        print(f'  [OK] favicon.png (64x64, {fav_path.stat().st_size // 1024} KB)')
        print('  [DONE] 图标生成完成')
    except Exception as e:
        print(f'  [FAIL] 生成失败: {e}')
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    main()
