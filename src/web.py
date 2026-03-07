"""Web UI presentation layer using Gradio Blocks. Imports core, renders to browser."""

from __future__ import annotations

import asyncio
import shutil

import gradio as gr

from .config import load_config, init_core
from .models import (
    CloneRequest,
    DiffRequest,
    MigrateRequest,
    MirrorState,
    PruneRequest,
)

config = load_config()
core = None


async def get_core():
    global core
    if core is None:
        core = await init_core(config)
    return core


# --- Dashboard Tab ---

async def refresh_dashboard():
    c = await get_core()
    result = await c.list_repos()
    if not result.repos:
        return []

    rows = []
    for repo in result.repos:
        tier = "tier2" if repo.tier2_path else "tier1"
        synced = str(repo.last_synced)[:19] if repo.last_synced else "never"
        size_gb = repo.total_size_bytes / (1024**3)
        rows.append([
            repo.repo_id,
            repo.state.value,
            f"{size_gb:.1f} GB",
            tier,
            synced,
        ])

    return rows


# --- Clone Tab ---

async def clone_repo(repo_id: str, revision: str, force_tier: str):
    if not repo_id.strip():
        yield "Please enter a repo ID."
        return

    c = await get_core()
    ft = force_tier if force_tier != "auto" else None
    request = CloneRequest(repo_id=repo_id.strip(), revision=revision, force_tier=ft)

    output_lines = []
    async for progress in c.clone(request):
        if progress.phase == "error":
            output_lines.append(f"ERROR: {progress.message}")
            yield "\n".join(output_lines)
            return

        pct = (
            f"{progress.bytes_downloaded / progress.bytes_total * 100:.1f}%"
            if progress.bytes_total > 0
            else "0%"
        )
        speed_mb = progress.speed_bytes_sec / (1024 * 1024) if progress.speed_bytes_sec > 0 else 0
        line = f"[{progress.phase}] {progress.message} | {pct} | {speed_mb:.1f} MB/s"
        if progress.files_total > 0:
            line += f" | {progress.files_completed}/{progress.files_total} files"
        output_lines.append(line)
        yield "\n".join(output_lines[-20:])  # Show last 20 lines

    output_lines.append("Clone complete!")
    yield "\n".join(output_lines[-20:])


# --- Diff Tab ---

async def check_diff(repo_id: str):
    if not repo_id.strip():
        return "Please enter a repo ID."

    c = await get_core()
    result = await c.diff(DiffRequest(repo_id=repo_id.strip()))

    lines = []
    if result.is_up_to_date:
        lines.append(f"{repo_id} is up to date.")
    else:
        lines.append(f"{repo_id} has upstream changes!")
        lines.append(f"Local commit:    {result.local_commit}")
        lines.append(f"Upstream commit: {result.upstream_commit}")
        lines.append("")

    for change in result.changes:
        if change.change_type == "unchanged":
            continue
        lfs = " [LFS]" if change.is_lfs else ""
        lines.append(f"  {change.change_type:>10}  {change.filename}{lfs}")

    return "\n".join(lines) if lines else "No changes."


# --- Storage Tab ---

async def get_storage_info():
    c = await get_core()
    lines = []

    # Tier 1
    config.tier1_path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(config.tier1_path)
    pct = usage.used / usage.total * 100
    lines.append(f"Tier 1 ({config.tier1_path}): {pct:.1f}% used, {usage.free / (1024**3):.1f} GB free")

    # Tier 2
    if config.tier2_path:
        config.tier2_path.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(config.tier2_path)
        pct = usage.used / usage.total * 100
        lines.append(f"Tier 2 ({config.tier2_path}): {pct:.1f}% used, {usage.free / (1024**3):.1f} GB free")
    else:
        lines.append("Tier 2: Not configured")

    # Per-repo breakdown
    result = await c.list_repos()
    if result.repos:
        lines.append("\nPer-repo breakdown:")
        for repo in result.repos:
            size_gb = repo.total_size_bytes / (1024**3)
            tier = "tier2" if repo.tier2_path else "tier1"
            lines.append(f"  {repo.repo_id}: {size_gb:.1f} GB ({tier})")

    return "\n".join(lines)


async def do_migrate(repo_id: str, target_tier: str):
    if not repo_id.strip():
        return "Please enter a repo ID."
    c = await get_core()
    result = await c.migrate(
        MigrateRequest(repo_id=repo_id.strip(), target_tier=target_tier)
    )
    return (
        f"Migrated {result.files_moved} files ({result.bytes_moved / (1024**3):.2f} GB)\n"
        f"Symlinks created: {result.symlinks_created}\n"
        f"Duration: {result.duration_seconds:.1f}s"
    )


# --- Health Tab ---

async def run_doctor():
    c = await get_core()
    result = await c.doctor()

    lines = []
    for check in result.checks:
        icon = "PASS" if check.passed else "FAIL"
        lines.append(f"[{icon}] {check.name}: {check.message}")
        for detail in check.details:
            lines.append(f"       {detail}")

    status = "All checks passed!" if result.all_passed else "Some checks failed."
    lines.append(f"\n{status}")
    return "\n".join(lines)


# --- Settings Tab ---

