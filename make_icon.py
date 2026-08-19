#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
创建 bell.ico 图标文件
"""
from PIL import Image, ImageDraw, ImageFont

# 创建一个 64x64 的图标
img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# 画一个橙色圆形背景
draw.ellipse([4, 4, 60, 60], fill="#FF9800")

# 画一个铃铛形状 (简化版)
draw.ellipse([18, 10, 46, 38], fill="white")
draw.rectangle([22, 38, 42, 46], fill="white")
draw.ellipse([26, 44, 38, 52], fill="white")
draw.rectangle([30, 38, 34, 46], fill="#FF9800")

# 保存为 ICO
img.save("bell.ico", format="ICO", sizes=[(64, 64)])
print("bell.ico 已创建")