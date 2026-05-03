"""Server-side PNG renderer for macro release [📊 Tarihsel kıyaslama].

Rendered as a small dark-mode line chart (matplotlib + Agg backend, no GUI)
and uploaded to Telegram via sendPhoto. The same data is also returned as a
text caption so the message remains useful if the user has images disabled.

Two chart variants — same logic as the dashboard's <HistoryChart>:
- PCT events (CPI / PCE / CORE_*) → Y axis = MoM% with '+0.87%' formatter
- NFP                              → Y axis = change_k with '+178K' formatter

Returns (png_bytes, caption_text). Either may be None on failure; callers
fall back to a plain-text reply when png_bytes is None.
"""
from __future__ import annotations

import html
import io
from datetime import datetime
from typing import Optional

# Headless backend MUST be set before pyplot import — Railway has no display.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from core.logger import get_logger

logger = get_logger("macro.chart")


_PCT_EVENTS = frozenset({"CPI", "PCE", "CORE_CPI", "CORE_PCE"})

_TR_MONTHS = [
    "", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
]

# Dark theme tuned to match the Telegram dark UI most users see.
_BG = "#0f1419"
_FG = "#e6e8eb"
_GRID = "#2a2f36"
_LINE = "#4ea1ff"
_FILL = "#4ea1ff22"


def _fmt_period(dt: datetime) -> str:
    try:
        return f"{_TR_MONTHS[dt.month]} {dt.year}"
    except Exception:
        return dt.strftime("%Y-%m") if dt else "?"


def _compute_points(et: str, rows: list) -> tuple[list, list]:
    """Return (x_labels, y_values) for the chart. Filters rows where the
    relevant metric (mom_pct or change_k) is None — chart needs at least 2
    points to draw a line.
    """
    is_nfp = et == "NFP"
    is_pct = et in _PCT_EVENTS
    xs: list = []
    ys: list = []
    for r in rows:
        released = r.get("released_at")
        if isinstance(released, str):
            try:
                released = datetime.fromisoformat(released.replace("Z", "+00:00"))
            except Exception:
                continue
        if released is None:
            continue
        if is_nfp:
            actual = r.get("actual_value")
            prior = r.get("prior_value")
            if actual is None or prior is None:
                continue
            try:
                v = float(actual) - float(prior)
            except (TypeError, ValueError):
                continue
        elif is_pct:
            actual = r.get("actual_value")
            prior = r.get("prior_value")
            if actual is None or prior is None:
                continue
            try:
                a = float(actual)
                p = float(prior)
            except (TypeError, ValueError):
                continue
            if p == 0:
                continue
            v = (a - p) / abs(p) * 100.0
        else:
            continue
        xs.append(released)
        ys.append(v)
    return xs, ys


def _build_caption(et: str, xs: list, ys: list) -> str:
    """Two-line summary placed under the chart image as Telegram caption."""
    if not xs or not ys:
        return f"📊 {html.escape(et)} — tarihsel veri yok."
    is_nfp = et == "NFP"
    last_period = _fmt_period(xs[-1])
    last = ys[-1]
    if is_nfp:
        sign = "+" if last >= 0 else ""
        latest_str = f"{sign}{last:.0f}K"
        unit = "Aylık değişim (K)"
    else:
        sign = "+" if last >= 0 else ""
        latest_str = f"{sign}{last:.2f}%"
        unit = "Aylık değişim (MoM%)"
    avg = sum(ys) / len(ys)
    avg_str = (
        f"{('+' if avg >= 0 else '')}{avg:.0f}K" if is_nfp
        else f"{('+' if avg >= 0 else '')}{avg:.2f}%"
    )
    return (
        f"📊 <b>{html.escape(et)} — son {len(xs)} ay</b>\n"
        f"{html.escape(last_period)}: <b>{latest_str}</b> · "
        f"{len(xs)}-ay ort. {avg_str}\n"
        f"<i>{html.escape(unit)}</i>"
    )


def render_history_chart(event_type: str, rows: list) -> tuple[Optional[bytes], str]:
    """Render `rows` (oldest first) as a dark-mode line chart PNG.

    `rows` shape matches what macro_callback._hist_payload reads from
    macro_releases (released_at + actual_value + prior_value). We compute
    MoM% / change_k inline so this helper has a single clear input contract.

    Returns (png_bytes_or_None, html_caption). On any matplotlib failure we
    return (None, caption) so the caller can still reply with text.
    """
    et = (event_type or "").upper()
    xs, ys = _compute_points(et, rows)
    caption = _build_caption(et, xs, ys)
    if len(xs) < 2:
        return None, caption

    is_nfp = et == "NFP"

    try:
        fig, ax = plt.subplots(figsize=(7.5, 3.6), dpi=130)
        fig.patch.set_facecolor(_BG)
        ax.set_facecolor(_BG)

        ax.plot(xs, ys, color=_LINE, linewidth=2.2, marker="o", markersize=4.5)
        ax.fill_between(xs, ys, 0, color=_FILL)
        ax.axhline(0, color=_GRID, linewidth=0.8)

        ax.tick_params(colors=_FG, labelsize=9)
        for spine in ax.spines.values():
            spine.set_color(_GRID)
        ax.grid(True, color=_GRID, alpha=0.35, linewidth=0.6)

        if is_nfp:
            ax.yaxis.set_major_formatter(
                FuncFormatter(lambda v, _: f"{('+' if v >= 0 else '')}{v:.0f}K")
            )
            unit_label = "Aylık değişim (K)"
        else:
            ax.yaxis.set_major_formatter(
                FuncFormatter(lambda v, _: f"{('+' if v >= 0 else '')}{v:.2f}%")
            )
            unit_label = "Aylık değişim (MoM%)"

        # Show every other x label so 14-month series stays readable.
        x_labels = [_fmt_period(d) for d in xs]
        stride = max(1, len(x_labels) // 6)
        ax.set_xticks(xs[::stride])
        ax.set_xticklabels(x_labels[::stride], rotation=20, ha="right")

        ax.set_title(f"{et} — son {len(xs)} ay", color=_FG, fontsize=11, loc="left", pad=8)
        ax.set_ylabel(unit_label, color=_FG, fontsize=9)

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=_BG, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue(), caption
    except Exception as e:
        logger.warning(f"chart render failed for {et}: {e}")
        try:
            plt.close("all")
        except Exception:
            pass
        return None, caption
