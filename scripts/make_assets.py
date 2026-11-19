"""生成商品与分类配图（本地 SVG，离线可用）。

为什么不用网络实拍图：演示环境常在内网/离线评审，外链图会裂；
而真实图库（维基共享资源、Unsplash、Pexels）在本机网络下分别遇到
TLS 握手失败与 429 限流，无法稳定批量拉取。因此这里用矢量插画统一出图：

- 每张 600x450、同构图的柔和渐变底 + 白色山丘 + 主体农产品，
  卡片、详情、分类导航复用同一套视觉，不会出现风格分裂；
- 文件都是纯 SVG（1~2KB），clone 即有图，不依赖 CDN，也不会因为
  商品改名而图不对文（图文件按 slug 命名，与种子数据一一对应）。

用法：python scripts/make_assets.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "app" / "web" / "static" / "img"

# 调色板：(背景起色, 背景止色, 主体主色, 主体暗色, 点缀色)
PALETTES: dict[str, tuple[str, str, str, str, str]] = {
    "citrus": ("#fff6e8", "#ffe0b8", "#f79021", "#d9740f", "#4f8f45"),
    "apple": ("#fdeef0", "#f9c9d1", "#d93a2b", "#b02a1d", "#4f8f45"),
    "grape": ("#f1f8e6", "#d5e9b8", "#a8c93a", "#7f9c25", "#4f8f45"),
    "berry": ("#ffedf0", "#f9c2cd", "#e2405a", "#b52b41", "#4f8f45"),
    "mango": ("#fff7dc", "#ffe08a", "#f2a03d", "#d17f22", "#4f8f45"),
    "kiwi": ("#f2f8e8", "#dcecc2", "#8ca83e", "#657c28", "#f5f2e6"),
    "tomato": ("#ffedeb", "#f8c2ba", "#d8352a", "#a92419", "#4f8f45"),
    "leafy": ("#eef8ec", "#cfe9c8", "#4e8f4a", "#376c34", "#e8f3e5"),
    "root": ("#f8f2e8", "#e6d8c1", "#c9a87c", "#a4835a", "#8a6b45"),
    "rice": ("#f8f5ea", "#e8e0c8", "#f6f1e2", "#d8cfb6", "#b9a87f"),
    "millet": ("#fbf3da", "#f0dfa8", "#e8b93b", "#c39623", "#8a6b45"),
    "egg": ("#fcf3e5", "#f0ddc0", "#f7ead1", "#dcc7a4", "#b58e5e"),
    "mushroom": ("#f5f0e7", "#e0d3bf", "#8b6647", "#664931", "#c8a887"),
    "walnut": ("#f7f0e4", "#e4d3b8", "#a97845", "#82592f", "#e3c9a2"),
    "bamboo": ("#f4f2e4", "#ddd7ba", "#b7a15c", "#8f7c3f", "#e6dcb6"),
    "tea": ("#eef7ed", "#cfe7ce", "#4c8b57", "#356541", "#e6f2e2"),
    "blacktea": ("#fbf0e6", "#edd2b5", "#8f3f2a", "#6b2a1b", "#e2b98e"),
    "other": ("#f6f2e9", "#e2dbc9", "#8f9b6f", "#6b7650", "#c9a87c"),
}


def scene(slug: str, palette: str, art: str, title: str) -> str:
    bg1, bg2, main, dark, accent = PALETTES[palette]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 450" width="600" height="450" role="img" aria-label="{title}">
  <title>{title}</title>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0.25" y2="1">
      <stop offset="0" stop-color="{bg1}"/>
      <stop offset="1" stop-color="{bg2}"/>
    </linearGradient>
    <radialGradient id="soft" cx="0.5" cy="0.4" r="0.7">
      <stop offset="0" stop-color="#ffffff" stop-opacity=".55"/>
      <stop offset="1" stop-color="#ffffff" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="600" height="450" fill="url(#bg)"/>
  <circle cx="470" cy="96" r="132" fill="#ffffff" opacity=".22"/>
  <circle cx="120" cy="360" r="96" fill="#ffffff" opacity=".16"/>
  <path d="M0 352 Q150 316 300 348 T600 336 L600 450 L0 450 Z" fill="#ffffff" opacity=".42"/>
  <ellipse cx="300" cy="332" rx="132" ry="20" fill="{dark}" opacity=".14"/>
  <g transform="translate(300 250)">{art}</g>
</svg>
"""


