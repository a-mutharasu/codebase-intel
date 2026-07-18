"""CLI entry point — the developer-facing interface.

Commands:
- init: Initialize codebase-intel for a project
- analyze: Build or update the code graph
- query: Query the graph, decisions, or contracts
- drift: Run drift detection
- mine: Mine git history for decision candidates
- serve: Start the MCP server
- status: Show component health

Design:
- Uses Typer for declarative CLI definition
- Rich for pretty output (tables, panels, progress bars)
- All heavy operations are async (run via asyncio.run)
- Commands degrade gracefully if components aren't initialized
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

if TYPE_CHECKING:
    from codebase_intel.core.config import ProjectConfig

app = typer.Typer(
    name="codebase-intel",
    help="Codebase Intelligence Platform — structured context for AI coding agents",
    no_args_is_help=True,
)
console = Console()
# stdout is the MCP stdio transport when serving — status noise must go to stderr instead
stderr_console = Console(stderr=True)
logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler()],
    )


# -------------------------------------------------------------------
# init
# -------------------------------------------------------------------


@app.command()
def init(
    path: Annotated[
        Path,
        typer.Argument(help="Project root directory"),
    ] = Path("."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Initialize codebase-intel for a project.

    Creates the .codebase-intel directory, builds the initial code graph,
    and generates starter configuration and contract templates.
    """
    _setup_logging(verbose)
    project_root = path.resolve()

    if not project_root.is_dir():
        console.print(f"[red]Not a directory: {project_root}[/red]")
        raise typer.Exit(1)

    console.print(
        Panel(
            f"Initializing codebase-intel for [bold]{project_root.name}[/bold]",
            title="codebase-intel init",
        )
    )

    asyncio.run(_init_async(project_root, verbose))


async def _init_async(project_root: Path, verbose: bool) -> None:
    from codebase_intel.core.config import ProjectConfig

    # Create config
    config = ProjectConfig(project_root=project_root)
    config.ensure_dirs()

    # Save default config
    import yaml

    config_file = project_root / ".codebase-intel" / "config.yaml"
    if not config_file.exists():
        config_file.write_text(
            yaml.dump(config.to_yaml_dict(), default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        console.print(f"  Created config: {config_file.relative_to(project_root)}")

    # Create starter contract template
    from codebase_intel.contracts.registry import ContractRegistry

    registry = ContractRegistry(config.contracts, project_root)
    template_path = await registry.create_template(
        "project-rules", f"{project_root.name} Quality Rules"
    )
    console.print(f"  Created contract template: {template_path.relative_to(project_root)}")

    # Build initial graph
    console.print()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Building code graph...", total=None)

        from codebase_intel.graph.builder import GraphBuilder
        from codebase_intel.graph.storage import GraphStorage

        async with GraphStorage.open(config.graph, project_root) as storage:
            builder = GraphBuilder(config, storage)
            build_progress = await builder.full_build()

        progress.update(task, description="Done!", completed=True)

    # Summary
    table = Table(title="Initialization Summary")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Files processed", str(build_progress.processed))
    table.add_row("Files skipped", str(build_progress.skipped))
    table.add_row("Nodes created", str(build_progress.nodes_created))
    table.add_row("Edges created", str(build_progress.edges_created))
    table.add_row("Warnings", str(len(build_progress.warnings)))
    console.print(table)

    if build_progress.warnings:
        console.print(f"\n[yellow]Warnings ({len(build_progress.warnings)}):[/yellow]")
        for w in build_progress.warnings[:10]:
            console.print(f"  {w}")
        if len(build_progress.warnings) > 10:
            console.print(f"  ... and {len(build_progress.warnings) - 10} more")

    # Mine git history for decisions
    try:
        from codebase_intel.decisions.miner import GitMiner

        miner = GitMiner(config.decisions, project_root)
        candidates = await miner.mine_commits(max_commits=200)
        if candidates:
            console.print(
                f"\n[cyan]Found {len(candidates)} decision candidates in git history.[/cyan]"
            )
            console.print("  Run `codebase-intel mine` to review and save them.")
    except Exception:
        # Mining is best-effort during init — a missing or odd git repo must not block setup
        logger.debug("Git decision mining skipped", exc_info=True)

    console.print(
        "\n[green]Initialization complete![/green] The MCP server is ready: `codebase-intel serve`"
    )


# -------------------------------------------------------------------
# analyze
# -------------------------------------------------------------------


@app.command()
def analyze(
    path: Annotated[Path, typer.Argument(help="Project root")] = Path("."),
    incremental: bool = typer.Option(
        False, "--incremental", "-i", help="Only re-parse changed files"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Build or update the code graph."""
    _setup_logging(verbose)
    asyncio.run(_analyze_async(path.resolve(), incremental))


async def _analyze_async(project_root: Path, incremental: bool) -> None:
    from codebase_intel.core.config import ProjectConfig
    from codebase_intel.graph.builder import GraphBuilder
    from codebase_intel.graph.storage import GraphStorage

    config = ProjectConfig(project_root=project_root)

    if not config.graph.db_path.parent.exists():
        console.print("[red]Not initialized. Run `codebase-intel init` first.[/red]")
        raise typer.Exit(1)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        mode = "incremental" if incremental else "full"
        task = progress.add_task(f"Running {mode} analysis...", total=None)

        async with GraphStorage.open(config.graph, project_root) as storage:
            builder = GraphBuilder(config, storage)
            if incremental:
                result = await builder.incremental_build()
            else:
                result = await builder.full_build()

        progress.update(task, description="Done!", completed=True)

    console.print(
        f"[green]Analysis complete:[/green] "
        f"{result.processed} files, {result.nodes_created} nodes, "
        f"{result.edges_created} edges"
    )


# -------------------------------------------------------------------
# drift
# -------------------------------------------------------------------


@app.command()
def drift(
    path: Annotated[Path, typer.Argument(help="Project root")] = Path("."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run drift detection across all components."""
    _setup_logging(verbose)
    asyncio.run(_drift_async(path.resolve()))


async def _drift_async(project_root: Path) -> None:
    from codebase_intel.core.config import ProjectConfig
    from codebase_intel.decisions.store import DecisionStore
    from codebase_intel.drift.detector import DriftDetector

    config = ProjectConfig(project_root=project_root)
    decision_store = DecisionStore(config.decisions, project_root)

    detector = DriftDetector(
        config=config.drift,
        project_root=project_root,
        decision_store=decision_store,
    )

    report = await detector.full_check()

    # Display results
    level_colors = {
        "none": "green",
        "low": "cyan",
        "medium": "yellow",
        "high": "red",
        "critical": "bold red",
    }

    color = level_colors.get(report.overall_level.value, "white")
    console.print(
        Panel(
            f"Overall: [{color}]{report.overall_level.value.upper()}[/{color}]\n{report.summary}",
            title="Drift Report",
        )
    )

    if report.items:
        table = Table(title="Drift Items")
        table.add_column("Level", style="bold")
        table.add_column("Component")
        table.add_column("Description")
        table.add_column("Fix")

        for item in report.items[:20]:
            level_color = level_colors.get(item.level.value, "white")
            table.add_row(
                f"[{level_color}]{item.level.value}[/{level_color}]",
                item.component,
                item.description[:80],
                item.remediation[:50] if item.remediation else "",
            )
        console.print(table)

        if len(report.items) > 20:
            console.print(f"\n... and {len(report.items) - 20} more items")

    if report.rot_detected:
        console.print(
            f"\n[bold red]CONTEXT ROT DETECTED:[/bold red] "
            f"{report.rot_percentage:.0%} of records are stale. "
            f"Consider running a team review session."
        )


# -------------------------------------------------------------------
# mine
# -------------------------------------------------------------------


@app.command()
def mine(
    path: Annotated[Path, typer.Argument(help="Project root")] = Path("."),
    max_commits: int = typer.Option(200, "--max", "-m", help="Max commits to scan"),
    save: bool = typer.Option(
        False, "--save", "-s", help="Auto-save candidates as draft decisions"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Mine git history for decision candidates."""
    _setup_logging(verbose)
    asyncio.run(_mine_async(path.resolve(), max_commits, save))


async def _mine_async(project_root: Path, max_commits: int, save: bool) -> None:
    from codebase_intel.core.config import ProjectConfig
    from codebase_intel.decisions.miner import GitMiner
    from codebase_intel.decisions.store import DecisionStore

    config = ProjectConfig(project_root=project_root)
    miner = GitMiner(config.decisions, project_root)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"Mining last {max_commits} commits...", total=None)
        candidates = await miner.mine_commits(max_commits=max_commits)
        progress.update(task, description="Done!", completed=True)

    if not candidates:
        console.print("[yellow]No decision candidates found in recent commits.[/yellow]")
        return

    table = Table(title=f"Decision Candidates ({len(candidates)} found)")
    table.add_column("#", style="dim")
    table.add_column("Title")
    table.add_column("Source")
    table.add_column("Confidence", justify="right")
    table.add_column("Keywords")

    for i, c in enumerate(candidates[:20], 1):
        table.add_row(
            str(i),
            c.title[:60],
            f"{c.source_type}:{c.source_ref[:8]}",
            f"{c.confidence:.0%}",
            ", ".join(c.keywords_matched[:3]),
        )
    console.print(table)

    if save:
        store = DecisionStore(config.decisions, project_root)
        saved = 0
        for candidate in candidates:
            decision_id = await store.next_id()
            record = candidate.to_decision_record(decision_id)
            await store.save(record)
            saved += 1
        console.print(
            f"\n[green]Saved {saved} draft decisions.[/green] "
            "Review them in .codebase-intel/decisions/"
        )
    else:
        console.print("\nRun with `--save` to save these as draft decisions.")


# -------------------------------------------------------------------
# detect-patterns
# -------------------------------------------------------------------


@app.command(name="detect-patterns")
def detect_patterns(
    path: Annotated[Path, typer.Argument(help="Project root")] = Path("."),
    save: bool = typer.Option(False, "--save", "-s", help="Save as draft contract"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Auto-detect code patterns and generate quality contracts."""
    _setup_logging(verbose)
    asyncio.run(_detect_patterns_async(path.resolve(), save))


async def _detect_patterns_async(project_root: Path, save: bool) -> None:
    from codebase_intel.contracts.auto_generator import AutoContractGenerator

    generator = AutoContractGenerator(project_root)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Analyzing codebase patterns...", total=None)
        patterns = await generator.analyze()
        progress.update(task, description="Done!", completed=True)

    if not patterns:
        console.print("[yellow]No strong patterns detected (need 70%+ consistency).[/yellow]")
        return

    console.print(
        Panel(
            f"Found [bold]{len(patterns)}[/bold] consistent patterns in your codebase",
            title="Auto-Detected Patterns",
        )
    )

    table = Table()
    table.add_column("#", style="dim")
    table.add_column("Pattern", style="bold")
    table.add_column("Confidence", justify="right", style="cyan")
    table.add_column("Follows", justify="right", style="green")
    table.add_column("Violates", justify="right", style="red")
    table.add_column("Type")

    for i, p in enumerate(patterns, 1):
        table.add_row(
            str(i),
            p.name,
            f"{p.confidence:.0%}",
            str(p.occurrences),
            str(p.violations),
            p.kind.value,
        )
    console.print(table)

    if save:
        contract = generator.generate_contract(patterns)
        import yaml

        contracts_dir = project_root / ".codebase-intel" / "contracts"
        contracts_dir.mkdir(parents=True, exist_ok=True)
        out_path = contracts_dir / "auto-detected.yaml"
        data = contract.model_dump(mode="json")
        out_path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        console.print(f"\n[green]Saved contract to {out_path.relative_to(project_root)}[/green]")
        console.print("Review the rules and adjust severity levels as needed.")
    else:
        console.print("\nRun with `--save` to generate a draft contract from these patterns.")


# -------------------------------------------------------------------
# crossrepo
# -------------------------------------------------------------------


@app.command(name="crossrepo")
def crossrepo(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="Paths to service repos (space-separated). Omit with --all."),
    ] = None,
    all_registered: bool = typer.Option(
        False, "--all", "-A", help="Scan all globally registered projects"
    ),
    impact: str = typer.Option("", "--impact", "-i", help="Service ID to check impact for"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Scan multiple repos and map cross-service dependencies.

    Provide paths explicitly or use --all to scan all registered projects:
        codebase-intel crossrepo /svc1 /svc2 /svc3
        codebase-intel crossrepo --all
    """
    _setup_logging(verbose)

    from codebase_intel.crossrepo.registry import CrossRepoRegistry

    # Resolve paths: explicit args or global registry
    if all_registered:
        from codebase_intel.workspace.registry import GlobalRegistry

        global_registry = GlobalRegistry()
        resolved_paths = global_registry.get_all_paths()
        if not resolved_paths:
            console.print("[red]No projects registered.[/red] Use `codebase-intel register` first.")
            raise typer.Exit(1)
        console.print(f"[cyan]Scanning {len(resolved_paths)} registered project(s)...[/cyan]")
    elif paths:
        resolved_paths = [p.resolve() for p in paths]
    else:
        console.print("[red]Provide repo paths or use --all to scan registered projects.[/red]")
        raise typer.Exit(1)

    registry_dir = resolved_paths[0].parent / ".codebase-intel-crossrepo"
    registry = CrossRepoRegistry(registry_dir)

    # Scan all services
    for resolved in resolved_paths:
        if not resolved.is_dir():
            console.print(f"[red]Not a directory: {resolved}[/red]")
            continue

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task(f"Scanning {resolved.name}...", total=None)
            result = registry.register_service(resolved)
            progress.update(
                task, description=f"Done: {result.to_dict()['endpoints']} endpoints", completed=True
            )

    # Resolve dependencies
    console.print()
    deps = registry.resolve_dependencies()
    console.print(f"[green]Resolved {len(deps)} cross-service dependencies[/green]")

    # Show service map
    service_map = registry.get_service_map()
    table = Table(title="Service Dependency Map")
    table.add_column("Service", style="bold")
    table.add_column("Endpoints", justify="right")
    table.add_column("Outbound Calls", justify="right")
    table.add_column("Depends On", style="cyan")
    table.add_column("Depended On By", style="yellow")

    for sid, info in service_map.items():
        table.add_row(
            sid,
            str(info["endpoints_exposed"]),
            str(info["outbound_calls"]),
            ", ".join(info["depends_on"]) or "—",
            ", ".join(info["depended_on_by"]) or "—",
        )
    console.print(table)

    # Impact analysis if requested
    if impact:
        impacts = registry.impact_analysis(impact)
        if impacts:
            console.print()
            for imp in impacts:
                risk_color = {
                    "critical": "bold red",
                    "high": "red",
                    "medium": "yellow",
                    "low": "green",
                }.get(imp.risk_level, "white")
                console.print(
                    Panel(
                        f"Endpoint: [bold]{imp.changed_endpoint}[/bold]\n"
                        f"Risk: [{risk_color}]{imp.risk_level.upper()}[/{risk_color}] — "
                        f"{imp.affected_count} services affected\n\n"
                        f"{imp.recommendation}",
                        title=f"Impact: {impact}",
                    )
                )
                for dep in imp.affected_services:
                    critical = " [red][CRITICAL][/red]" if dep.is_critical else ""
                    console.print(
                        f"  → {dep.consumer_service}{critical} at "
                        f"{dep.file_path}:{dep.line_number or '?'}"
                    )
        else:
            console.print(f"[green]No cross-service impact found for {impact}[/green]")

    # Save registry
    saved = registry.save()
    console.print(f"\n[dim]Registry saved to {saved}[/dim]")


# -------------------------------------------------------------------
# serve
# -------------------------------------------------------------------


@app.command()
def serve(
    path: Annotated[Path | None, typer.Argument(help="Project root (omit for --auto mode)")] = None,
    auto: bool = typer.Option(
        False, "--auto", "-a", help="Workspace-aware mode: serve all registered projects"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Start the MCP server (stdio transport).

    Single-project mode (default):
        codebase-intel serve /path/to/project

    Multi-project mode (global):
        codebase-intel serve --auto

    In --auto mode, the server routes each request to the correct project
    based on file paths. Register projects first with `codebase-intel register`.
    """
    _setup_logging(verbose)

    from codebase_intel.mcp.server import run_server

    if auto:
        stderr_console.print("[dim]Starting MCP server in workspace-aware mode...[/dim]")
        asyncio.run(run_server(auto_mode=True))
    else:
        project_root = (path or Path(".")).resolve()
        stderr_console.print(f"[dim]Starting MCP server for {project_root.name}...[/dim]")
        asyncio.run(run_server(project_root))


# -------------------------------------------------------------------
# register / unregister / projects (global workspace)
# -------------------------------------------------------------------


@app.command()
def register(
    path: Annotated[Path, typer.Argument(help="Project root to register")] = Path("."),
    name: str | None = typer.Option(None, "--name", "-n", help="Display name for the project"),
    project_id: str | None = typer.Option(
        None, "--id", help="Custom project ID (default: directory name)"
    ),
) -> None:
    """Register a project in the global registry.

    Once registered, the project is available to `codebase-intel serve --auto`
    and `codebase-intel crossrepo scan --all`.

    Examples:
        codebase-intel register /path/to/user-service
        codebase-intel register . --name "Payment API" --id payment-api
    """
    from codebase_intel.workspace.registry import GlobalRegistry

    registry = GlobalRegistry()
    resolved = path.resolve()

    if not resolved.is_dir():
        console.print(f"[red]Not a directory: {resolved}[/red]")
        raise typer.Exit(1)

    entry = registry.register(resolved, project_id=project_id, name=name)

    init_status = (
        "[green]initialized[/green]" if entry.initialized else "[yellow]not initialized[/yellow]"
    )
    console.print(
        f"[green]Registered[/green] [bold]{entry.project_id}[/bold] "
        f"→ {entry.root_path} ({init_status})"
    )

    if not entry.initialized:
        console.print(f"[dim]Run `codebase-intel init {resolved}` to build the code graph.[/dim]")


@app.command()
def unregister(
    project_id: Annotated[str, typer.Argument(help="Project ID to remove")],
) -> None:
    """Remove a project from the global registry."""
    from codebase_intel.workspace.registry import GlobalRegistry

    registry = GlobalRegistry()

    if registry.unregister(project_id):
        console.print(f"[green]Unregistered[/green] [bold]{project_id}[/bold]")
    else:
        console.print(f"[red]Project '{project_id}' not found in registry[/red]")
        raise typer.Exit(1)


@app.command()
def projects() -> None:
    """List all globally registered projects."""
    from codebase_intel.workspace.registry import GlobalRegistry

    registry = GlobalRegistry()
    all_projects = registry.get_all()

    if not all_projects:
        console.print("[yellow]No projects registered.[/yellow]")
        console.print("Register with: `codebase-intel register /path/to/project`")
        return

    table = Table(title="Registered Projects")
    table.add_column("ID", style="bold")
    table.add_column("Name")
    table.add_column("Path")
    table.add_column("Status")
    table.add_column("Initialized")
    table.add_column("Last Accessed", style="dim")

    for entry in all_projects:
        exists = entry.is_valid
        status = "[green]OK[/green]" if exists else "[red]MISSING[/red]"
        init_status = "[green]Yes[/green]" if entry.initialized else "[yellow]No[/yellow]"
        last_accessed = (
            entry.last_accessed.strftime("%Y-%m-%d %H:%M") if entry.last_accessed else "—"
        )

        table.add_row(
            entry.project_id,
            entry.name,
            entry.root_path,
            status,
            init_status,
            last_accessed,
        )

    console.print(table)
    console.print(
        f"\n[dim]{len(all_projects)} project(s) registered. "
        f"Use `codebase-intel serve --auto` to serve all.[/dim]"
    )


# -------------------------------------------------------------------
# intent
# -------------------------------------------------------------------


@app.command()
def intent(
    path: Annotated[Path, typer.Argument(help="Project root")] = Path("."),
    verify: bool = typer.Option(
        False, "--verify", "-v", help="Run verification on all active intents"
    ),
) -> None:
    """Show and verify tracked intents — what was requested vs what's delivered."""
    asyncio.run(_intent_async(path.resolve(), verify))


async def _intent_async(project_root: Path, verify: bool) -> None:
    from codebase_intel.intent.store import IntentStore
    from codebase_intel.intent.verifier import IntentVerifier

    intents_dir = project_root / ".codebase-intel" / "intents"
    store = IntentStore(intents_dir)
    all_intents = store.load_all()

    if not all_intents:
        console.print("[yellow]No intents tracked yet.[/yellow]")
        console.print("Use the MCP tool `set_intent` to capture goals with acceptance criteria.")
        return

    if verify:
        verifier = IntentVerifier(project_root)
        console.print()

        for intent_obj in all_intents:
            if intent_obj.status.value not in ("active", "partial"):
                continue

            report = await verifier.verify(intent_obj)

            # Update stored state
            updated_criteria = []
            for r in report.results:
                updated_criteria.append(
                    r.criterion.model_copy(
                        update={
                            "verified": r.passed,
                            "failure_reason": r.failure_reason,
                        }
                    )
                )
            store.update_criteria(intent_obj.id, updated_criteria)
            store.update_status(intent_obj.id, report.status)

            # Display
            status_color = {
                "verified": "green",
                "partial": "yellow",
                "failed": "red",
                "active": "cyan",
            }.get(report.status.value, "white")

            console.print(
                Panel(
                    f"[bold]{intent_obj.title}[/bold]\n"
                    f"Status: [{status_color}]{report.status.value.upper()}[/{status_color}] — "
                    f"{report.passed_count}/{report.total} criteria met",
                    title=f"Intent {intent_obj.id}",
                )
            )

            for r in report.results:
                icon = "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]"
                console.print(f"  {icon} {r.criterion.description}")
                if r.failure_reason:
                    console.print(f"       [dim]{r.failure_reason}[/dim]")
            console.print()

    else:
        # Just show status
        table = Table(title="Tracked Intents")
        table.add_column("ID", style="dim")
        table.add_column("Title", style="bold")
        table.add_column("Status")
        table.add_column("Progress", justify="right")
        table.add_column("Gaps", justify="right", style="red")

        for i in all_intents:
            status_color = {
                "verified": "green",
                "partial": "yellow",
                "failed": "red",
                "active": "cyan",
                "abandoned": "dim",
            }.get(i.status.value, "white")

            table.add_row(
                i.id,
                i.title[:50],
                f"[{status_color}]{i.status.value}[/{status_color}]",
                f"{i.criteria_met}/{i.criteria_total} ({i.completion_pct:.0f}%)",
                str(len(i.gaps)),
            )
        console.print(table)
        console.print("\nRun with `--verify` to check all criteria against the actual codebase.")


# -------------------------------------------------------------------
# status
# -------------------------------------------------------------------


@app.command()
def status(
    path: Annotated[Path, typer.Argument(help="Project root")] = Path("."),
) -> None:
    """Show component health status."""
    project_root = path.resolve()
    intel_dir = project_root / ".codebase-intel"

    table = Table(title=f"codebase-intel status: {project_root.name}")
    table.add_column("Component", style="bold")
    table.add_column("Status")
    table.add_column("Details")

    # Check config
    config_file = intel_dir / "config.yaml"
    if config_file.exists():
        table.add_row("Config", "[green]OK[/green]", str(config_file.relative_to(project_root)))
    else:
        table.add_row("Config", "[red]Missing[/red]", "Run `codebase-intel init`")

    # Check graph
    graph_db = intel_dir / "graph.db"
    if graph_db.exists():
        size_mb = graph_db.stat().st_size / (1024 * 1024)
        table.add_row("Code Graph", "[green]OK[/green]", f"{size_mb:.1f} MB")
    else:
        table.add_row("Code Graph", "[red]Not built[/red]", "Run `codebase-intel init`")

    # Check decisions
    decisions_dir = intel_dir / "decisions"
    if decisions_dir.exists():
        count = len(list(decisions_dir.glob("*.yaml")))
        table.add_row("Decisions", "[green]OK[/green]", f"{count} records")
    else:
        table.add_row("Decisions", "[yellow]Empty[/yellow]", "Run `codebase-intel mine`")

    # Check contracts
    contracts_dir = intel_dir / "contracts"
    if contracts_dir.exists():
        count = len(list(contracts_dir.glob("*.yaml")))
        table.add_row("Contracts", "[green]OK[/green]", f"{count} files + builtins")
    else:
        table.add_row("Contracts", "[yellow]Builtins only[/yellow]", "")

    console.print(table)


# -------------------------------------------------------------------
# benchmark
# -------------------------------------------------------------------


@app.command()
def benchmark(
    path: Annotated[Path, typer.Argument(help="Project root")] = Path("."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run benchmarks — measure token efficiency on this project."""
    _setup_logging(verbose)
    asyncio.run(_benchmark_async(path.resolve()))


async def _benchmark_async(project_root: Path) -> None:
    from codebase_intel.analytics.benchmark import BenchmarkRunner
    from codebase_intel.analytics.tracker import AnalyticsTracker

    tracker = AnalyticsTracker(project_root / ".codebase-intel" / "analytics.db")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Running benchmarks...", total=None)

        runner = BenchmarkRunner(project_root)
        report = await runner.run(tracker=tracker)

        progress.update(task, description="Done!", completed=True)

    # Display results
    console.print()
    console.print(
        Panel(
            f"[bold]{report.repo_name}[/bold]\n"
            f"{report.total_files} files | {report.total_nodes} nodes | "
            f"{report.total_edges} edges | Built in {report.build_time_ms:.0f}ms",
            title="Benchmark Results",
        )
    )

    table = Table(title="Token Efficiency — Before vs After")
    table.add_column("Scenario", style="bold")
    table.add_column("Naive\n(without tool)", justify="right", style="red")
    table.add_column("Graph Only", justify="right", style="yellow")
    table.add_column("Full Pipeline", justify="right", style="green")
    table.add_column("Reduction", justify="right", style="bold cyan")
    table.add_column("Multiplier", justify="right", style="bold cyan")
    table.add_column("Decisions", justify="right")
    table.add_column("Contracts", justify="right")

    for s in report.scenarios:
        table.add_row(
            s.name,
            f"{s.naive_tokens:,}",
            f"{s.graph_tokens:,}",
            f"{s.full_tokens:,}",
            f"{s.naive_vs_full_reduction:.0f}%",
            f"{s.multiplier:.1f}x",
            str(s.decisions_surfaced),
            str(s.contracts_applied),
        )

    # Average row
    table.add_row(
        "[bold]Average[/bold]",
        "",
        "",
        "",
        f"[bold]{report.avg_reduction_pct:.0f}%[/bold]",
        f"[bold]{report.avg_multiplier:.1f}x[/bold]",
        f"[bold]{report.total_decisions_surfaced}[/bold]",
        f"[bold]{report.total_contracts_applied}[/bold]",
        style="on grey23",
    )
    console.print(table)

    # Before/After summary
    console.print()
    console.print("[bold]What this means:[/bold]")
    console.print(
        f"  Without codebase-intel: agent reads "
        f"[red]{report.scenarios[0].naive_tokens:,}[/red] tokens "
        f"(every file in the directory)"
    ) if report.scenarios else None
    console.print(
        f"  With codebase-intel:    agent reads "
        f"[green]{report.scenarios[0].full_tokens:,}[/green] tokens "
        f"(only what matters + decisions + contracts)"
    ) if report.scenarios else None
    if report.total_decisions_surfaced > 0:
        console.print(
            f"  [cyan]{report.total_decisions_surfaced}[/cyan] architectural decisions surfaced "
            f"that the agent would have missed"
        )
    if report.total_contracts_applied > 0:
        console.print(
            f"  [cyan]{report.total_contracts_applied}[/cyan] quality rules enforced "
            f"before code was generated"
        )

    tracker.close()


# -------------------------------------------------------------------
# intent-eval
# -------------------------------------------------------------------


@app.command(name="intent-eval")
def intent_eval(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="Project root(s) to evaluate. Omit for the current directory."),
    ] = None,
    max_cases: int = typer.Option(10, "--max-cases", "-n", help="Max eval cases per repo"),
    budget_tokens: int = typer.Option(2000, "--budget", "-b", help="Bootstrap token budget"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Intent-graph eval — token AND correctness A/B vs the v1 baseline (T-006).

    Held-out questions are synthesized from each intent node's WHY text with
    its own title terms removed, so the harness must find the node by how its
    reasoning is described, not by an exact name match. Reports whether
    bootstrap surfaces the right precedent (accuracy) and how many fewer
    tokens that costs vs reading files naively (reduction).

    Pass multiple repo paths to run the same eval across several projects
    and get a combined report — that is how a broader A/B beyond this repo
    gets built once more repos are available.
    """
    _setup_logging(verbose)
    targets = [p.resolve() for p in paths] if paths else [Path(".").resolve()]
    asyncio.run(_intent_eval_async(targets, max_cases, budget_tokens))


async def _intent_eval_async(targets: list[Path], max_cases: int, budget_tokens: int) -> None:
    from codebase_intel.analytics.eval import IntentEvalRunner
    from codebase_intel.analytics.tracker import AnalyticsTracker

    all_results = []
    for project_root in targets:
        tracker = AnalyticsTracker(project_root / ".codebase-intel" / "analytics.db")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task(f"Evaluating {project_root.name}...", total=None)
            runner = IntentEvalRunner(project_root)
            report = await runner.run(
                tracker=tracker, max_cases=max_cases, budget_tokens=budget_tokens
            )
            progress.update(task, description="Done!", completed=True)

        tracker.close()
        all_results.append(report)

        console.print()
        if not report.results:
            console.print(f"[yellow]{report.repo_name}:[/yellow] {report.warnings[0]}")
            continue

        table = Table(title=f"Intent Eval — {report.repo_name}")
        table.add_column("Query (why, title-terms held out)", style="dim")
        table.add_column("Found", justify="center")
        table.add_column("Rank", justify="right")
        table.add_column("Naive", justify="right", style="red")
        table.add_column("Citation", justify="right", style="green")
        table.add_column("Reduction", justify="right", style="bold cyan")

        for r in report.results:
            mark = (
                "[green]✓[/green]"
                if r.correct
                else ("[yellow]~[/yellow]" if r.found else "[red]✗[/red]")
            )
            table.add_row(
                r.query[:50],
                mark,
                str(r.rank) if r.rank else "-",
                f"{r.naive_tokens:,}",
                f"{r.citation_tokens:,}",
                f"{r.reduction_pct:.0f}%",
            )
        console.print(table)
        console.print(
            f"[bold]Accuracy@1:[/bold] {report.accuracy_at_1:.0%}  "
            f"[bold]Accuracy@3:[/bold] {report.accuracy_at_3:.0%}  "
            f"[bold]Avg reduction:[/bold] {report.avg_reduction_pct:.0f}% "
            f"({report.avg_multiplier:.1f}x)"
        )

    if len(all_results) > 1:
        scored = [r for r in all_results if r.results]
        if scored:
            combined_acc1 = sum(r.accuracy_at_1 for r in scored) / len(scored)
            combined_reduction = sum(r.avg_reduction_pct for r in scored) / len(scored)
            console.print()
            console.print(
                Panel(
                    f"Across {len(scored)} repos: "
                    f"[bold]{combined_acc1:.0%}[/bold] avg accuracy@1, "
                    f"[bold]{combined_reduction:.0f}%[/bold] avg token reduction",
                    title="Combined A/B",
                )
            )


# -------------------------------------------------------------------
# dashboard
# -------------------------------------------------------------------


@app.command()
def dashboard(
    path: Annotated[Path, typer.Argument(help="Project root")] = Path("."),
) -> None:
    """Show live efficiency dashboard — proves value over time."""
    project_root = path.resolve()
    analytics_db = project_root / ".codebase-intel" / "analytics.db"

    if not analytics_db.exists():
        console.print(
            "[yellow]No analytics data yet.[/yellow] Run `codebase-intel benchmark` "
            "or use the MCP server to start collecting data."
        )
        raise typer.Exit(0)

    from codebase_intel.analytics.tracker import AnalyticsTracker

    tracker = AnalyticsTracker(analytics_db)
    stats = tracker.get_lifetime_stats()
    comparison = tracker.get_before_after_comparison()
    benchmarks = tracker.get_benchmark_results()

    # --- Header ---
    console.print()
    console.print(
        Panel(
            f"[bold]codebase-intel Dashboard[/bold] — {project_root.name}",
            subtitle="Live efficiency tracking",
        )
    )

    # --- Before/After ---
    if comparison.get("has_data"):
        console.print()
        before_after = Table(title="Before vs After — Token Efficiency")
        before_after.add_column("Metric", style="bold")
        before_after.add_column("Without\ncodebase-intel", justify="right", style="red")
        before_after.add_column("With\ncodebase-intel", justify="right", style="green")
        before_after.add_column("Improvement", justify="right", style="bold cyan")

        b = comparison["before"]
        a = comparison["after"]
        imp = comparison["improvement"]

        before_after.add_row(
            "Avg tokens / request",
            f"{b['tokens_per_request']:,}",
            f"{a['tokens_per_request']:,}",
            f"{imp['token_reduction_pct']:.0f}% fewer ({imp['multiplier']:.1f}x)",
        )
        before_after.add_row(
            "Decisions available",
            "0 (blind)",
            f"{a['decisions_available']}",
            f"{imp['decisions_that_prevented_mistakes']} surfaced",
        )
        before_after.add_row(
            "Contract enforcement",
            "0 (no guardrails)",
            f"{a['contract_checks']}",
            f"{imp['violations_caught_before_generation']} rules enforced",
        )
        before_after.add_row(
            "Drift awareness",
            "None",
            "Active",
            "Stale context detected",
        )
        before_after.add_row(
            "Knows WHY code exists",
            "[red]No[/red]",
            "[green]Yes[/green]",
            "Decision provenance",
        )
        console.print(before_after)

    # --- Lifetime stats ---
    tokens = stats["tokens"]
    quality = stats["context_quality"]

    console.print()
    lifetime = Table(title="Lifetime Statistics")
    lifetime.add_column("Metric", style="bold")
    lifetime.add_column("Value", justify="right")

    lifetime.add_row("Total context requests", f"{stats['total_requests']:,}")
    lifetime.add_row("Total tokens saved", f"[green]{tokens['total_saved']:,}[/green]")
    lifetime.add_row("Token reduction", f"[cyan]{tokens['reduction_pct']:.0f}%[/cyan]")
    lifetime.add_row("Decisions surfaced", f"{quality['decisions_surfaced']:,}")
    lifetime.add_row("Contracts applied", f"{quality['contracts_applied']:,}")
    lifetime.add_row("Drift warnings", f"{quality['drift_warnings']:,}")
    lifetime.add_row("Avg assembly time", f"{stats['performance']['avg_assembly_ms']:.0f}ms")
    console.print(lifetime)

    # --- Benchmark history ---
    if benchmarks:
        console.print()
        bench_table = Table(title="Benchmark History")
        bench_table.add_column("Date", style="dim")
        bench_table.add_column("Project", style="bold")
        bench_table.add_column("Files", justify="right")
        bench_table.add_column("Nodes", justify="right")
        bench_table.add_column("Reduction", justify="right", style="cyan")
        bench_table.add_column("Build Time", justify="right")

        for b in benchmarks[:10]:
            bench_table.add_row(
                b["timestamp"][:10],
                b["repo_name"],
                str(b["total_files"]),
                str(b["total_nodes"]),
                f"{b['avg_token_reduction_pct']:.0f}%",
                f"{b['build_time_ms']:.0f}ms",
            )
        console.print(bench_table)

    # --- Daily trend (sparkline-style) ---
    daily = tracker.get_daily_trend(14)
    if daily:
        console.print()
        console.print("[bold]Daily Token Savings (last 14 days)[/bold]")
        max_saved = max((d["total_naive_tokens"] - d["total_full_tokens"]) for d in daily) or 1
        for d in daily:
            saved = d["total_naive_tokens"] - d["total_full_tokens"]
            bar_len = int((saved / max_saved) * 40)
            bar = "█" * bar_len
            console.print(
                f"  {d['date']}  [green]{bar}[/green] "
                f"{saved:,} tokens saved ({d['total_requests']} requests)"
            )

    tracker.close()


# -------------------------------------------------------------------
# Intent graph (v2)
# -------------------------------------------------------------------

_HOOK_MARKER = "# codebase-intel intent gate"
_HOOK_SCRIPT = f"""#!/bin/sh
{_HOOK_MARKER}
codebase-intel intent-gate || exit $?
"""


def _intentgraph_config(path: Path) -> ProjectConfig:
    from codebase_intel.core.config import ProjectConfig

    return ProjectConfig(project_root=path)


@app.command()
def why(
    title: Annotated[str, typer.Argument(help="Short name for the decision")],
    reason: Annotated[str, typer.Argument(help="The why — rationale behind the decision")],
    path: Annotated[Path, typer.Option("--path", "-p", help="Project root")] = Path("."),
    files: Annotated[
        list[str] | None, typer.Option("--file", "-f", help="Code files this why anchors to")
    ] = None,
    level: int = typer.Option(3, "--level", "-l", min=0, max=3, help="0=system … 3=decision"),
    supersedes: str = typer.Option("", "--supersedes", help="Node ID this why replaces"),
    tag: Annotated[list[str] | None, typer.Option("--tag", "-t")] = None,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Record a why in the intent graph (human capture → confirmed trust)."""
    _setup_logging(verbose)
    from codebase_intel.intentgraph import CaptureService, IntentJournal, IntentLevel, TrustTier

    config = _intentgraph_config(path)
    journal = IntentJournal(config.intentgraph.journal_dir)
    capture = CaptureService(journal, config.intentgraph.db_path, actor="cli")

    result = asyncio.run(
        capture.record_why(
            title=title,
            why=reason,
            level=IntentLevel(level),
            file_paths=files or [],
            tags=tag or [],
            author="human",
            trust=TrustTier.CONFIRMED,
            supersedes=supersedes or None,
        )
    )
    console.print(f"[green]Recorded[/green] {result.node.node_id}: {result.node.title}")
    for warning in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}")


@app.command(name="intent-gate")
def intent_gate(
    path: Annotated[Path, typer.Argument(help="Project root")] = Path("."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Pre-commit gate: warn (or block, if configured hard) on unexplained structural changes."""
    _setup_logging(verbose)
    from codebase_intel.intentgraph import run_gate

    config = _intentgraph_config(path)
    result = asyncio.run(
        run_gate(config.project_root, config.intentgraph, hard=config.intentgraph.gate_hard)
    )
    for message in result.messages:
        console.print(f"[yellow]{message}[/yellow]")
    if not result.passed:
        raise typer.Exit(code=1)


@app.command(name="install-hooks")
def install_hooks(
    path: Annotated[Path, typer.Argument(help="Project root")] = Path("."),
) -> None:
    """Install the pre-commit intent gate into .git/hooks."""
    hooks_dir = path.resolve() / ".git" / "hooks"
    if not hooks_dir.is_dir():
        console.print("[red]No .git/hooks directory — is this a git repo?[/red]")
        raise typer.Exit(code=1)

    hook_path = hooks_dir / "pre-commit"
    if hook_path.exists() and _HOOK_MARKER not in hook_path.read_text(encoding="utf-8"):
        console.print(
            "[yellow]A pre-commit hook already exists. Add this line to it manually:[/yellow]\n"
            "  codebase-intel intent-gate || exit $?"
        )
        raise typer.Exit(code=1)

    hook_path.write_text(_HOOK_SCRIPT, encoding="utf-8")
    hook_path.chmod(0o755)
    console.print(f"[green]Installed[/green] intent gate at {hook_path}")


@app.command(name="intent-migrate")
def intent_migrate(
    path: Annotated[Path, typer.Argument(help="Project root")] = Path("."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Migrate v1 decision records into the intent graph (safe to re-run)."""
    _setup_logging(verbose)

    async def _run() -> None:
        from codebase_intel.decisions.store import DecisionStore
        from codebase_intel.intentgraph import IntentGraphStore, IntentJournal
        from codebase_intel.intentgraph.migrate import migrate_v1_decisions

        config = _intentgraph_config(path)
        decision_store = DecisionStore(config.decisions, config.project_root)
        journal = IntentJournal(config.intentgraph.journal_dir)
        async with IntentGraphStore.open(config.intentgraph.db_path) as store:
            result = await migrate_v1_decisions(decision_store, journal, store)
        console.print(
            f"[green]Migrated {result.migrated}[/green] "
            f"(skipped {result.skipped}, superseded {result.superseded}, edges {result.edges})"
        )
        for warning in result.warnings:
            console.print(f"[yellow]warning:[/yellow] {warning}")

    asyncio.run(_run())


@app.command(name="intent-check")
def intent_check(
    path: Annotated[Path, typer.Argument(help="Project root")] = Path("."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Validate intent anchors against the code: repair moves, flag drift, report health."""
    _setup_logging(verbose)

    async def _run() -> None:
        from codebase_intel.intentgraph import IntentGraphStore, IntentJournal
        from codebase_intel.intentgraph.anchors import (
            AnchorRepairer,
            coverage_report,
            find_conflicts,
        )

        config = _intentgraph_config(path)
        journal = IntentJournal(config.intentgraph.journal_dir)
        async with IntentGraphStore.open(config.intentgraph.db_path) as store:
            await store.materialize(journal)
            report = await AnchorRepairer(journal, store).validate_and_repair(config.project_root)
            conflicts = await find_conflicts(store)
            health = await coverage_report(store)

        table = Table(title="Anchor validation")
        table.add_column("Metric", style="bold")
        table.add_column("Count", justify="right")
        table.add_row("Nodes checked", str(report.nodes_checked))
        table.add_row("Anchors checked", str(report.anchors_checked))
        table.add_row("Valid", f"[green]{report.valid}[/green]")
        table.add_row("Hash backfilled", str(report.backfilled))
        table.add_row("Auto-repaired (moved)", f"[cyan]{report.repaired}[/cyan]")
        table.add_row("Diverged (marked stale)", f"[yellow]{report.diverged}[/yellow]")
        table.add_row("Queued for review", f"[red]{report.queued}[/red]")
        console.print(table)

        for finding in report.findings:
            if finding.outcome in ("repaired", "diverged", "queued"):
                console.print(
                    f"  [{finding.outcome}] {finding.anchor_path}"
                    + (f" → {finding.new_path}" if finding.new_path else "")
                    + (f" ({finding.detail})" if finding.detail else "")
                )

        if conflicts:
            console.print(f"\n[yellow]{len(conflicts)} potential conflicts:[/yellow]")
            for a, b, reason in conflicts[:10]:
                console.print(f"  {a} vs {b}: {reason}")

        console.print(
            f"\nHealth: {health['total_nodes']} nodes, "
            f"{health['stale_pct']}% stale, "
            f"{health['missing_why_backlog']} missing-why, "
            f"{health['orphaned_anchors']} orphaned"
        )

    asyncio.run(_run())


@app.command(name="intent-confirm")
def intent_confirm(
    node_id: Annotated[str, typer.Argument(help="Intent node ID to promote")],
    by: Annotated[str, typer.Option("--by", help="Reviewer name")] = "human",
    owner: str = typer.Option("", "--owner", help="Owning person/team for this subtree"),
    path: Annotated[Path, typer.Option("--path", "-p", help="Project root")] = Path("."),
) -> None:
    """Promote an agent-asserted why to confirmed trust after review."""
    from codebase_intel.intentgraph import CaptureService, IntentJournal

    config = _intentgraph_config(path)
    journal = IntentJournal(config.intentgraph.journal_dir)
    capture = CaptureService(journal, config.intentgraph.db_path, actor="cli")

    node = asyncio.run(capture.promote(node_id, reviewer=by, owner=owner or None))
    if node is None:
        console.print(f"[red]Node {node_id} not found[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]Confirmed[/green] {node.node_id}: {node.title} (by {by})")


@app.command(name="intent-render")
def intent_render(
    path: Annotated[Path, typer.Argument(help="Project root")] = Path("."),
    fmt: str = typer.Option("html", "--format", "-f", help="mermaid | html"),
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Output file (default: stdout)")
    ] = None,
) -> None:
    """Render the intent graph for humans (read-only projection of the same graph)."""

    async def _run() -> str:
        from codebase_intel.intentgraph import IntentJournal
        from codebase_intel.intentgraph.render import IntentRenderer

        config = _intentgraph_config(path)
        renderer = IntentRenderer(
            IntentJournal(config.intentgraph.journal_dir), config.intentgraph.db_path
        )
        if fmt == "mermaid":
            return await renderer.to_mermaid()
        return await renderer.to_html(project_name=config.project_root.name)

    rendered = asyncio.run(_run())
    if output is not None:
        output.write_text(rendered, encoding="utf-8")
        console.print(f"[green]Wrote[/green] {output}")
    else:
        print(rendered)


@app.command(name="intent-rebuild")
def intent_rebuild(
    path: Annotated[Path, typer.Argument(help="Project root")] = Path("."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Rebuild the intent index from the journal (recovery path — always safe)."""
    _setup_logging(verbose)

    async def _run() -> None:
        from codebase_intel.intentgraph import IntentGraphStore, IntentJournal

        config = _intentgraph_config(path)
        journal = IntentJournal(config.intentgraph.journal_dir)
        async with IntentGraphStore.open(config.intentgraph.db_path) as store:
            result = await store.rebuild(journal)
        console.print(
            f"[green]Rebuilt[/green] — applied {result.applied} entries, "
            f"{len(result.errors)} errors"
        )
        for error in result.errors[:10]:
            console.print(f"[yellow]{error}[/yellow]")

    asyncio.run(_run())


if __name__ == "__main__":
    app()
