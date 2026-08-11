"""
charts/chart_generator.py
---------------------------
بند ۳: چارت‌ها باید بدون هرگونه اندیکاتور باشند - فقط کندل، نام نماد،
تایم‌فریم، قیمت، مقیاس قیمت و زمان آخرین کندل.
هیچ RSI/MACD/Moving Average یا خط اضافه‌ای رسم نمی‌شود مگر خطوط Watch
قبلی که صراحتاً در بند ۳ مجاز شمرده شده‌اند.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import mplfinance as mpf
import pandas as pd

from config import CHART_TMP_DIR


def _candles_to_dataframe(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time")
    df = df.rename(columns={
        "open": "Open", "high": "High", "low": "Low", "close": "Close",
    })
    if "volume" not in df.columns:
        df["volume"] = 0
    df = df.rename(columns={"volume": "Volume"})
    return df[["Open", "High", "Low", "Close", "Volume"]]


def generate_clean_chart(
    symbol: str,
    timeframe_label: str,
    candles: list[dict],
    watch_zone: tuple[float, float] | None = None,
    watch_level: float | None = None,
) -> Path:
    """
    یک تصویر PNG تولید می‌کند که فقط شامل کندل‌ها، نام نماد، تایم‌فریم،
    قیمت/مقیاس قیمت و زمان آخرین کندل است.
    watch_zone / watch_level: طبق بند ۳، تنها خطوط مجاز اضافه (محدوده/سطح
    Watch تعریف‌شده در تحلیل قبلی) که در صورت وجود روی چارت رسم می‌شود.
    """
    df = _candles_to_dataframe(candles)
    last_candle_time = df.index[-1]

    hlines = None
    if watch_zone is not None:
        hlines = dict(hlines=list(watch_zone), colors=["#888888", "#888888"],
                      linestyle="--", linewidths=0.8)
    elif watch_level is not None:
        hlines = dict(hlines=[watch_level], colors=["#888888"], linestyle="--", linewidths=0.8)

    out_path = CHART_TMP_DIR / f"{symbol}_{timeframe_label}_{uuid.uuid4().hex[:8]}.png"

    market_colors = mpf.make_marketcolors(up="#26a69a", down="#ef5350", inherit=True)
    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        marketcolors=market_colors,
        gridstyle="",
    )

    title = f"{symbol}  |  {timeframe_label}  |  Last candle: {last_candle_time:%Y-%m-%d %H:%M UTC}"

    # عرض تصویر متناسب با تعداد کندل - وگرنه با تعداد بالا (مثلاً ۲۰۰+)
    # کندل‌ها آن‌قدر فشرده می‌شوند که هوش مصنوعی نمی‌تواند شکلشان را تشخیص
    # دهد. سقف ۲۴ اینچ برای جلوگیری از رشد بی‌رویه هزینه پردازش تصویر
    # (OpenAI بر اساس اندازه تصویر هم هزینه می‌گیرد) در نظر گرفته شده است.
    candle_count = len(candles)
    chart_width = min(24.0, max(10.0, candle_count * 0.045))

    plot_kwargs = dict(
        type="candle",
        style=style,
        title=title,
        ylabel="Price",
        volume=False,           # طبق بند ۳ فقط کندل - حجم هم اندیکاتور اضافه محسوب می‌شود
        savefig=dict(fname=str(out_path), dpi=150, bbox_inches="tight"),
        figsize=(chart_width, 6),
        tight_layout=True,
    )
    if hlines is not None:      # فقط وقتی خط Watch قبلی واقعاً وجود دارد اضافه شود (بند ۳)
        plot_kwargs["hlines"] = hlines

    mpf.plot(df, **plot_kwargs)
    return out_path


def generate_required_charts(
    symbol: str,
    snapshot_candles: dict[str, list[dict]],
    include_correlated: bool,
    correlated_candles: dict[str, dict[str, list[dict]]] | None = None,
    watch_zone: tuple[float, float] | None = None,
    watch_level: float | None = None,
) -> list[Path]:
    """
    بند ۳: برای نماد اصلی M5/M15/H1، و در صورت نیاز DXY/USDJPY نیز اضافه می‌شود.
    snapshot_candles: {"M5": [...], "M15": [...], "H1": [...]}
    correlated_candles: {"DXY": {"M5":..., "M15":..., "H1":...}, "USDJPY": {...}}
    """
    paths: list[Path] = []
    for tf_label in ("M5", "M15", "H1"):
        candles = snapshot_candles.get(tf_label)
        if not candles:
            continue
        paths.append(
            generate_clean_chart(symbol, tf_label, candles, watch_zone=watch_zone, watch_level=watch_level)
        )

    if include_correlated and correlated_candles:
        for corr_symbol, tf_dict in correlated_candles.items():
            for tf_label, candles in tf_dict.items():
                if candles:
                    paths.append(generate_clean_chart(corr_symbol, tf_label, candles))

    return paths
