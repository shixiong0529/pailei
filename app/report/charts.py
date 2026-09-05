"""内联 SVG 图表生成。

报告要求离线可读（方案 §8）：不依赖外部字体、脚本或图片服务，
因此图表一律以内联 SVG 渲染，数值由程序计算。
"""

from __future__ import annotations

from typing import Optional

WIDTH = 660
HEIGHT = 240
PAD_L, PAD_R, PAD_T, PAD_B = 62, 18, 26, 46

# 中国股市惯例：涨红跌绿。此处用于“较上期变动”的着色。
UP_COLOR = "#dc2626"
DOWN_COLOR = "#16a34a"
NEUTRAL = "#2563eb"


def _fmt_value(value: float) -> str:
    """按量级选择合适的单位。"""
    abs_v = abs(value)
    if abs_v >= 1e12:
        return f"{value / 1e12:,.2f} 万亿"
    if abs_v >= 1e8:
        return f"{value / 1e8:,.2f} 亿"
    if abs_v >= 1e4:
        return f"{value / 1e4:,.1f} 万"
    return f"{value:,.2f}"


def _nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    step = (hi - lo) / count
    if step <= 0:
        return [lo]
    magnitude = 10 ** (int(len(str(int(step)))) - 1) if step >= 1 else 1
    import math
    magnitude = 10 ** math.floor(math.log10(step)) if step > 0 else 1
    step = math.ceil(step / magnitude) * magnitude
    start = math.floor(lo / magnitude) * magnitude
    ticks = []
    v = start
    while v <= hi + step * 0.5 and len(ticks) < 12:
        ticks.append(v)
        v += step
    return ticks


def trend_chart(
    series: list[dict],
    title: str,
    *,
    color: str = NEUTRAL,
    compare_previous: bool = True,
) -> str:
    """生成柱状 + 趋势折线图。series 为 [{"label","value","period"}]，按时间正序传入。"""
    import math

    points = [p for p in series if p.get("value") is not None]
    if not points:
        return (
            f'<div class="chart-empty">{_esc(title)}：无可用数据（该科目在已获取资料中缺失）</div>'
        )

    values = [float(p["value"]) for p in points]
    lo, hi = min(values + [0.0]), max(values + [0.0])
    span = hi - lo or 1.0
    lo -= span * 0.12
    hi += span * 0.12
    plot_w = WIDTH - PAD_L - PAD_R
    plot_h = HEIGHT - PAD_T - PAD_B

    def x_of(i: int) -> float:
        if len(points) == 1:
            return PAD_L + plot_w / 2
        return PAD_L + plot_w * i / (len(points) - 1)

    def y_of(v: float) -> float:
        return PAD_T + plot_h * (1 - (v - lo) / (hi - lo))

    ticks = _nice_ticks(lo, hi)
    parts = [
        f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
        f'aria-label="{_esc(title)}趋势图" xmlns="http://www.w3.org/2000/svg">',
        f'<text x="{PAD_L}" y="16" class="chart-title">{_esc(title)}</text>',
    ]
    # 网格与刻度
    for t in ticks:
        y = y_of(t)
        parts.append(
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{WIDTH - PAD_R}" y2="{y:.1f}" '
            f'stroke="#e5e7eb" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{PAD_L - 8}" y="{y + 4:.1f}" class="chart-tick" text-anchor="end">'
            f'{_esc(_fmt_value(t))}</text>'
        )
    # 零轴
    if lo < 0 < hi:
        parts.append(
            f'<line x1="{PAD_L}" y1="{y_of(0):.1f}" x2="{WIDTH - PAD_R}" y2="{y_of(0):.1f}" '
            f'stroke="#94a3b8" stroke-width="1.5" stroke-dasharray="4 3"/>'
        )

    bar_w = min(46, max(10, plot_w / max(len(points), 1) * 0.5))
    for i, p in enumerate(points):
        v = float(p["value"])
        x = x_of(i)
        y = y_of(v)
        fill = color
        if compare_previous and i > 0:
            prev = float(points[i - 1]["value"])
            fill = UP_COLOR if v > prev else (DOWN_COLOR if v < prev else color)
        top = y if v >= 0 else y_of(0)
        height = abs(y_of(0) - y)
        parts.append(
            f'<rect x="{x - bar_w / 2:.1f}" y="{top:.1f}" width="{bar_w:.1f}" '
            f'height="{max(height, 1.5):.1f}" fill="{fill}" opacity="0.82" rx="2"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{(top - 6) if v >= 0 else (top + height + 14):.1f}" '
            f'class="chart-value" text-anchor="middle">{_esc(_fmt_value(v))}</text>'
        )
        label = str(p.get("label") or p.get("period") or "")
        parts.append(
            f'<text x="{x:.1f}" y="{HEIGHT - PAD_B + 20:.1f}" class="chart-label" '
            f'text-anchor="middle">{_esc(label)}</text>'
        )

    polyline = " ".join(f"{x_of(i):.1f},{y_of(float(p['value'])):.1f}" for i, p in enumerate(points))
    if len(points) > 1:
        parts.append(
            f'<polyline points="{polyline}" fill="none" stroke="{color}" '
            f'stroke-width="2" stroke-linejoin="round"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def coverage_donut(applicable: int, evaluated: int, insufficient: int) -> str:
    """覆盖度环形图。"""
    total = max(applicable + evaluated, 1)
    done = min(evaluated, total)
    radius, cx, cy, stroke = 52, 80, 80, 20
    circumference = 2 * 3.14159265 * radius
    filled = circumference * (done / total) if total else 0
    pct = round(done / total * 100) if total else 0
    return (
        f'<svg class="donut" viewBox="0 0 160 160" xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="检查覆盖度 {pct}%">'
        f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="#e5e7eb" stroke-width="{stroke}"/>'
        f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="{NEUTRAL}" stroke-width="{stroke}" '
        f'stroke-dasharray="{filled:.1f} {circumference:.1f}" transform="rotate(-90 {cx} {cy})" '
        f'stroke-linecap="round"/>'
        f'<text x="{cx}" y="{cy + 6}" text-anchor="middle" class="donut-text">{pct}%</text>'
        f'<text x="{cx}" y="{cy + 26}" text-anchor="middle" class="donut-sub">'
        f'{evaluated}/{applicable}</text>'
        f"</svg>"
    )


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )
