"""`sticky` CLI."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table
from sqlalchemy import select

from . import account as accounts
from . import cache_index, config, daemon, keychain, state
from .account import TelegramAccount
from .botapi import BotClient
from .config import Config, ConfigError
from .db import DynamicPack, Pack, StickerUsage, init_schema, make_engine, session_scope
from .ingest import apply as apply_scan
from .packs import (
    PackError,
    create_pack,
    delete_pack_record,
    install_url,
    list_packs,
    refresh_pack,
)
from .rank import top_by_window
from .scan import ScanResult, run_scan

app = typer.Typer(
    add_completion=False,
    help="sticky — Telegram sticker analytics + dynamic packs (macOS).",
    no_args_is_help=True,
)
console = Console()

packs_app = typer.Typer(help="Create + manage dynamic sticker packs.", no_args_is_help=True)
app.add_typer(packs_app, name="packs")
daemon_app = typer.Typer(help="Manage the periodic-sync launchd agent.", no_args_is_help=True)
app.add_typer(daemon_app, name="daemon")


# ─── helpers ────────────────────────────────────────────────────────────────


def _load_config() -> Config:
    try:
        return config.load()
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


def _resolve_account(cfg: Config) -> TelegramAccount:
    try:
        return accounts.resolve_account(cfg.account_id)
    except (FileNotFoundError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


async def _run_scan_async(cfg: Config, full: bool) -> tuple[TelegramAccount, ScanResult]:
    st = state.load_state()
    acct = _resolve_account(cfg)
    since: int | None = None
    if not full:
        async with session_scope(_engine()) as session:
            from .db import SyncState as SS

            sync_row = (
                await session.execute(select(SS).where(SS.id == 1))
            ).scalar_one_or_none()
            if sync_row and sync_row.last_message_timestamp:
                since = sync_row.last_message_timestamp
    result = await asyncio.to_thread(
        run_scan, acct, peer_salt=st.peer_salt, since_ts=since
    )
    return acct, result


_engine_singleton = None


def _engine():
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = make_engine()
    return _engine_singleton


async def _ensure_db() -> None:
    await init_schema(_engine())


def _bot_client(cfg: Config) -> BotClient:
    jwt = keychain.load_jwt() if cfg.is_proxy() else None
    if cfg.is_proxy() and not jwt:
        console.print(
            "[red]Not paired.[/red] Run `sticky pair <code>` to pair against the proxy."
        )
        raise typer.Exit(code=1)
    return BotClient(cfg, jwt_token=jwt)


def _cache_dir(cfg: Config) -> Path:
    acct = _resolve_account(cfg)
    return acct.account_dir / "postbox" / "media" / "cache"


# ─── init ───────────────────────────────────────────────────────────────────


@app.command()
def init() -> None:
    """Walk through first-run setup: pick mode (proxy or local), pair or paste token."""
    if config.config_path().exists():
        if not Confirm.ask(
            f"Config already exists at {config.config_path()}. Overwrite?",
            default=False,
        ):
            raise typer.Exit()

    mode = Prompt.ask(
        "Mode", choices=["proxy", "local"], default="proxy",
    )
    accts = accounts.discover_accounts()
    if not accts:
        console.print(
            "[red]No Telegram-macOS account found.[/red] Make sure Telegram-macOS is installed and has logged in at least once."
        )
        raise typer.Exit(code=1)
    acct = accts[0]
    if len(accts) > 1:
        console.print("Multiple Telegram-macOS accounts found. Pick one:")
        for i, candidate in enumerate(accts):
            console.print(f"  [{i}] {candidate.display_id}")
        idx = int(Prompt.ask("Which", default="0"))
        acct = accts[idx]

    if mode == "proxy":
        proxy_url = Prompt.ask(
            "Proxy URL", default=config.DEFAULT_PROXY_URL,
        )
        console.print()
        console.print(
            "[bold]Open Telegram[/bold], message [cyan]@sticky_sticky_sticky_bot[/cyan] "
            "and send [bold]/pair[/bold]. It will reply with a 6-digit code."
        )
        code = Prompt.ask("Paste code")
        try:
            resp = httpx.post(
                proxy_url.rstrip("/") + "/pair",
                json={"code": code.strip()},
                timeout=30.0,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            console.print(f"[red]Pair failed:[/red] {exc}")
            raise typer.Exit(code=1) from exc

        keychain.save_jwt(body["token"])
        cfg = Config(
            mode="proxy",
            telegram_user_id=int(body["telegram_user_id"]),
            bot_username=body["bot_username"],
            proxy_url=proxy_url,
            account_id=acct.display_id,
        )
        config.write(cfg)
        console.print(
            f"[green]Paired[/green] as user {cfg.telegram_user_id} via @{cfg.bot_username}."
        )
        return

    # local
    token = Prompt.ask(
        "BotFather bot token (keep it secret)", password=True,
    )
    try:
        resp = httpx.get(
            f"https://api.telegram.org/bot{token}/getMe", timeout=30.0
        )
        resp.raise_for_status()
        me = resp.json()["result"]
    except Exception as exc:
        console.print(f"[red]Bad token:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"Now open Telegram, message [cyan]@{me['username']}[/cyan] and send [bold]/start[/bold] "
        "so the bot sees your chat_id."
    )
    Prompt.ask("Press enter once you've sent /start")

    updates_resp = httpx.get(
        f"https://api.telegram.org/bot{token}/getUpdates", timeout=30.0
    )
    updates = updates_resp.json().get("result") or []
    if not updates:
        console.print(
            "[yellow]No updates from the bot yet. "
            "Send /start in a DM with the bot first, then re-run `sticky init`.[/yellow]"
        )
        raise typer.Exit(code=1)
    tg_user_id = int(updates[-1]["message"]["from"]["id"])
    cfg = Config(
        mode="local",
        telegram_user_id=tg_user_id,
        bot_username=me["username"],
        bot_token=token,
        account_id=acct.display_id,
    )
    config.write(cfg)
    console.print(
        f"[green]Configured[/green] local mode for @{cfg.bot_username} (user {tg_user_id})."
    )


# ─── pair (standalone, for re-pairing after init) ──────────────────────────


@app.command()
def pair(code: str = typer.Argument(..., help="6-digit code from the proxy bot.")) -> None:
    """Re-pair against the proxy (use if your JWT was revoked)."""
    cfg = _load_config()
    if not cfg.is_proxy():
        console.print("[red]Configured for local mode — no pairing needed.[/red]")
        raise typer.Exit(code=1)
    try:
        resp = httpx.post(
            (cfg.proxy_url or config.DEFAULT_PROXY_URL).rstrip("/") + "/pair",
            json={"code": code.strip()},
            timeout=30.0,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        console.print(f"[red]Pair failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    keychain.save_jwt(body["token"])
    console.print(f"[green]Paired[/green] as user {body['telegram_user_id']}.")


@app.command()
def unpair() -> None:
    """Wipe the local JWT, config, and state. Does NOT delete ~/.sticky/sticky.db."""
    keychain.clear_jwt()
    state.reset_state()
    path = config.config_path()
    if path.exists():
        path.unlink()
    if daemon.is_installed():
        daemon.uninstall()
    console.print("[green]Unpaired.[/green] Your sticker stats at ~/.sticky/sticky.db remain.")


# ─── sync ──────────────────────────────────────────────────────────────────


@app.command()
def sync(
    full: bool = typer.Option(False, "--full", help="Re-scan entire message history."),
    quiet: bool = typer.Option(False, "--quiet"),
) -> None:
    """Scan Postbox and update the local DB."""
    cfg = _load_config()

    async def _run():
        await _ensure_db()
        acct, result = await _run_scan_async(cfg, full=full)
        async with session_scope(_engine()) as session:
            stats = await apply_scan(session, result)
        return acct, result, stats

    if quiet:
        acct, result, stats = asyncio.run(_run())
    else:
        start = time.monotonic()
        with console.status("Scanning Postbox + writing local DB…"):
            acct, result, stats = asyncio.run(_run())
        elapsed = time.monotonic() - start
        console.print(
            f"[green]Synced {stats['stickers']} stickers, "
            f"{stats['packs']} packs in {elapsed:.1f}s[/green] "
            f"(account {acct.display_id})."
        )


# ─── status ────────────────────────────────────────────────────────────────


@app.command()
def status() -> None:
    """Show local stats."""
    cfg = _load_config()

    async def _run():
        await _ensure_db()
        async with session_scope(_engine()) as session:
            top = await top_by_window(session, window="all", limit=10)
            total_sends = sum(t.total_sends for t in top)
            distinct = (
                await session.execute(select(StickerUsage))
            ).scalars().all()
            packs_count = (
                await session.execute(select(Pack))
            ).scalars().all()
            dynamic = await list_packs(session)
            return top, len(distinct), len(packs_count), dynamic, total_sends

    top, distinct, packs_count, dynamic, _top10_sends = asyncio.run(_run())

    header = Table.grid(padding=(0, 2))
    header.add_row(
        f"[bold]Mode[/bold]: {cfg.mode}",
        f"[bold]Bot[/bold]: @{cfg.bot_username}",
        f"[bold]Account[/bold]: {cfg.account_id or '?'}",
    )
    console.print(header)

    totals = Table("metric", "value", title="Local stats")
    totals.add_row("distinct stickers", str(distinct))
    totals.add_row("total sends", str(sum(u.total_sends for u in top)) + " (top 10)")
    totals.add_row("installed packs", str(packs_count))
    totals.add_row("dynamic packs", str(len(dynamic)))
    console.print(totals)

    if top:
        table = Table("rank", "file_id", "sends", "last_sent", title="Top 10 all-time")
        for i, row in enumerate(top, 1):
            last = str(row.last_sent_at or "—")
            table.add_row(str(i), str(row.file_id), str(row.total_sends), last)
        console.print(table)


# ─── packs ─────────────────────────────────────────────────────────────────


@packs_app.command("create")
def packs_create(
    title: str = typer.Argument(..., help='e.g. "All-time Top 30"'),
    source: str = typer.Option(
        "top-all", help="top-all | top-7d | top-30d | top-90d"
    ),
    count: int = typer.Option(30, help="Number of stickers (1-120)."),
) -> None:
    """Create a dynamic pack from top stickers and DM the install link."""
    cfg = _load_config()

    async def _run():
        await _ensure_db()
        bot = _bot_client(cfg)
        try:
            async with session_scope(_engine()) as session:
                return await create_pack(
                    session,
                    bot,
                    cfg,
                    title=title,
                    source=source,
                    count=count,
                    cache_dir=_cache_dir(cfg),
                )
        finally:
            await bot.aclose()

    try:
        result = asyncio.run(_run())
    except PackError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]Created[/green] {result.short_name} with {result.added} stickers.\n"
        f"Install: [cyan]{result.install_url}[/cyan]"
    )
    if result.skipped_no_png:
        console.print(
            f"[yellow]Skipped {len(result.skipped_no_png)} stickers with no cached PNG.[/yellow]"
        )


@packs_app.command("refresh")
def packs_refresh(
    short_name: str = typer.Argument(..., help="Pack short_name (e.g. all_time_top_30_by_…)"),
) -> None:
    """Refresh a dynamic pack to match current top stickers."""
    cfg = _load_config()

    async def _run():
        await _ensure_db()
        bot = _bot_client(cfg)
        try:
            async with session_scope(_engine()) as session:
                return await refresh_pack(
                    session,
                    bot,
                    cfg,
                    short_name=short_name,
                    cache_dir=_cache_dir(cfg),
                )
        finally:
            await bot.aclose()

    try:
        summary = asyncio.run(_run())
    except PackError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]Refreshed {summary['short_name']}[/green]: "
        f"+{summary['added']}, -{summary['removed']} "
        f"(now {summary['total']} stickers)"
    )


@packs_app.command("list")
def packs_list() -> None:
    """List your dynamic packs."""
    _load_config()

    async def _run():
        await _ensure_db()
        async with session_scope(_engine()) as session:
            return await list_packs(session)

    packs = asyncio.run(_run())
    if not packs:
        console.print("No dynamic packs yet. Try `sticky packs create \"Top 30\"`.")
        return
    table = Table("short_name", "title", "source", "count", "last_refreshed", title="Dynamic packs")
    for p in packs:
        table.add_row(
            p.short_name,
            p.title,
            p.source,
            str(p.count),
            str(p.last_refreshed_at or "—"),
        )
    console.print(table)


@packs_app.command("delete")
def packs_delete(
    short_name: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    """Forget a dynamic pack locally. Does NOT delete the Telegram sticker set itself
    (Bot API has no 'delete set' call — use @BotFather if you want the set gone)."""
    cfg = _load_config()
    if not yes and not Confirm.ask(f"Forget {short_name} locally?"):
        raise typer.Exit()

    async def _run():
        await _ensure_db()
        async with session_scope(_engine()) as session:
            return await delete_pack_record(session, short_name)

    ok = asyncio.run(_run())
    if not ok:
        console.print(f"[red]No pack named {short_name!r}.[/red]")
        raise typer.Exit(code=1)
    console.print(
        f"[green]Forgot[/green] {short_name}. Telegram sticker set still lives at "
        f"{install_url(short_name)} — ask @BotFather to delete it if you want it gone."
    )


@packs_app.command("archive")
def packs_archive(
    short_name: str = typer.Argument(..., help="Sticker-set short_name to archive."),
) -> None:
    """Open a tg:// link that lets you archive the set in the Telegram app."""
    _load_config()
    tg_url = f"tg://addstickers?set={short_name}"
    console.print(
        f"Open this in Telegram to archive (the app does the archive; Bot API can't):\n"
        f"  [cyan]{tg_url}[/cyan]\n"
        f"Web: [cyan]{install_url(short_name)}[/cyan]"
    )


