"""生成 EchoDesk 应用图标（.png 预览 + 六尺寸 .ico）。

设计说明（呼应任务书"体现『回声』与『本地工具』的结合"）：
  * 外形：圆角方形"工具砖"，表达"本地工具"的容器感；
  * 主体：左侧一枚实心声源，向右扩散三道同心回声弧 —— "回声"的直观符号；
  * 主色：深靛蓝 → 深紫的对角渐变背景（#1B1B4D → #4A1E6E）；
  * 点缀：暖陶土色（#C8674A / #D9835F / #E7A183）声源与回声弧，形成冷暖对比；
  * 风格：扁平矢量，无文字、无水印、无阴影，粗线条保证 16px 下仍可辨识。

输出：
  assets/EchoDesk.png   256×256 预览图
  assets/EchoDesk.ico   含 16/32/48/64/128/256 六种尺寸

用法：python tools/make_icon.py
"""
import math
import os

from PIL import Image, ImageDraw

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")
SIZE = 256
SS = 4                      # 超采样倍数（先大后小，得到平滑边缘）
ICO_SIZES = [16, 32, 48, 64, 128, 256]

# ---------- 配色 ----------
INDIGO = (0x1B, 0x1B, 0x4D)     # 深靛蓝（渐变起点，左上）
PURPLE = (0x4A, 0x1E, 0x6E)     # 深紫（渐变终点，右下）
TERRACOTTA_CORE = (0xD9, 0x83, 0x5F)   # 暖陶土：声源
TERRACOTTA_1 = (0xC8, 0x67, 0x4A)      # 暖陶土：第一道回声（最深）
TERRACOTTA_2 = (0xD9, 0x83, 0x5F)      # 暖陶土：第二道回声
TERRACOTTA_3 = (0xE7, 0xA1, 0x83)      # 暖陶土：第三道回声（最浅）

# ---------- 几何（基于 256 网格，绘制时乘超采样倍数） ----------
TILE_MARGIN = 6
TILE_RADIUS = 52
SOURCE_CENTER = (88, 128)
SOURCE_RADIUS = 21
# (半径, 线宽, 颜色) —— 由内到外，颜色渐浅表达回声衰减
ARCS = [
    (47, 17, TERRACOTTA_1),
    (74, 15, TERRACOTTA_2),
    (100, 13, TERRACOTTA_3),
]
ARC_SPREAD = 56.0           # 单侧张角（度），总张角 = 2 × ARC_SPREAD


def _scale(value: float) -> float:
    return value * SS


def _diagonal_gradient() -> Image.Image:
    """深靛蓝 → 深紫的对角线性渐变。"""
    gradient = Image.new("RGBA", (SIZE, SIZE))
    pixels = gradient.load()
    for y in range(SIZE):
        for x in range(SIZE):
            t = (x + y) / (2.0 * (SIZE - 1))
            pixels[x, y] = (
                round(INDIGO[0] + (PURPLE[0] - INDIGO[0]) * t),
                round(INDIGO[1] + (PURPLE[1] - INDIGO[1]) * t),
                round(INDIGO[2] + (PURPLE[2] - INDIGO[2]) * t),
                255,
            )
    return gradient


def _rounded_tile_mask() -> Image.Image:
    """圆角方形遮罩（L 模式，255=不透明）。"""
    mask = Image.new("L", (SIZE * SS, SIZE * SS), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        [
            _scale(TILE_MARGIN), _scale(TILE_MARGIN),
            _scale(SIZE - TILE_MARGIN), _scale(SIZE - TILE_MARGIN),
        ],
        radius=_scale(TILE_RADIUS),
        fill=255,
    )
    return mask


def _draw_echo(draw: ImageDraw.ImageDraw) -> None:
    """绘制声源与三道向右扩散的回声弧（弧端补圆点做出圆头效果）。"""
    cx, cy = SOURCE_CENTER

    # 由外向内绘制，保证内层颜色覆盖外层接缝
    for radius, width, color in reversed(ARCS):
        start, end = -ARC_SPREAD, ARC_SPREAD
        box = [
            _scale(cx - radius), _scale(cy - radius),
            _scale(cx + radius), _scale(cy + radius),
        ]
        draw.arc(box, start=start, end=end, fill=color, width=int(_scale(width)))
        # 圆弧两端补实心圆点 → 视觉上的圆头线帽
        cap = _scale(width) / 2.0
        for angle in (start, end):
            rad = math.radians(angle)
            px = cx + radius * math.cos(rad)
            py = cy + radius * math.sin(rad)
            draw.ellipse(
                [_scale(px) - cap, _scale(py) - cap, _scale(px) + cap, _scale(py) + cap],
                fill=color,
            )

    # 声源：实心圆
    r = SOURCE_RADIUS
    draw.ellipse(
        [
            _scale(cx - r), _scale(cy - r),
            _scale(cx + r), _scale(cy + r),
        ],
        fill=TERRACOTTA_CORE,
    )


def build_master() -> Image.Image:
    """在超采样画布上合成完整图标，再缩小到 256×256。"""
    canvas = Image.new("RGBA", (SIZE * SS, SIZE * SS), (0, 0, 0, 0))

    # 背景：渐变填满画布，再用圆角遮罩裁剪
    gradient = _diagonal_gradient().resize((SIZE * SS, SIZE * SS), Image.NEAREST)
    canvas.paste(gradient, (0, 0), _rounded_tile_mask())

    _draw_echo(ImageDraw.Draw(canvas))
    return canvas.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    master = build_master()

    png_path = os.path.join(OUT_DIR, "EchoDesk.png")
    master.save(png_path)

    ico_path = os.path.join(OUT_DIR, "EchoDesk.ico")
    master.save(ico_path, format="ICO", sizes=[(s, s) for s in ICO_SIZES])

    print(f"已生成 {os.path.normpath(png_path)}")
    print(f"已生成 {os.path.normpath(ico_path)}（尺寸 {ICO_SIZES}）")


if __name__ == "__main__":
    main()