# --------------------------------------------------------------------- 单件农产品
def art_orange(main: str, dark: str, accent: str) -> str:
    return f"""<circle cx="0" cy="0" r="104" fill="{main}"/>
<circle cx="-34" cy="-34" r="30" fill="#ffffff" opacity=".18"/>
<path d="M6 -104 q10 -26 34 -32 q-2 26 -16 38 z" fill="{accent}"/>
<path d="M2 -100 q-6 -30 14 -46 q10 20 4 42 z" fill="{accent}" opacity=".85"/>
<circle cx="6" cy="-98" r="7" fill="{dark}"/>
<path d="M-98 6 a98 98 0 0 0 196 0" fill="none" stroke="{dark}" stroke-opacity=".18" stroke-width="6"/>"""


def art_apple(main: str, dark: str, accent: str) -> str:
    return f"""<path d="M0 -56 c-26 -40 -104 -34 -112 34 c-8 66 42 126 76 126 c16 0 26 -8 36 -8
c10 0 20 8 36 8 c34 0 84 -60 76 -126 c-8 -68 -86 -74 -112 -34 z" fill="{main}"/>
<path d="M0 -56 c-26 -40 -104 -34 -112 34 c-8 66 42 126 76 126 c10 0 18 -3 26 -6 z"
 fill="{dark}" opacity=".18"/>
<path d="M2 -60 q-8 -34 8 -58" stroke="{dark}" stroke-width="11" stroke-linecap="round" fill="none"/>
<path d="M14 -92 q46 -30 66 -4 q-30 34 -66 4 z" fill="{accent}"/>
<ellipse cx="-44" cy="10" rx="22" ry="34" fill="#ffffff" opacity=".18" transform="rotate(-18 -44 10)"/>"""


def art_grape(main: str, dark: str, accent: str) -> str:
    circles = []
    rows = [(-3, -96, 4), (-2, -66, 4), (-1, -36, 4), (0, -6, 3)]
    for _row, (_x, y, n) in enumerate(rows):
        for i in range(n):
            cx = (i - (n - 1) / 2) * 58
            circles.append(f'<circle cx="{cx:.0f}" cy="{y}" r="30" fill="{main}"/>')
            circles.append(
                f'<circle cx="{cx - 9:.0f}" cy="{y - 9}" r="8" fill="#ffffff" opacity=".28"/>'
            )
    return "".join(circles) + f"""<path d="M-14 -118 q-24 -30 -6 -56 q26 14 22 44 z" fill="{accent}"/>
<path d="M22 -120 q34 -14 46 10 q-26 16 -46 -10 z" fill="{accent}" opacity=".9"/>
<path d="M18 -110 q22 24 6 46" stroke="{dark}" stroke-width="5" fill="none" opacity=".5"/>"""


def art_strawberry(main: str, dark: str, accent: str) -> str:
    seeds = "".join(
        f'<ellipse cx="{x}" cy="{y}" rx="4" ry="6" fill="#ffe9a8" opacity=".9"/>'
        for x, y in [(-46, -34), (0, -46), (46, -34), (-58, 6), (-20, 0), (20, 2), (58, 8),
                     (-38, 44), (0, 34), (38, 46), (-14, 72), (16, 74)]
    )
    return f"""<path d="M0 -66 c58 -6 96 34 96 76 c0 62 -58 104 -96 118 c-38 -14 -96 -56 -96 -118
c0 -42 38 -82 96 -76 z" fill="{main}"/>
<path d="M-96 12 c0 62 58 104 96 118 v-196 c-58 -6 -96 36 -96 78 z" fill="{dark}" opacity=".16"/>
{seeds}
<path d="M-52 -70 q-14 -30 8 -44 q16 18 8 40 z" fill="{accent}"/>
<path d="M0 -72 q-4 -32 20 -40 q6 22 -6 40 z" fill="{accent}"/>
<path d="M50 -70 q16 -28 40 -22 q-6 26 -30 30 z" fill="{accent}"/>"""


