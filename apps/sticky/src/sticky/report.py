"""Single-file HTML stats report.

Reads the local DB (populated by `sticky sync`) and emits a static HTML
file with sticker thumbnails inlined as base64 data URIs so it works
fully offline.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Template
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import DynamicPack, Pack, StickerUsage, SyncState, UnresolvedSend
from .rank import Window, graveyard, top_by_window


# Ranked cache-filename suffix prefixes. Lower index = preferred.
# Every `*-sticker-v*` variant is actually a PNG preview despite the
# "animated" label — only `first-frame` / `scaled-frame-*` are JPEGs.
_THUMB_RANK: tuple[tuple[str, str], ...] = (
    ("sticker-v3-png-120x120", "image/png"),
    ("sticker-v1-png-120x120", "image/png"),
    ("sticker-v3-png", "image/png"),
    ("sticker-v1-png", "image/png"),
    ("1animated-sticker-v18-", "image/png"),
    ("1animated-sticker-v17-", "image/png"),
    ("animated-sticker-v17-", "image/png"),
    ("1animated-sticker-v8-", "image/png"),
    ("animated-sticker-v8-", "image/png"),
    ("1animated-sticker-v1-", "image/png"),
    ("animated-sticker-v1-", "image/png"),
    ("first-frame", "image/jpeg"),
    ("scaled-frame-160x160", "image/jpeg"),
)


def _build_thumb_index(cache_dir: Path) -> dict[int, tuple[Path, str]]:
    """One-pass scan of postbox media cache keyed by document id.

    The cache has ~200k files; doing per-sticker `glob()` calls was O(N*M).
    This builds a {file_id -> (path, mime)} dict in a single directory walk
    and picks the highest-ranked variant per id.
    """
    best: dict[int, tuple[int, Path, str]] = {}
    try:
        it = os.scandir(cache_dir)
    except OSError:
        return {}
    with it:
        for entry in it:
            name = entry.name
            if not name.startswith("telegram-cloud-document"):
                continue
            if ":" not in name:
                continue
            head, suffix = name.split(":", 1)
            fid: int | None = None
            for part in reversed(head.split("-")):
                if part.isdigit() and len(part) >= 10:
                    fid = int(part)
                    break
            if fid is None:
                continue
            rank: int | None = None
            mime: str | None = None
            for i, (prefix, mt) in enumerate(_THUMB_RANK):
                if suffix.startswith(prefix):
                    rank = i
                    mime = mt
                    break
            if rank is None or mime is None:
                continue
            cur = best.get(fid)
            if cur is None or rank < cur[0]:
                best[fid] = (rank, Path(entry.path), mime)
    return {fid: (path, mime) for fid, (_, path, mime) in best.items()}


def _thumb_for_id(
    file_id: int, index: dict[int, tuple[Path, str]]
) -> tuple[Path, str] | None:
    return index.get(file_id)


@dataclass
class MediaBody:
    path: Path
    kind: str  # 'webm' | 'tgs' | 'webp'


def _sniff_kind(head: bytes) -> str | None:
    if head.startswith(b"\x1a\x45\xdf\xa3"):  # EBML
        return "webm"
    if head.startswith(b"\x1f\x8b"):  # gzip → TGS
        return "tgs"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    return None


def _scan_media_dir(directory: Path, accept: tuple[str, ...]) -> dict[int, MediaBody]:
    """Walk a flat media directory, return {file_id: MediaBody} for accepted kinds.

    Handles both Postbox's `telegram-cloud-document-<ns>-<fid>` flat files
    and our fetch dir's plain integer filenames.
    """
    found: dict[int, MediaBody] = {}
    try:
        it = os.scandir(directory)
    except OSError:
        return {}
    with it:
        for entry in it:
            if not entry.is_file():
                continue
            name = entry.name
            fid: int | None = None
            if name.startswith("telegram-cloud-document-"):
                if "-size-" in name or "_partial" in name or name.endswith(".meta"):
                    continue
                for part in reversed(name.split("-")):
                    if part.isdigit() and len(part) >= 10:
                        fid = int(part)
                        break
            elif name.isdigit():
                fid = int(name)
            if fid is None:
                continue
            try:
                with open(entry.path, "rb") as f:
                    head = f.read(16)
            except OSError:
                continue
            kind = _sniff_kind(head)
            if kind is None or kind not in accept:
                continue
            found[fid] = MediaBody(Path(entry.path), kind)
    return found


def _build_media_index(media_dir: Path, fetch_dir: Path) -> dict[int, MediaBody]:
    """Find full-document sticker bodies keyed by file_id.

    `postbox/media/` holds Telegram-macOS's own sticker documents. Our
    `~/.sticky/media/` holds bodies pulled via the Bot API (`sticky
    fetch-missing`). Postbox files take precedence when both are present.
    """
    fetched = _scan_media_dir(fetch_dir, accept=("webm", "tgs", "webp"))
    local = _scan_media_dir(media_dir, accept=("webm", "tgs", "webp"))
    fetched.update(local)
    return fetched


@dataclass
class Summary:
    total_sends: int
    distinct_stickers: int
    installed_packs: int
    dynamic_packs: int
    last_sync: str | None


@dataclass
class StickerCell:
    rank: int
    file_id: int
    sends: int
    thumb_src: str | None
    video_src: str | None
    tgs_src: str | None
    webp_src: str | None
    last_sent: str | None


@dataclass
class PackRow:
    title: str
    short_name: str | None
    heat: int
    sticker_count: int
    bar_pct: float


@dataclass
class PackGrid:
    title: str
    short_name: str | None
    total_sends: int
    used_count: int
    installed_count: int
    cells: list[StickerCell]


@dataclass
class DailyPoint:
    day: str
    count: int


@dataclass
class ReportData:
    generated_at: str
    account_id: str | None
    summary: Summary
    daily: list[DailyPoint]
    windows: list[tuple[str, list[StickerCell]]]
    packs: list[PackRow]
    graveyard: list[StickerCell]
    unresolved: list[StickerCell]
    unresolved_total_sends: int
    pack_grids: list[PackGrid]


async def gather(
    session: AsyncSession,
    *,
    cache_dir: Path,
    account_id: str | None,
    top_n: int = 24,
) -> ReportData:
    """Pull everything the report needs out of the local DB."""
    all_usage = (await session.execute(select(StickerUsage))).scalars().all()
    thumb_index = _build_thumb_index(cache_dir)
    fetch_base = Path.home() / ".sticky" / "media"
    media_index = _build_media_index(cache_dir.parent, fetch_base)
    thumb_cache: dict[int, str | None] = {}
    media_cache: dict[int, dict[str, str | None]] = {}

    def cached_thumb(file_id: int) -> str | None:
        if file_id in thumb_cache:
            return thumb_cache[file_id]
        entry = thumb_index.get(file_id)
        if entry is None:
            thumb_cache[file_id] = None
            return None
        path, mime = entry
        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            src = f"data:{mime};base64,{encoded}"
        except OSError:
            src = None
        thumb_cache[file_id] = src
        return src

    _MIME_BY_KIND = {
        "webm": "video/webm",
        "tgs": "application/gzip",
        "webp": "image/webp",
    }

    def cached_media(file_id: int) -> dict[str, str | None]:
        cached = media_cache.get(file_id)
        if cached is not None:
            return cached
        entry = media_index.get(file_id)
        out: dict[str, str | None] = {"video": None, "tgs": None, "webp": None}
        if entry is not None:
            try:
                raw = entry.path.read_bytes()
                encoded = base64.b64encode(raw).decode("ascii")
                src = f"data:{_MIME_BY_KIND[entry.kind]};base64,{encoded}"
                if entry.kind == "webm":
                    out["video"] = src
                elif entry.kind == "tgs":
                    out["tgs"] = src
                elif entry.kind == "webp":
                    out["webp"] = src
            except OSError:
                pass
        media_cache[file_id] = out
        return out

    def cell_assets(file_id: int) -> tuple[str | None, str | None, str | None, str | None]:
        """Return (thumb_src, video_src, tgs_src, webp_src)."""
        thumb = cached_thumb(file_id)
        media = cached_media(file_id)
        return thumb, media["video"], media["tgs"], media["webp"]

    sync_row = (
        await session.execute(select(SyncState).where(SyncState.id == 1))
    ).scalar_one_or_none()
    last_sync = (
        sync_row.last_sync_at.strftime("%Y-%m-%d %H:%M UTC")
        if sync_row and sync_row.last_sync_at
        else None
    )
    installed_packs = int(
        (await session.execute(select(func.count()).select_from(Pack))).scalar() or 0
    )
    dynamic_packs = int(
        (await session.execute(select(func.count()).select_from(DynamicPack))).scalar() or 0
    )
    summary = Summary(
        total_sends=sum(u.total_sends for u in all_usage),
        distinct_stickers=len(all_usage),
        installed_packs=installed_packs,
        dynamic_packs=dynamic_packs,
        last_sync=last_sync,
    )

    windows: list[tuple[str, list[StickerCell]]] = []
    for label, window in [("All-time", "all"), ("90 days", "90d"), ("30 days", "30d"), ("7 days", "7d")]:
        rows = await top_by_window(session, window=window, limit=top_n)  # type: ignore[arg-type]
        cells = [
            _cell(r, i + 1, *cell_assets(r.file_id), for_window=window)
            for i, r in enumerate(rows)
        ]
        windows.append((label, cells))

    pack_rows = (
        await session.execute(
            select(Pack).where(Pack.heat_score > 0).order_by(desc(Pack.heat_score)).limit(20)
        )
    ).scalars().all()
    peak = max((p.heat_score for p in pack_rows), default=1.0) or 1.0
    packs = [
        PackRow(
            title=p.title or "(untitled)",
            short_name=p.short_name,
            heat=int(p.heat_score),
            sticker_count=p.sticker_count,
            bar_pct=100.0 * p.heat_score / peak,
        )
        for p in pack_rows
    ]

    grave = await graveyard(session, min_lifetime_sends=10, idle_days=90, limit=18)
    grave_cells = [
        _cell(r, i + 1, *cell_assets(r.file_id), for_window="all")
        for i, r in enumerate(grave)
    ]

    daily = _aggregate_daily(all_usage, days=90)

    unresolved_rows = (
        await session.execute(
            select(UnresolvedSend).order_by(desc(UnresolvedSend.total_sends)).limit(top_n)
        )
    ).scalars().all()
    unresolved_total = int(
        (
            await session.execute(select(func.sum(UnresolvedSend.total_sends)))
        ).scalar()
        or 0
    )
    unresolved_cells: list[StickerCell] = []
    for i, row in enumerate(unresolved_rows):
        last = (
            datetime.fromtimestamp(row.last_sent_at, tz=timezone.utc).strftime("%Y-%m-%d")
            if row.last_sent_at
            else None
        )
        thumb_src, video_src, tgs_src, webp_src = cell_assets(row.file_id)
        unresolved_cells.append(
            StickerCell(
                rank=i + 1,
                file_id=row.file_id,
                sends=row.total_sends,
                thumb_src=thumb_src,
                video_src=video_src,
                tgs_src=tgs_src,
                webp_src=webp_src,
                last_sent=last,
            )
        )

    pack_grids = await _build_pack_grids(session, cell_assets)

    return ReportData(
        generated_at=datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        account_id=account_id,
        summary=summary,
        daily=daily,
        windows=windows,
        packs=packs,
        graveyard=grave_cells,
        unresolved=unresolved_cells,
        unresolved_total_sends=unresolved_total,
        pack_grids=pack_grids,
    )


async def _build_pack_grids(
    session: AsyncSession,
    cell_assets,
) -> list[PackGrid]:
    """For each installed pack, grid of its stickers you've actually sent."""
    rows = (
        await session.execute(
            select(Pack).where(Pack.heat_score > 0).order_by(desc(Pack.heat_score))
        )
    ).scalars().all()
    grids: list[PackGrid] = []
    for pack in rows:
        usage_rows = (
            await session.execute(
                select(StickerUsage)
                .where(StickerUsage.sticker_set_id == pack.collection_id)
                .where(StickerUsage.total_sends > 0)
                .order_by(desc(StickerUsage.total_sends))
            )
        ).scalars().all()
        if not usage_rows:
            continue
        cells = [
            _cell(u, i + 1, *cell_assets(u.file_id), for_window="all")
            for i, u in enumerate(usage_rows)
        ]
        grids.append(
            PackGrid(
                title=pack.title or "(untitled)",
                short_name=pack.short_name,
                total_sends=int(pack.heat_score),
                used_count=len(usage_rows),
                installed_count=pack.sticker_count,
                cells=cells,
            )
        )
    return grids