# ─── diagnose / cache ──────────────────────────────────────────────────────


@app.command()
def diagnose() -> None:
    """Print Postbox table layout (useful when a scan can't find a table)."""
    from .postbox import derive_tempkey, list_kv_tables, open_postbox

    cfg = _load_config()
    acct = _resolve_account(cfg)
    tempkey = derive_tempkey(acct.tempkey_path)
    with open_postbox(acct.db_path, tempkey) as (conn, profile):
        tables = list_kv_tables(conn)
        console.print(f"[bold]SQLCipher profile:[/bold] {profile.name}")
        table = Table("table", "rows", "key_lengths", title="Postbox tables")
        for t in tables:
            table.add_row(t.name, str(t.rows), ",".join(str(k) for k in t.key_lengths))
        console.print(table)


@app.command()
def cache(
    out: Optional[Path] = typer.Option(None, "--out", help="Write index JSON to this path."),
) -> None:
    """Index Postbox's local sticker cache (local-only)."""
    cfg = _load_config()
    acct = _resolve_account(cfg)
    if out:
        count = cache_index.save_index(acct, out)
        console.print(f"[green]Wrote {count} cache entries to {out}[/green]")
        return
    table = Table("document_id", "size", "path", title="Postbox media cache")
    for entry in cache_index.iter_cache(acct):
        table.add_row(str(entry.document_id), str(entry.size), entry.path)
    console.print(table)


# ─── daemon ────────────────────────────────────────────────────────────────


@daemon_app.command("install")
def daemon_install(
    interval_hours: int = typer.Option(12, help="Hours between automatic syncs."),
) -> None:
    """Install the launchd agent for periodic sync."""
    _load_config()
    path = daemon.install(interval_hours * 3600)
    console.print(f"[green]Installed[/green] {path}")


@daemon_app.command("uninstall")
def daemon_uninstall() -> None:
    """Remove the launchd agent."""
    if daemon.uninstall():
        console.print("[green]Uninstalled.[/green]")
    else:
        console.print("Nothing to uninstall.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