def art_mango(main: str, dark: str, accent: str) -> str:
    return f"""<g transform="rotate(-16)">
<ellipse cx="0" cy="0" rx="112" ry="84" fill="{main}"/>
<ellipse cx="-42" cy="-26" rx="34" ry="24" fill="#ffe9a8" opacity=".45" transform="rotate(-18 -42 -26)"/>
<path d="M60 -66 q28 -22 52 -10 q-18 30 -52 10 z" fill="{accent}"/>
<path d="M66 -60 q-10 -20 6 -34" stroke="{dark}" stroke-width="6" fill="none" stroke-linecap="round"/>
</g>
<ellipse cx="0" cy="98" rx="86" ry="12" fill="{dark}" opacity=".12"/>"""


def art_kiwi(main: str, dark: str, accent: str) -> str:
    seeds = "".join(
        f'<ellipse cx="{x}" cy="{y}" rx="3.4" ry="6" fill="#3f4a1c" transform="rotate({a} {x} {y})"/>'
        for x, y, a in [(-34, -8, 80), (-24, -26, 55), (0, -34, 0), (24, -26, -55), (34, -8, -80),
                        (26, 20, -110), (0, 30, 0), (-26, 20, 110)]
    )
    return f"""<circle cx="0" cy="0" r="106" fill="#8d6a3f"/>
<circle cx="0" cy="0" r="96" fill="{main}"/>
<circle cx="0" cy="0" r="40" fill="{accent}" opacity=".92"/>
{seeds}
<circle cx="-40" cy="-40" r="26" fill="#ffffff" opacity=".18"/>
<g opacity=".9">{"".join(f'<circle cx="{x}" cy="{y}" r="4" fill="#f4f1e4"/>' for x, y in [(-58,5),(-46,-40),(0,-56),(46,-40),(58,5),(46,40),(0,56),(-46,40)])}</g>"""


def art_tomato(main: str, dark: str, accent: str) -> str:
    petals = "".join(
        f'<path d="M0 -16 L{a} -46 L{b} -18 Z" fill="{accent}"/>'
        for a, b in [(-4, -14), (-26, -8), (-28, 12), (-8, 22), (14, 20), (26, 4), (22, -16)]
    )
    return f"""<circle cx="0" cy="8" r="100" fill="{main}"/>
<path d="M-100 8 a100 100 0 0 0 200 0" fill="{dark}" opacity=".16"/>
<circle cx="-36" cy="-24" r="26" fill="#ffffff" opacity=".22"/>
{petals}
<path d="M0 -18 q-4 -26 12 -34" stroke="{accent}" stroke-width="8" stroke-linecap="round" fill="none"/>"""


def art_bok_choy(main: str, dark: str, accent: str) -> str:
    return f"""<path d="M-8 92 q-58 -30 -66 -92 q-4 -34 24 -36 q28 0 32 34 q8 58 10 94 z" fill="{main}"/>
<path d="M10 92 q60 -28 70 -90 q4 -32 -24 -36 q-28 -2 -34 32 q-8 58 -12 94 z" fill="{dark}"/>
<path d="M2 98 q-32 -28 -32 -72 q0 -32 30 -32 q32 0 32 32 q0 44 -30 72 z" fill="#7cb668"/>
<path d="M0 98 q-12 -38 -12 -70" stroke="#f4f9ec" stroke-width="8" opacity=".85" fill="none"/>
<path d="M-56 96 q-10 14 4 20 l104 0 q14 -6 4 -20 z" fill="#f7faf0"/>
<path d="M-56 96 q-10 14 4 20 l104 0 q14 -6 4 -20 z" fill="none" stroke="{dark}" stroke-opacity=".18" stroke-width="2"/>"""