def _cell(
    usage: StickerUsage,
    rank: int,
    thumb_src: str | None,
    video_src: str | None,
    tgs_src: str | None,
    webp_src: str | None,
    *,
    for_window: str,
) -> StickerCell:
    last = (
        datetime.fromtimestamp(usage.last_sent_at, tz=timezone.utc).strftime("%Y-%m-%d")
        if usage.last_sent_at
        else None
    )
    return StickerCell(
        rank=rank,
        file_id=usage.file_id,
        sends=_windowed_sends(usage, for_window),
        thumb_src=thumb_src,
        video_src=video_src,
        tgs_src=tgs_src,
        webp_src=webp_src,
        last_sent=last,
    )


def _windowed_sends(usage: StickerUsage, window: str) -> int:
    if window == "all":
        return usage.total_sends
    from datetime import date, timedelta

    days = {"7d": 7, "30d": 30, "90d": 90}.get(window, 0)
    if not days:
        return usage.total_sends
    cutoff = date.today() - timedelta(days=days)
    series = (usage.daily_sends or {}).get("series") or []
    total = 0
    for entry in series:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        try:
            d = date.fromisoformat(str(entry[0]))
        except ValueError:
            continue
        if d >= cutoff:
            total += int(entry[1] or 0)
    return total


def _aggregate_daily(rows: list[StickerUsage], *, days: int) -> list[DailyPoint]:
    from datetime import date, timedelta

    by_day: dict[str, int] = {}
    cutoff = date.today() - timedelta(days=days)
    for u in rows:
        series = (u.daily_sends or {}).get("series") or []
        for entry in series:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            try:
                d = date.fromisoformat(str(entry[0]))
            except ValueError:
                continue
            if d < cutoff:
                continue
            by_day[str(d)] = by_day.get(str(d), 0) + int(entry[1] or 0)
    # Fill in days with 0 so the sparkline is continuous.
    out: list[DailyPoint] = []
    for offset in range(days, -1, -1):
        d = str(date.today() - timedelta(days=offset))
        out.append(DailyPoint(day=d, count=by_day.get(d, 0)))
    return out


