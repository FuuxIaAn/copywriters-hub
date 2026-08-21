# -*- coding: utf-8 -*-
"""生成「靓仔文案工作台」桌面图标 icon.ico（微信风格：绿底白对话气泡）。"""
import os
import sys

from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "icon.ico")


def draw_icon(size: int) -> Image.Image:
    """绘制 size x size 图标：微信绿圆角底 + 白色对话气泡 + 两个圆点。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    s = size
    # 背景圆角方块（微信绿）
    bg = (7, 193, 96, 255)
    r = int(s * 0.22)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, fill=bg)

    # 白色对话气泡（居中圆角矩形）
    bw, bh = int(s * 0.62), int(s * 0.46)
    bx0, by0 = (s - bw) // 2, int(s * 0.30)
    bx1, by1 = bx0 + bw, by0 + bh
    br = int(s * 0.12)
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=br, fill=(255, 255, 255, 255))

    # 气泡尾巴（左下小三角）
    tail = [
        (int(s * 0.44), by1),
        (int(s * 0.44), int(s * 0.84)),
        (int(s * 0.62), by1),
    ]
    d.polygon(tail, fill=(255, 255, 255, 255))

    # 气泡内两个圆点（眼睛，微信绿）
    dot_r = int(s * 0.05)
    cy = (by0 + by1) // 2
    for cx in (int(s * 0.42), int(s * 0.58)):
        d.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=bg)

    return img


def main() -> None:
    sizes = [256, 128, 64, 48, 32, 16]
    imgs = [draw_icon(sz) for sz in sizes]
    imgs[0].save(OUT, format="ICO", sizes=[(sz, sz) for sz in sizes])
    print(f"[icon] 已生成 {OUT}（尺寸: {sizes}）")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main()