def art_yam(main: str, dark: str, accent: str) -> str:
    sticks = []
    for _idx, (x, rot, h) in enumerate([(-46, -12, 104), (4, 0, 118), (52, 12, 100)]):
        sticks.append(
            f'<g transform="rotate({rot} {x} 10)">'
            f'<rect x="{x - 20}" y="{-h}" width="40" height="{h * 2}" rx="20" fill="{main}"/>'
            f'<rect x="{x - 20}" y="{-h}" width="14" height="{h * 2}" rx="10" fill="#ffffff" opacity=".22"/>'
            f'<g fill="{dark}" opacity=".45">'
            + "".join(
                f'<circle cx="{x - 6 + (k % 3) * 8}" cy="{-h + 30 + k * 26}" r="3"/>' for k in range(6)
            )
            + "</g></g>"
        )
    return "".join(sticks)


def art_rice(main: str, dark: str, accent: str) -> str:
    grains = "".join(
        f'<ellipse cx="{x}" cy="{y}" rx="7" ry="4" fill="#fffdf4" opacity=".95" stroke="#d8cfb6" stroke-width="1" transform="rotate({(x + y) % 40 - 20} {x} {y})"/>'
        for x, y in [(-30, -16), (-8, -24), (14, -20), (32, -8), (-36, 4), (-12, -2), (10, 2), (30, 8)]
    )
    return f"""<path d="M-108 8 h216 a108 92 0 0 1 -216 0 z" fill="#dfe6dc"/>
<path d="M-108 8 h216 a108 92 0 0 1 -216 0 z" fill="none" stroke="{dark}" stroke-width="3" opacity=".35"/>
<path d="M-90 -10 q90 -72 180 0 z" fill="{main}" stroke="{dark}" stroke-width="2.5" stroke-opacity=".28"/>
{grains}
<rect x="-116" y="0" width="232" height="14" rx="7" fill="#ffffff" opacity=".85"/>
<g stroke="{accent}" stroke-width="6" stroke-linecap="round" opacity=".95">
  <path d="M82 -46 l56 -40"/><path d="M90 -26 l60 -26"/>
</g>"""


def art_millet(main: str, dark: str, accent: str) -> str:
    grains = "".join(
        f'<circle cx="{x}" cy="{y}" r="7" fill="{main}"/><circle cx="{x - 2}" cy="{y - 2}" r="2.6" fill="#ffffff" opacity=".6"/>'
        for x, y in [(-38, -10), (-16, -20), (6, -14), (28, -18), (-30, 10), (-6, 4), (18, 8), (36, 2),
                     (-20, 26), (2, 30), (24, 26), (-42, 24)]
    )
    return f"""<path d="M-104 -4 h208 l-16 96 q-4 16 -20 16 h-136 q-16 0 -20 -16 z" fill="#efe6cf"/>
<path d="M-104 -4 h208 l-4 24 h-200 z" fill="{dark}" opacity=".18"/>
<ellipse cx="0" cy="2" rx="104" ry="18" fill="{main}" opacity=".25"/>
{grains}
<path d="M-60 -22 q18 -34 46 -40 q-4 30 -22 42 z" fill="#a8b96a"/>
<path d="M-2 -26 q16 -34 44 -40 q-2 30 -20 42 z" fill="#8fa653"/>"""


def art_eggs(main: str, dark: str, accent: str) -> str:
    eggs = ""
    for cx, cy, r in [(-46, 6, 1.0), (0, -18, 1.05), (48, 4, 0.98)]:
        eggs += (
            f'<ellipse cx="{cx}" cy="{cy}" rx="{44 * r:.0f}" ry="{56 * r:.0f}" fill="{main}" '
            f'stroke="{dark}" stroke-width="2.5" stroke-opacity=".3"/>'
            f'<ellipse cx="{cx - 12 * r:.0f}" cy="{cy - 16 * r:.0f}" rx="{13 * r:.0f}" ry="{17 * r:.0f}" fill="#ffffff" opacity=".7"/>'
        )
    return f"""<path d="M-118 62 q118 -40 236 0 l-16 48 q-102 20 -204 0 z" fill="{accent}"/>
<path d="M-118 62 q118 -40 236 0 l-16 48 q-102 20 -204 0 z" fill="none" stroke="{dark}" stroke-width="3" opacity=".35"/>
<path d="M-110 68 q110 -32 220 0" stroke="{dark}" stroke-width="6" fill="none" opacity=".35"/>
{eggs}
<g stroke="{dark}" stroke-width="4" opacity=".3" fill="none">
  <path d="M-88 30 q18 26 4 46"/><path d="M-26 40 q16 26 2 42"/><path d="M62 32 q18 26 4 44"/>
</g>"""