def sparkline_svg(points: list[DailyPoint], width: int = 800, height: int = 100) -> str:
    if not points:
        return ""
    peak = max(p.count for p in points) or 1
    step = width / max(len(points) - 1, 1)
    coords = []
    for i, p in enumerate(points):
        x = i * step
        y = height - (p.count / peak) * (height - 8) - 4
        coords.append(f"{x:.1f},{y:.1f}")
    path = "M " + " L ".join(coords)
    area = f"M 0,{height} L " + " L ".join(coords) + f" L {width},{height} Z"
    return (
        f'<svg class="timeseries" viewBox="0 0 {width} {height}" preserveAspectRatio="none">'
        f'<path d="{area}" fill="rgba(124, 58, 237, 0.08)"/>'
        f'<path d="{path}" fill="none" stroke="#7c3aed" stroke-width="1.5"/>'
        f"</svg>"
    )


TEMPLATE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sticky — your sticker stats</title>
<style>
:root { --fg:#111827; --muted:#6b7280; --card:#f9fafb; --accent:#7c3aed; --border:#e5e7eb; }
* { box-sizing: border-box; }
body { font: 15px/1.55 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif; color: var(--fg); margin: 0; background: #fff; }
.container { max-width: 980px; margin: 0 auto; padding: 40px 24px 80px; }
h1 { font-size: 28px; margin: 0 0 6px; letter-spacing: -0.01em; }
h2 { font-size: 16px; font-weight: 600; margin: 44px 0 14px; letter-spacing: -0.005em; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 28px; }
.kpi { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
.kpi-value { font-size: 22px; font-weight: 600; line-height: 1.2; }
.kpi-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; margin-top: 2px; }
.grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; }
.cell { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 10px 8px 8px; text-align: center; position: relative; }
.cell .thumb { height: 88px; display: flex; align-items: center; justify-content: center; }
.cell img, .cell video { max-width: 88px; max-height: 88px; width: auto; height: auto; display: block; }
.cell .tgs { width: 88px; height: 88px; }
.cell .tgs svg { width: 100%; height: 100%; display: block; }
.cell .placeholder { width: 88px; height: 88px; background: #fff; border: 1px dashed var(--border); border-radius: 6px; display: inline-flex; align-items: center; justify-content: center; color: var(--muted); font-size: 10px; }
.cell .rank { position: absolute; top: 6px; left: 8px; font-size: 10px; color: var(--muted); }
.cell .sends { font-weight: 600; font-size: 14px; }
.cell .last { font-size: 10px; color: var(--muted); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: middle; }
th { color: var(--muted); font-weight: 500; text-transform: uppercase; font-size: 10px; letter-spacing: .06em; }
.bar-track { background: var(--border); border-radius: 3px; height: 6px; width: 180px; display: inline-block; overflow: hidden; vertical-align: middle; }
.bar { display: block; height: 100%; background: var(--accent); }
.timeseries { width: 100%; height: 100px; display: block; }
details.pack { border: 1px solid var(--border); border-radius: 10px; padding: 10px 14px; margin: 10px 0; background: #fff; }
details.pack[open] { background: var(--card); }
details.pack summary { cursor: pointer; list-style: none; padding: 4px 0; }
details.pack summary::-webkit-details-marker { display: none; }
details.pack summary::before { content: "▸"; display: inline-block; width: 14px; color: var(--muted); transition: transform .15s ease; }
details.pack[open] summary::before { transform: rotate(90deg); }
.pack-title { font-weight: 600; }
.pack-meta { margin-left: 6px; }
.tgs-fallback { width: 88px; height: 88px; display: inline-flex; align-items: center; justify-content: center; color: var(--muted); font-size: 10px; background: #fff; border: 1px dashed var(--border); border-radius: 6px; }
footer { margin-top: 48px; color: var(--muted); font-size: 12px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
@media (max-width: 720px) { .grid { grid-template-columns: repeat(3, 1fr); } .kpis { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body>
{% macro sticker_thumb(c) -%}
  {%- if c.video_src -%}
    <div class="thumb"><video src="{{ c.video_src }}" autoplay loop muted playsinline disableremoteplayback></video></div>
  {%- elif c.tgs_src -%}
    <div class="thumb"><div class="tgs" data-tgs="{{ c.tgs_src }}"></div></div>
  {%- elif c.webp_src -%}
    <div class="thumb"><img src="{{ c.webp_src }}" alt=""></div>
  {%- elif c.thumb_src -%}
    <div class="thumb"><img src="{{ c.thumb_src }}" alt=""></div>
  {%- else -%}
    <div class="placeholder">no<br>thumb</div>
  {%- endif -%}
{%- endmacro %}
<div class="container">

<h1>Your sticker stats</h1>
<div class="muted small">
  {% if data.account_id %}account {{ data.account_id }} · {% endif %}
  {% if data.summary.last_sync %}last synced {{ data.summary.last_sync }}{% else %}never synced{% endif %}
</div>

<div class="kpis">
  <div class="kpi"><div class="kpi-value">{{ "{:,}".format(data.summary.total_sends) }}</div><div class="kpi-label">Total sends</div></div>
  <div class="kpi"><div class="kpi-value">{{ "{:,}".format(data.summary.distinct_stickers) }}</div><div class="kpi-label">Distinct stickers</div></div>
  <div class="kpi"><div class="kpi-value">{{ data.summary.installed_packs }}</div><div class="kpi-label">Installed packs</div></div>
  <div class="kpi"><div class="kpi-value">{{ data.summary.dynamic_packs }}</div><div class="kpi-label">Dynamic packs</div></div>
</div>

<h2>Daily sends · last 90 days</h2>
{{ sparkline|safe }}

{% for label, cells in data.windows %}
  <h2>Top stickers · {{ label }}</h2>
  {% if cells %}
    <div class="grid">
    {% for c in cells %}
      <div class="cell">
        <div class="rank">#{{ c.rank }}</div>
        {{ sticker_thumb(c) }}
        <div class="sends">{{ c.sends }}</div>
        <div class="last">{% if c.last_sent %}{{ c.last_sent }}{% else %}—{% endif %}</div>
      </div>
    {% endfor %}
    </div>
  {% else %}
    <div class="muted small">No data in this window.</div>
  {% endif %}
{% endfor %}

<h2>Pack heat</h2>
{% if data.packs %}
<table>
  <tr><th>Pack</th><th>Stickers</th><th>Sends</th><th></th></tr>
  {% for p in data.packs %}
    <tr>
      <td>{{ p.title }}{% if p.short_name %} <span class="muted small">/ {{ p.short_name }}</span>{% endif %}</td>
      <td>{{ p.sticker_count }}</td>
      <td>{{ p.heat }}</td>
      <td><span class="bar-track"><span class="bar" style="width: {{ "%.1f"|format(p.bar_pct) }}%"></span></span></td>
    </tr>
  {% endfor %}
</table>
{% else %}
<div class="muted small">No pack usage recorded yet.</div>
{% endif %}

<h2>By pack</h2>
<div class="muted small">Which stickers you actually sent from each installed pack.</div>
{% if data.pack_grids %}
  {% for pg in data.pack_grids %}
    <details class="pack" {% if loop.index <= 3 %}open{% endif %}>
      <summary>
        <span class="pack-title">{{ pg.title }}</span>
        {% if pg.short_name %}<span class="muted small"> / {{ pg.short_name }}</span>{% endif %}
        <span class="pack-meta muted small">— {{ pg.total_sends }} sends · {{ pg.used_count }} of {{ pg.installed_count }} stickers used</span>
      </summary>
      <div class="grid" style="margin-top: 12px;">
      {% for c in pg.cells %}
        <div class="cell">
          <div class="rank">#{{ c.rank }}</div>
          {{ sticker_thumb(c) }}
          <div class="sends">{{ c.sends }}</div>
          <div class="last">{% if c.last_sent %}{{ c.last_sent }}{% else %}—{% endif %}</div>
        </div>
      {% endfor %}
      </div>
    </details>
  {% endfor %}
{% else %}
  <div class="muted small">No per-pack usage yet.</div>
{% endif %}

<h2>Graveyard</h2>
<div class="muted small">Stickers you used 10+ times but haven't sent in the last 90 days.</div>
{% if data.graveyard %}
  <div class="grid" style="margin-top: 12px;">
  {% for c in data.graveyard %}
    <div class="cell">
      <div class="rank">#{{ c.rank }}</div>
      {{ sticker_thumb(c) }}
      <div class="sends">{{ c.sends }}</div>
      <div class="last">{% if c.last_sent %}last {{ c.last_sent }}{% else %}—{% endif %}</div>
    </div>
  {% endfor %}
  </div>
{% else %}
  <div class="muted small">Nothing qualifies — you're using your stickers.</div>
{% endif %}

<h2>From uninstalled / external packs</h2>
<div class="muted small">
  Stickers you sent whose pack isn't currently installed — usually because you uninstalled it after sending.
  These are included in your total sends but can't be put into dynamic packs.
  {% if data.unresolved_total_sends %}{{ "{:,}".format(data.unresolved_total_sends) }} sends across {{ data.unresolved|length }} stickers shown.{% endif %}
</div>
{% if data.unresolved %}
  <div class="grid" style="margin-top: 12px;">
  {% for c in data.unresolved %}
    <div class="cell">
      <div class="rank">#{{ c.rank }}</div>
      {{ sticker_thumb(c) }}
      <div class="sends">{{ c.sends }}</div>
      <div class="last">{% if c.last_sent %}last {{ c.last_sent }}{% else %}—{% endif %}</div>
    </div>
  {% endfor %}
  </div>
{% else %}
  <div class="muted small">None — every sticker you've sent maps to an installed pack.</div>
{% endif %}

<footer>
Generated by <a href="https://github.com/adrianleb/sticky">Sticky</a> · {{ data.generated_at }}
</footer>

</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/lottie-web/5.12.2/lottie_light.min.js" crossorigin="anonymous"></script>
<script>
(function () {
  const nodes = document.querySelectorAll('.tgs[data-tgs]');
  if (!nodes.length) return;
  const hasLottie = typeof lottie !== 'undefined';
  const hasDecompression = typeof DecompressionStream !== 'undefined';
  if (!hasLottie || !hasDecompression) {
    nodes.forEach((n) => {
      n.outerHTML = '<div class="tgs-fallback">tgs</div>';
    });
    return;
  }
  async function decode(uri) {
    const resp = await fetch(uri);
    const stream = resp.body.pipeThrough(new DecompressionStream('gzip'));
    const text = await new Response(stream).text();
    return JSON.parse(text);
  }
  const io = new IntersectionObserver((entries, obs) => {
    entries.forEach((e) => {
      if (!e.isIntersecting) return;
      obs.unobserve(e.target);
      const node = e.target;
      const uri = node.getAttribute('data-tgs');
      node.removeAttribute('data-tgs');
      decode(uri).then((animationData) => {
        lottie.loadAnimation({
          container: node,
          renderer: 'svg',
          loop: true,
          autoplay: true,
          animationData,
        });
      }).catch(() => {
        node.outerHTML = '<div class="tgs-fallback">tgs err</div>';
      });
    });
  }, { rootMargin: '200px' });
  nodes.forEach((n) => io.observe(n));
})();
</script>
</body>
</html>
"""
)


def render(data: ReportData) -> str:
    return TEMPLATE.render(
        data=data,
        sparkline=sparkline_svg(data.daily),
    )
