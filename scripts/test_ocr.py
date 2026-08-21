# -*- coding: utf-8 -*-
"""OCR 测试：生成一张带中文的模拟截图，验证识别效果"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from knowledge_loader import _read_image  # noqa: E402

# 找 Windows 中文字体
FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",   # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf", # 黑体
    "C:/Windows/Fonts/simsun.ttc", # 宋体
]
font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
if not font_path:
    print("未找到中文字体，跳过测试")
    sys.exit(1)

# 生成一张模拟截图（白底黑字，像文章截图）
lines = [
    "口播文案写作技巧总结",
    "",
    "1. 开头3秒抓住注意力，用痛点或反常识开场",
    "2. 中间用具体场景和画面感代替参数堆砌",
    "3. 结尾要有明确的行动号召，制造紧迫感",
    "4. 口语化表达，短句为主，节奏明快",
    "记住：卖产品先卖那个更想成为的自己",
]
img = Image.new("RGB", (640, 400), "white")
draw = ImageDraw.Draw(img)
font = ImageFont.truetype(font_path, 28)
y = 30
for line in lines:
    draw.text((30, y), line, fill="black", font=font)
    y += 52
test_png = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ocr_test.png")
img.save(test_png)

print("已生成测试截图:", test_png)
text = _read_image(test_png)
print("=== OCR 识别结果 ===")
print(text or "(未识别到文字)")

os.remove(test_png)