def art_mushroom(main: str, dark: str, accent: str) -> str:
    return f"""<g transform="translate(-38 18) scale(.86)">
  <path d="M-84 0 q6 -74 84 -74 q78 0 84 74 q-84 18 -168 0 z" fill="{main}"/>
  <path d="M-84 0 q6 -74 84 -74 v74 q-42 12 -84 0 z" fill="#ffffff" opacity=".12"/>
  <path d="M-16 0 h32 l10 78 q-26 12 -52 0 z" fill="{accent}"/>
  <g fill="{accent}" opacity=".55"><circle cx="-40" cy="-34" r="7"/><circle cx="10" cy="-48" r="6"/><circle cx="44" cy="-26" r="5"/></g>
</g>
<g transform="translate(64 -4) scale(.6)">
  <path d="M-84 0 q6 -74 84 -74 q78 0 84 74 q-84 18 -168 0 z" fill="{main}"/>
  <path d="M-16 0 h32 l10 78 q-26 12 -52 0 z" fill="{accent}"/>
</g>"""


def art_walnut(main: str, dark: str, accent: str) -> str:
    return f"""<g transform="translate(-46 4)">
  <circle cx="0" cy="0" r="76" fill="{accent}"/>
  <path d="M0 -76 a76 76 0 0 1 0 152 a38 76 0 0 1 0 -152 z" fill="{main}"/>
  <path d="M0 -70 v140 M-30 -60 q26 34 0 62 q-26 28 0 56" stroke="{dark}" stroke-width="5" fill="none" opacity=".45"/>
</g>
<g transform="translate(58 18) rotate(18)">
  <circle cx="0" cy="0" r="66" fill="{main}"/>
  <path d="M-46 -22 q46 -26 92 0 q-12 44 -46 48 q-34 -4 -46 -48 z" fill="#e6cda6"/>
  <path d="M-30 -14 q30 -14 60 0" stroke="{dark}" stroke-width="4" fill="none" opacity=".4"/>
</g>"""


def art_bamboo(main: str, dark: str, accent: str) -> str:
    def shoot(x: int, y: int, scale: float, opacity: float = 1.0) -> str:
        return (
            f'<g transform="translate({x} {y}) scale({scale})" opacity="{opacity}">'
            f'<path d="M0 -112 q42 42 28 108 q-12 56 -28 78 q-16 -22 -28 -78 q-14 -66 28 -108 z" fill="{main}"/>'
            f'<path d="M0 -112 q-14 44 -10 108 q4 42 10 78 z" fill="{dark}" opacity=".22"/>'
            f'<g stroke="{accent}" stroke-width="5" opacity=".75">'
            f'<path d="M-18 -58 q18 12 34 2"/><path d="M-16 -16 q16 12 30 2"/><path d="M-13 26 q14 12 26 2"/>'
            f"</g></g>"
        )

    return shoot(-46, 8, 1.0) + shoot(52, 34, 0.72, 0.95)


def art_tea(main: str, dark: str, accent: str) -> str:
    leaves = ""
    for cx, cy, rot in [(-52, -34, -24), (-10, -58, 6), (34, -40, 26), (-28, 10, -8), (18, 6, 18)]:
        leaves += (
            f'<g transform="translate({cx} {cy}) rotate({rot})">'
            f'<path d="M0 0 q28 -22 62 -6 q-30 30 -62 6 z" fill="{main}"/>'
            f'<path d="M0 0 q30 -8 58 -4" stroke="{dark}" stroke-width="3" opacity=".5" fill="none"/></g>'
        )
    return f"""{leaves}
<path d="M-92 52 h184 l-18 42 q-6 14 -22 14 h-104 q-16 0 -22 -14 z" fill="#fbf9f2"/>
<path d="M-92 52 h184 l-4 12 h-176 z" fill="{main}" opacity=".35"/>
<path d="M-118 46 q118 -22 236 0" stroke="#ffffff" stroke-width="7" fill="none" opacity=".8"/>
<path d="M24 30 q22 -34 4 -52" stroke="{dark}" stroke-width="4" fill="none" opacity=".4"/>"""


