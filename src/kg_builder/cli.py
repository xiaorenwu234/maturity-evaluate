"""Command-line interface for Module 1 (``kgb``).

Commands:
    kgb build        -- collect sources and emit the unified knowledge graph
    kgb inspect      -- print stats / search nodes of a built graph
    kgb evidence     -- show the full provenance (evidence chain) for one entity
    kgb export-neo4j -- import a built graph into a live Neo4j database
"""
from __future__ import annotations

import json

import click

from .config import settings_for
from .graph.ontology import NODE_TYPE_LABELS
from .graph.serialization import load_graph
from .pipeline import BuildPipeline


@click.group()
@click.version_option()
def cli() -> None:
    """Maturity-evaluation Module 1: build a Unified System Knowledge Graph."""


@cli.command("build")
@click.argument("repo_path", type=click.Path(exists=True, file_okay=False))
@click.option("-o", "--out", default="out/knowledge_graph.json", show_default=True, help="Output JSON path.")
@click.option("-c", "--config", "config_file", default=None, type=click.Path(exists=True, dir_okay=False),
              help="YAML config file (llm/code/document/... sections).")
@click.option("-n", "--namespace", default="", show_default=True, help="Graph namespace (repo/tenant).")
@click.option("--docs-path", default=None, type=click.Path(), help="Docs root (defaults to repo).")
@click.option("--skip-enrichment", is_flag=True,
              help="Build the structural graph only; skip LLM semantic enrichment.")
@click.option("--enable-deployment", is_flag=True, help="Enable the (stub) deployment collector.")
@click.option("--enable-runtime", is_flag=True, help="Enable the (stub) runtime collector.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging.")
def build(repo_path, out, config_file, namespace, docs_path, skip_enrichment, enable_deployment, enable_runtime, verbose):
    """Collect code/document/deployment/runtime from REPO_PATH into a graph.

    Semantic enrichment runs by LLM (OpenAI-compatible via LangChain) and is ON by
    default; configure the model in --config or env, or pass --skip-enrichment.
    """
    import logging

    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING)

    overrides: dict = {}
    if skip_enrichment:
        overrides["enrich"] = False
    if enable_deployment:
        overrides["deployment__enabled"] = True
    if enable_runtime:
        overrides["runtime__enabled"] = True

    settings = settings_for(
        repo_path, out, config_file=config_file,
        namespace=namespace, docs_path=docs_path, **overrides,
    )
    try:
        report = BuildPipeline(settings).run()
    except Exception as exc:  # noqa: BLE001 - surface config/LLM errors clearly
        raise click.ClickException(str(exc)) from exc
    click.echo(report.summary_text())


@cli.command("inspect")
@click.argument("graph_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--type", "node_type", default=None, help="Filter nodes by type (e.g. Service).")
@click.option("--name", default=None, help="Filter nodes by exact/substring name.")
@click.option("--limit", default=50, show_default=True)
def inspect(graph_path, node_type, name, limit):
    """Summarise a built graph and optionally list matching nodes."""
    graph = load_graph(graph_path)
    stats = graph.stats()
    click.echo(json.dumps(stats, ensure_ascii=False, indent=2))

    matches = graph.nodes
    if node_type:
        matches = [n for n in matches if n.type.lower() == node_type.lower() or NODE_TYPE_LABELS.get(n.type) == node_type]
    if name:
        matches = [n for n in matches if name.lower() in n.name.lower() or name.lower() in n.canonical_name.lower()]
    click.echo(f"\nmatched nodes: {len(matches)}")
    for node in matches[:limit]:
        click.echo(f"  [{node.type}] {node.name}  (id={node.id})")


@cli.command("evidence")
@click.argument("graph_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("entity")
def evidence(graph_path, entity):
    """Show the full evidence chain (all sources) for node/entity."""
    graph = load_graph(graph_path)
    node = _find_one(graph, entity)
    if node is None:
        raise click.ClickException(f"no node matching '{entity}'")

    click.echo(f"Node: [{node.type}] {node.name}  ({node.id})")
    click.echo("Attributes:")
    for k, v in node.attributes.items():
        click.echo(f"  - {k}: {v}")
    click.echo("\nEvidence (source provenance):")
    for p in node.provenance:
        click.echo(f"  * source={p.source_type} conf={p.confidence:.2f} @ {p.source_id}")
        click.echo(f"      evidence: {p.evidence}")

    click.echo("\nRelations:")
    for e in graph.out_edges(node.id):
        dst = graph.get_node(e.target)
        _print_edge("->", e, dst, e.source)
    for e in graph.in_edges(node.id):
        src = graph.get_node(e.source)
        _print_edge("<-", e, src, e.target)


@cli.command("export-neo4j")
@click.argument("graph_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--uri", default=None, show_default="bolt://localhost:7687",
              help="Neo4j Bolt URI (env KGB_NEO4J__URI).")
@click.option("--user", default=None, show_default="neo4j", help="Username (env KGB_NEO4J__USER).")
@click.option("--password", default=None, hide_input=True,
              help="Password (env KGB_NEO4J__PASSWORD / NEO4J_PASSWORD; prompted if absent).")
@click.option("--database", default=None, show_default="neo4j", help="Target database.")
@click.option("--clear", is_flag=True, help="Delete all existing nodes/edges first (DETACH DELETE).")
def export_neo4j(graph_path, uri, user, password, database, clear):
    """Import a built graph JSON into a live Neo4j database (idempotent MERGE)."""
    import os

    from .graph import import_graph_to_neo4j, Neo4jImportError

    uri = uri or os.getenv("KGB_NEO4J__URI") or "bolt://localhost:7687"
    user = user or os.getenv("KGB_NEO4J__USER") or "neo4j"
    database = database or os.getenv("KGB_NEO4J__DATABASE") or "neo4j"
    password = password or os.getenv("KGB_NEO4J__PASSWORD") or os.getenv("NEO4J_PASSWORD")
    if not password:
        password = click.prompt("Neo4j password", hide_input=True, err=True)

    graph = load_graph(graph_path)

    if clear:
        _neo4j_clear(uri, user, password, database)

    try:
        res = import_graph_to_neo4j(
            graph, uri=uri, user=user, password=password, database=database,
        )
    except Neo4jImportError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"\u2713 已导入 Neo4j {res['uri']} (db={res['database']})\n"
        f"  nodes: {res['nodes']}  edges: {res['edges']}\n"
        f"  labels: {', '.join(res['labels'])}\n"
        f"  relations: {', '.join(res['relations'])}"
    )


def _neo4j_clear(uri, user, password, database) -> None:
    """Optional --clear: wipe the target database before import."""
    from neo4j import GraphDatabase  # type: ignore

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=database) as session:
            session.run("MATCH (n) DETACH DELETE n").consume()
    finally:
        driver.close()


def _print_edge(direction, edge, other, self_endpoint):
    rel = edge.relation
    sources = sorted({p.source_type for p in edge.provenance})
    who = other.name if other else edge.target
    if direction == "->":
        head = f"    {rel} --> {who}"
    else:
        head = f"    {who} {rel} --> (this)"
    click.echo(head + f"   [sources: {', '.join(sources)}]")
    for p in edge.provenance:
        click.echo(f"        - ({p.source_type}, conf={p.confidence:.2f}) {p.evidence} @ {p.source_id}")


def _find_one(graph, query: str):
    exact = graph.find_nodes(name=query)
    if exact:
        return exact[0]
    q = query.lower()
    for n in graph.nodes:
        if q in n.name.lower() or q in n.canonical_name.lower():
            return n
    return None


if __name__ == "__main__":  # pragma: no cover
    cli()
