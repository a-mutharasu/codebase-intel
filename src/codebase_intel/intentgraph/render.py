"""Human projection — render the intent graph for people.

"Serve machines first, render for humans second": these renders are read-only
projections of the same graph agents traverse. Two formats:

- Mermaid: paste into GitHub/Notion/docs; levels as subgraphs, CONTAINS edges
- HTML: dependency-free static page (no CDN, no JS) using nested <details> —
  works offline, in air-gapped repos, and in a plain browser

Edge cases:
- Empty graph: both formats render an honest "no intent recorded yet" shell
- Superseded nodes: rendered struck-through/dimmed with their successor —
  the reversal trail is part of the map, not noise
- Titles with Mermaid-hostile characters (quotes, brackets): sanitized
- Nodes with no CONTAINS parent: grouped under their level, not dropped
"""

from __future__ import annotations

import html as html_mod
from typing import TYPE_CHECKING

from codebase_intel.intentgraph.models import (
    IntentEdgeKind,
    IntentLevel,
    IntentNode,
    IntentNodeStatus,
)
from codebase_intel.intentgraph.store import IntentGraphStore

if TYPE_CHECKING:
    from pathlib import Path

    from codebase_intel.intentgraph.journal import IntentJournal

_LEVEL_TITLES = {
    IntentLevel.SYSTEM: "L0 · System topology",
    IntentLevel.SERVICE: "L1 · Services & modules",
    IntentLevel.COMPONENT: "L2 · Components & flows",
    IntentLevel.DECISION: "L3 · Decisions",
}


def _mermaid_label(node: IntentNode) -> str:
    title = node.title.replace('"', "'").replace("[", "(").replace("]", ")")
    marker = {"active": "", "stale": " ⚠", "superseded": " ✕"}[node.status.value]
    return f"{title}{marker}<br/><i>{node.trust.value}</i>"


class IntentRenderer:
    """Renders the intent graph to Mermaid or standalone HTML."""

    def __init__(self, journal: IntentJournal, db_path: Path) -> None:
        self._journal = journal
        self._db_path = db_path

    async def _load(self) -> tuple[dict[IntentLevel, list[IntentNode]], list[tuple[str, str]]]:
        by_level: dict[IntentLevel, list[IntentNode]] = {}
        contains: list[tuple[str, str]] = []
        async with IntentGraphStore.open(self._db_path) as store:
            await store.materialize(self._journal)
            for level in IntentLevel:
                nodes = await store.nodes_at_level(level, include_superseded=True)
                by_level[level] = nodes
                for node in nodes:
                    for edge in await store.edges_from(node.node_id):
                        if edge.kind == IntentEdgeKind.CONTAINS:
                            contains.append((edge.source_id, edge.target_id))
        return by_level, contains

    async def to_mermaid(self) -> str:
        by_level, contains = await self._load()
        total = sum(len(nodes) for nodes in by_level.values())
        lines = ["flowchart TD"]
        if total == 0:
            lines.append('    empty["No intent recorded yet — codebase-intel why ..."]')
            return "\n".join(lines)

        for level, nodes in by_level.items():
            if not nodes:
                continue
            lines.append(f'    subgraph SG{int(level)}["{_LEVEL_TITLES[level]}"]')
            for node in nodes:
                lines.append(f'        {node.node_id}["{_mermaid_label(node)}"]')
            lines.append("    end")

        for source, target in contains:
            lines.append(f"    {source} --> {target}")

        superseded = [n for nodes in by_level.values() for n in nodes if n.superseded_by]
        for node in superseded:
            lines.append(f"    {node.node_id} -. superseded by .-> {node.superseded_by}")
            lines.append(f"    style {node.node_id} stroke-dasharray: 5 5,opacity:0.5")
        return "\n".join(lines)

    async def to_html(self, project_name: str = "project") -> str:
        by_level, contains = await self._load()
        child_ids = {target for _, target in contains}

        def node_card(node: IntentNode, children_by_parent: dict[str, list[IntentNode]]) -> str:
            status_cls = node.status.value
            why = html_mod.escape(node.why)
            title = html_mod.escape(node.title)
            supersede_note = (
                f'<p class="note">superseded by <code>{node.superseded_by}</code>'
                + (f" — {html_mod.escape(node.supersede_reason)}" if node.supersede_reason else "")
                + "</p>"
                if node.status == IntentNodeStatus.SUPERSEDED
                else ""
            )
            anchors = "".join(
                f"<li><code>{html_mod.escape(a.file_path.as_posix())}</code></li>"
                for a in node.anchors
            )
            anchor_block = f"<ul class='anchors'>{anchors}</ul>" if anchors else ""
            kids = children_by_parent.get(node.node_id, [])
            kid_block = "".join(node_card(k, children_by_parent) for k in kids)
            return (
                f'<details class="{status_cls}" open><summary>{title} '
                f'<span class="badge {node.trust.value}">{node.trust.value}</span> '
                f'<span class="badge status">{node.status.value}</span></summary>'
                f"<p>{why}</p>{supersede_note}{anchor_block}{kid_block}</details>"
            )

        children_by_parent: dict[str, list[IntentNode]] = {}
        all_nodes = {n.node_id: n for nodes in by_level.values() for n in nodes}
        for source, target in contains:
            if target in all_nodes:
                children_by_parent.setdefault(source, []).append(all_nodes[target])

        sections = []
        for level, nodes in by_level.items():
            roots = [n for n in nodes if n.node_id not in child_ids]
            if not roots:
                continue
            cards = "".join(node_card(n, children_by_parent) for n in roots)
            sections.append(f"<section><h2>{_LEVEL_TITLES[level]}</h2>{cards}</section>")

        body = (
            "".join(sections)
            if sections
            else "<p>No intent recorded yet — <code>codebase-intel why ...</code></p>"
        )
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>Intent graph — {html_mod.escape(project_name)}</title>"
            "<style>"
            "body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;"
            "padding:0 1rem;line-height:1.5}"
            "details{border:1px solid #ccc;border-radius:6px;padding:.5rem .8rem;"
            "margin:.5rem 0}"
            "details details{margin-left:1rem}"
            "summary{cursor:pointer;font-weight:600}"
            ".badge{font-size:.7rem;padding:.1rem .4rem;border-radius:4px;"
            "background:#eee;font-weight:400}"
            ".badge.confirmed{background:#c8e6c9}.badge.asserted{background:#fff9c4}"
            ".badge.mined,.badge.derived{background:#ffe0b2}"
            ".stale{border-color:#e6a23c}.superseded{opacity:.55}"
            ".anchors{font-size:.8rem;color:#555}.note{font-size:.85rem;color:#a33}"
            "</style></head><body>"
            f"<h1>Intent graph — {html_mod.escape(project_name)}</h1>{body}"
            "</body></html>"
        )
