"""CLI presentation layer using Typer + Rich. Imports core, formats output."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

from .config import load_config, init_core
from .models import CloneRequest, DiffRequest, MigrateRequest, MirrorState, PruneRequest

app = typer.Typer(name="hfmirror", help="Hugging Face Model Mirror & Backup Utility")
console = Console()


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


@app.command()
def clone(
    repo_id: str = typer.Argument(..., help="HF repo ID, e.g. meta-llama/Llama-3.1-70B"),
    revision: str = typer.Option("main", help="Branch/tag/commit to clone"),
    force_tier: Optional[str] = typer.Option(None, help="Force routing to drive1 or drive2 (tier1/tier2)"),
):
    """Clone a Hugging Face repository and mirror to Gitea."""
    async def _clone():
        config = load_config()
        core = await init_core(config)
        try:
            request = CloneRequest(
                repo_id=repo_id, revision=revision, force_tier=force_tier
            )
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TextColumn("{task.fields[info]}"),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Cloning...", total=100, info="")
                async for p in core.clone(request):
                    if p.phase == "error":
                        console.print(f"[red]Error:[/red] {p.message}")
                        raise typer.Exit(1)
                    pct = (
                        (p.bytes_downloaded / p.bytes_total * 100)
                        if p.bytes_total > 0
                        else 0
                    )
                    info = p.message
                    if p.speed_bytes_sec > 0:
                        speed_mb = p.speed_bytes_sec / (1024 * 1024)
                        info += f" | {speed_mb:.1f} MB/s"
                    if p.files_total > 0:
                        info += f" | {p.files_completed}/{p.files_total} files"
                    progress.update(task, completed=pct, description=p.phase, info=info)
            console.print(f"[green]Clone complete:[/green] {repo_id}")
        finally:
            await core.close()

    _run(_clone())


@app.command()
def status(
    repo_id: Optional[str] = typer.Argument(None, help="Specific repo ID or all"),
):
    """Show status of mirrored repositories."""
    async def _status():
        config = load_config()
        core = await init_core(config)
        try:
            if repo_id:
                repo = await core.get_repo_status(repo_id)
                if repo is None:
                    console.print(f"[yellow]Repo {repo_id} not found[/yellow]")
                    raise typer.Exit(1)
                _print_repo_detail(repo)
            else:
                result = await core.list_repos()
                _print_repo_table(result.repos)
        finally:
            await core.close()

    _run(_status())


@app.command(name="list")
def list_repos():
    """List all mirrored repositories."""
    async def _list():
        config = load_config()
        core = await init_core(config)
        try:
            result = await core.list_repos()
            _print_repo_table(result.repos)
        finally:
            await core.close()

    _run(_list())


@app.command()
def diff(repo_id: str = typer.Argument(..., help="HF repo ID")):
    """Show upstream vs local changes for a repository."""
    async def _diff():
        config = load_config()
        core = await init_core(config)
        try:
            result = await core.diff(DiffRequest(repo_id=repo_id))
            if result.is_up_to_date:
                console.print(f"[green]{repo_id} is up to date[/green]")
            else:
                console.print(f"[yellow]{repo_id} has upstream changes[/yellow]")
                console.print(f"  Local commit:    {result.local_commit}")
                console.print(f"  Upstream commit: {result.upstream_commit}")

            table = Table(title="File Changes")
            table.add_column("File")
            table.add_column("Change")
            table.add_column("LFS")
            table.add_column("Local Size")
            table.add_column("Upstream Size")

            for change in result.changes:
                if change.change_type == "unchanged":
                    continue
                style = {
                    "added": "green",
                    "modified": "yellow",
                    "deleted": "red",
                }.get(change.change_type, "")
                table.add_row(
                    change.filename,
                    f"[{style}]{change.change_type}[/{style}]",
                    "Yes" if change.is_lfs else "No",
                    _fmt_size(change.local_size),
                    _fmt_size(change.upstream_size),
                )

            if any(c.change_type != "unchanged" for c in result.changes):
                console.print(table)
        finally:
            await core.close()

    _run(_diff())


@app.command()
def update(
    repo_id: Optional[str] = typer.Argument(None, help="Repo ID or --all"),
    all_repos: bool = typer.Option(False, "--all", help="Update all stale repos"),
):
    """Pull upstream changes for a repository."""
    async def _update():
        config = load_config()
        core = await init_core(config)
        try:
            if all_repos:
                async for p in core.update_all():
                    console.print(f"  [{p.phase}] {p.message}")
                console.print("[green]All repos updated[/green]")
            elif repo_id:
                async for p in core.update(repo_id):
                    console.print(f"  [{p.phase}] {p.message}")
                console.print(f"[green]{repo_id} updated[/green]")
            else:
                console.print("[red]Specify a repo_id or --all[/red]")
                raise typer.Exit(1)
        finally:
            await core.close()

    _run(_update())


@app.command()
def migrate(
    repo_id: str = typer.Argument(..., help="HF repo ID"),
    to: str = typer.Option(..., "--to", help="Target drive: drive1/tier1 or drive2/tier2"),
    files: Optional[str] = typer.Option(None, help="Glob pattern for files to migrate"),
):
    """Migrate files between drives."""
    async def _migrate():
        config = load_config()
        core = await init_core(config)
        try:
            file_list = [files] if files else None
            result = await core.migrate(
                MigrateRequest(repo_id=repo_id, target_tier=to, files=file_list)
            )
            console.print(f"[green]Migrated {result.files_moved} files ({_fmt_size(result.bytes_moved)})[/green]")
            console.print(f"  Symlinks created: {result.symlinks_created}")
            console.print(f"  Duration: {result.duration_seconds:.1f}s")
        finally:
            await core.close()

    _run(_migrate())


@app.command()
def prune(
    repo_id: str = typer.Argument(..., help="HF repo ID"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without deleting"),
    keep_gitea: bool = typer.Option(False, "--keep-gitea", help="Don't delete from Gitea"),
):
    """Remove a mirrored repository and reclaim storage."""
    async def _prune():
        config = load_config()
        core = await init_core(config)
        try:
            result = await core.prune(
                PruneRequest(
                    repo_id=repo_id,
                    delete_from_gitea=not keep_gitea,
                    dry_run=dry_run,
                )
            )
            prefix = "[dim](dry run)[/dim] " if result.was_dry_run else ""
            console.print(f"{prefix}[green]Pruned {repo_id}[/green]")
            console.print(f"  Files deleted: {result.files_deleted}")
            console.print(f"  Space reclaimed: {_fmt_size(result.bytes_reclaimed)}")
            console.print(f"  Gitea repo deleted: {result.gitea_repo_deleted}")
        finally:
            await core.close()

    _run(_prune())


@app.command()
def doctor():
    """Run system health checks."""
    async def _doctor():
        config = load_config()
        core = await init_core(config)
        try:
            result = await core.doctor()
            for check in result.checks:
                icon = "[green]PASS[/green]" if check.passed else "[red]FAIL[/red]"
                console.print(f"  {icon} {check.name}: {check.message}")
                for detail in check.details:
                    console.print(f"       {detail}")

            if result.all_passed:
                console.print("\n[green]All checks passed[/green]")
            else:
                console.print("\n[red]Some checks failed[/red]")
                raise typer.Exit(1)
        finally:
            await core.close()

    _run(_doctor())


@app.command()
def setup():
    """Interactive first-run configuration wizard."""
    console.print("[bold]HFMirror Setup Wizard[/bold]\n")
    env_path = Path(".env")

    values = {}
    values["HF_TOKEN"] = typer.prompt("Hugging Face token (hf_...)")
    values["TIER1_PATH"] = typer.prompt("Drive 1 storage path", default="./downloads")
    tier2 = typer.prompt("Drive 2 storage path (leave empty to disable)", default="")
    if tier2:
        values["TIER2_PATH"] = tier2
    values["GITEA_PORT"] = typer.prompt("Gitea port", default="3000")
    values["GITEA_ADMIN_USER"] = typer.prompt("Gitea admin username", default="hfmirror")

    import secrets
    default_pass = secrets.token_urlsafe(16)
    values["GITEA_ADMIN_PASSWORD"] = typer.prompt(
        "Gitea admin password", default=default_pass
    )

    lines = [f"{k}={v}" for k, v in values.items()]
    env_path.write_text("\n".join(lines) + "\n")
    console.print(f"\n[green]Configuration saved to {env_path}[/green]")
    console.print("Run [bold]./run.sh web[/bold] or [bold]./run.sh cli clone <repo>[/bold] to get started.")


@app.command()
def restart():
    """Restart the application to apply new settings."""
    import os
    import sys
    console.print("[yellow]Restarting...[/yellow]")
    os.execv(sys.executable, [sys.executable] + sys.argv)


@app.command(name="open")
def open_repo(repo_id: str = typer.Argument(..., help="HF repo ID")):
    """Print the Gitea URL for a mirrored repository."""
    async def _open():
        config = load_config()
        core = await init_core(config)
        try:
            url = core.get_gitea_url(repo_id)
            console.print(url)
        finally:
            await core.close()

    _run(_open())


# --- Display Helpers ---


def _print_repo_table(repos):
    table = Table(title="Mirrored Repositories")
    table.add_column("Repository")
    table.add_column("State")
    table.add_column("Size")
    table.add_column("Location")
    table.add_column("Last Synced")

    state_icons = {
        MirrorState.SYNCED: "[green]synced[/green]",
        MirrorState.STALE: "[yellow]stale[/yellow]",
        MirrorState.ERROR: "[red]error[/red]",
        MirrorState.CLONING: "[blue]cloning[/blue]",
        MirrorState.UPDATING: "[blue]updating[/blue]",
        MirrorState.PENDING: "[dim]pending[/dim]",
        MirrorState.PRUNED: "[dim]pruned[/dim]",
    }

    for repo in repos:
        location = "Drive 2" if repo.tier2_path else "Drive 1"
        synced = _fmt_time_ago(repo.last_synced) if repo.last_synced else "never"
        table.add_row(
            repo.repo_id,
            state_icons.get(repo.state, str(repo.state)),
            _fmt_size(repo.total_size_bytes),
            location,
            synced,
        )

    console.print(table)


def _print_repo_detail(repo):
    console.print(f"[bold]{repo.repo_id}[/bold]")
    console.print(f"  State:           {repo.state.value}")
    console.print(f"  Gitea name:      {repo.gitea_repo_name}")
    console.print(f"  Total size:      {_fmt_size(repo.total_size_bytes)}")
    console.print(f"  LFS size:        {_fmt_size(repo.lfs_size_bytes)}")
    console.print(f"  Drive 1 path:    {repo.tier1_path}")
    console.print(f"  Drive 2 path:    {repo.tier2_path or 'N/A'}")
    console.print(f"  Upstream commit: {repo.upstream_commit or 'unknown'}")
    console.print(f"  Local commit:    {repo.local_commit or 'unknown'}")
    console.print(f"  Last checked:    {repo.last_checked or 'never'}")
    console.print(f"  Last synced:     {repo.last_synced or 'never'}")
    if repo.error_message:
        console.print(f"  [red]Error: {repo.error_message}[/red]")


def _fmt_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024  # type: ignore[assignment]
    return f"{size_bytes:.1f} PB"


def _fmt_time_ago(dt) -> str:
    if dt is None:
        return "never"
    from datetime import datetime
    delta = datetime.now() - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"