def art_basket(main: str, dark: str, accent: str) -> str:
    return f"""<g>
  <circle cx="-58" cy="-16" r="40" fill="#f79021"/>
  <circle cx="2" cy="-40" r="36" fill="#d93a2b"/>
  <circle cx="58" cy="-12" r="34" fill="#a8c93a"/>
  <circle cx="-12" cy="6" r="34" fill="#e8b93b"/>
  <path d="M-104 26 h208 l-18 74 q-6 20 -26 20 h-120 q-20 0 -26 -20 z" fill="{accent}"/>
  <g stroke="{dark}" stroke-width="4" opacity=".3" fill="none">
    <path d="M-80 34 l-10 76"/><path d="M-30 34 l-4 82"/><path d="M20 34 l4 82"/><path d="M72 34 l10 76"/>
    <path d="M-96 62 h192"/><path d="M-92 92 h184"/>
  </g>
</g>"""


ART: dict[str, tuple[str, str, str]] = {
    # slug: (调色板, 生成函数, 标题)
    "navel-orange": ("citrus", art_orange, "脐橙"),
    "fuji-apple": ("apple", art_apple, "红富士苹果"),
    "shine-muscat": ("grape", art_grape, "阳光玫瑰葡萄"),
    "strawberry": ("berry", art_strawberry, "红颜草莓"),
    "mango": ("mango", art_mango, "贵妃芒"),
    "kiwi": ("kiwi", art_kiwi, "徐香猕猴桃"),
    "tomato": ("tomato", art_tomato, "沙瓤番茄"),
    "bok-choy": ("leafy", art_bok_choy, "上海青"),
    "chinese-yam": ("root", art_yam, "铁棍山药"),
    "wuchang-rice": ("rice", art_rice, "稻花香大米"),
    "millet": ("millet", art_millet, "沁州黄小米"),
    "free-range-eggs": ("egg", art_eggs, "散养土鸡蛋"),
    "shiitake": ("mushroom", art_mushroom, "干香菇"),
    "walnut": ("walnut", art_walnut, "山核桃"),
    "bamboo-shoot": ("bamboo", art_bamboo, "笋干"),
    "green-tea": ("tea", art_tea, "绿茶"),
    "black-tea": ("blacktea", art_tea, "红茶"),
    "other": ("other", art_basket, "其他农产品"),
}

# 分类图直接借一组主图素材，保证分类导航与商品卡片风格一致
CATEGORY_ART: dict[str, str] = {
    "fruit": "navel-orange",
    "vegetable": "bok-choy",
    "grain": "wuchang-rice",
    "tea": "green-tea",
    "nuts": "walnut",
    "dried": "shiitake",
    "poultry": "free-range-eggs",
    "other": "other",
}


def main() -> None:
    product_dir = IMG / "products"
    category_dir = IMG / "categories"
    product_dir.mkdir(parents=True, exist_ok=True)
    category_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for slug, (palette, art_fn, title) in ART.items():
        path = product_dir / f"{slug}.svg"
        body = art_fn(*PALETTES[palette][2:5])
        path.write_text(scene(slug, palette, body, title), encoding="utf-8")
        written.append(str(path.relative_to(ROOT)))

    for code, source_slug in CATEGORY_ART.items():
        palette, art_fn, title = ART[source_slug]
        body = art_fn(*PALETTES[palette][2:5])
        path = category_dir / f"{code}.svg"
        path.write_text(scene(code, palette, body, title), encoding="utf-8")
        written.append(str(path.relative_to(ROOT)))

    print(f"生成 {len(written)} 个 SVG：")
    for item in written:
        print("  ", item)


if __name__ == "__main__":
    main()