async def save_settings(
    hf_token, tier1_path, tier2_path, gitea_port, hf_concurrent, log_level
):
    from pathlib import Path

    env_lines = [
        f"HF_TOKEN={hf_token}",
        f"TIER1_PATH={tier1_path}",
    ]
    if tier2_path:
        env_lines.append(f"TIER2_PATH={tier2_path}")
    env_lines.extend([
        f"GITEA_PORT={gitea_port}",
        f"HF_CONCURRENT_DOWNLOADS={hf_concurrent}",
        f"LOG_LEVEL={log_level}",
    ])

    # Preserve existing values not shown in the form
    env_path = Path(".env")
    existing = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()

    new_vals = {}
    for line in env_lines:
        k, v = line.split("=", 1)
        new_vals[k] = v

    merged = {**existing, **new_vals}
    env_path.write_text("\n".join(f"{k}={v}" for k, v in merged.items()) + "\n")
    return "Settings saved. Click 'Restart Server' to apply changes."


async def do_restart():
    """Restart the server process to apply new settings."""
    import os
    import sys

    # Reset the core so it gets re-created with new config on next use
    global core, config
    if core is not None:
        await core.close()
        core = None
    config = load_config()
    return "Server config reloaded. New settings are now active."


# --- Build the Gradio App ---

def create_app() -> gr.Blocks:
    with gr.Blocks(title="HFMirror - Hugging Face Model Mirror") as app:
        gr.Markdown("# HFMirror - Hugging Face Model Mirror")

        with gr.Tab("Dashboard"):
            dashboard_table = gr.Dataframe(
                headers=["Repository", "State", "Size", "LFS Tier", "Last Synced"],
                label="Mirrored Repositories",
            )
            refresh_btn = gr.Button("Refresh")
            refresh_btn.click(fn=refresh_dashboard, outputs=dashboard_table)

        with gr.Tab("Clone"):
            with gr.Row():
                clone_repo_id = gr.Textbox(
                    label="Repository ID",
                    placeholder="meta-llama/Llama-3.1-70B",
                )
                clone_revision = gr.Textbox(label="Revision", value="main")
                clone_tier = gr.Radio(
                    choices=["auto", "tier1", "tier2"],
                    label="Tier Override",
                    value="auto",
                )
            clone_btn = gr.Button("Clone", variant="primary")
            clone_output = gr.Textbox(label="Progress", lines=15, interactive=False)
            clone_btn.click(
                fn=clone_repo,
                inputs=[clone_repo_id, clone_revision, clone_tier],
                outputs=clone_output,
            )

        with gr.Tab("Diff"):
            diff_repo_id = gr.Textbox(
                label="Repository ID",
                placeholder="meta-llama/Llama-3.1-70B",
            )
            diff_btn = gr.Button("Check Diff")
            diff_output = gr.Textbox(label="Diff Result", lines=15, interactive=False)
            diff_btn.click(
                fn=check_diff, inputs=diff_repo_id, outputs=diff_output
            )

        with gr.Tab("Storage"):
            storage_info = gr.Textbox(
                label="Storage Overview", lines=15, interactive=False
            )
            storage_refresh_btn = gr.Button("Refresh Storage Info")
            storage_refresh_btn.click(fn=get_storage_info, outputs=storage_info)

            gr.Markdown("### Migrate Files")
            with gr.Row():
                migrate_repo_id = gr.Textbox(label="Repository ID")
                migrate_tier = gr.Radio(
                    choices=["tier1", "tier2"],
                    label="Target Tier",
                    value="tier2",
                )
            migrate_btn = gr.Button("Migrate")
            migrate_output = gr.Textbox(
                label="Migration Result", lines=5, interactive=False
            )
            migrate_btn.click(
                fn=do_migrate,
                inputs=[migrate_repo_id, migrate_tier],
                outputs=migrate_output,
            )

        with gr.Tab("Health"):
            health_output = gr.Textbox(
                label="Health Checks", lines=20, interactive=False
            )
            health_btn = gr.Button("Run Doctor")
            health_btn.click(fn=run_doctor, outputs=health_output)

        with gr.Tab("Settings"):
            with gr.Column():
                s_token = gr.Textbox(
                    label="HF_TOKEN", type="password",
                    value=config.hf_token.get_secret_value() or "",
                )
                s_tier1 = gr.Textbox(
                    label="TIER1_PATH",
                    value=str(config.tier1_path),
                )
                s_tier2 = gr.Textbox(
                    label="TIER2_PATH (optional)",
                    value=str(config.tier2_path) if config.tier2_path else "",
                )
                s_gitea_port = gr.Number(
                    label="GITEA_PORT",
                    value=config.gitea_port,
                )
                s_concurrent = gr.Slider(
                    label="HF_CONCURRENT_DOWNLOADS",
                    minimum=1, maximum=16, step=1,
                    value=config.hf_concurrent_downloads,
                )
                s_log_level = gr.Radio(
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                    label="LOG_LEVEL",
                    value=config.log_level,
                )
            with gr.Row():
                save_btn = gr.Button("Save Settings", variant="primary")
                restart_btn = gr.Button("Restart Server", variant="secondary")
            settings_output = gr.Textbox(label="Status", interactive=False)
            save_btn.click(
                fn=save_settings,
                inputs=[s_token, s_tier1, s_tier2, s_gitea_port, s_concurrent, s_log_level],
                outputs=settings_output,
            )
            restart_btn.click(fn=do_restart, outputs=settings_output)

    return app


def launch_web(port: int = 7860, share: bool = False):
    """Entry point called by main.py."""
    app = create_app()
    app.launch(
        server_name="0.0.0.0",
        server_port=port,
        share=share,
    )


if __name__ == "__main__":
    launch_web(port=config.gradio_port, share=config.gradio_share)
