import argparse
import contextvars
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
import uvicorn

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from neo4j.graph import Node, Path, Relationship

from neo4j import READ_ACCESS, GraphDatabase
from neo4j.exceptions import AuthError, CypherSyntaxError, ServiceUnavailable
from utils.logging_config import setup_logging

load_dotenv()
setup_logging(write_to_file=False)
log = logging.getLogger("ark-mcp")

mcp = FastMCP(
    "DeepMorph Orchestrator",
    host="0.0.0.0",
    json_response=True,
    stateless_http=not bool(os.environ.get("SUPABASE_URL")),
)

_connections: dict[str | tuple, tuple] = {}

_current_user_domain: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "user_domain", default=None
)
_current_user_jwt: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "user_jwt", default=None
)
_current_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "user_id", default=None
)


def _get_connection(graph_name: str) -> tuple:
    user_domain = _current_user_domain.get()
    user_jwt = _current_user_jwt.get()

    if user_domain is None or user_jwt is None:
        cache_key: str | tuple = "__default__"
        if cache_key not in _connections:
            uri = os.environ["NEO4J_URI"]
            username = os.environ["NEO4J_USERNAME"]
            password = os.environ["NEO4J_PASSWORD"]
            database = os.environ.get("NEO4J_DATABASE") or "neo4j"
            driver = GraphDatabase.driver(uri, auth=(username, password))
            _connections[cache_key] = (driver, database)
        return _connections[cache_key]

    cache_key = (user_domain, graph_name)
    if cache_key not in _connections:
        uri, username, password, database = _fetch_instance_credentials(user_jwt, graph_name)
        driver = GraphDatabase.driver(uri, auth=(username, password))
        _connections[cache_key] = (driver, database)
    return _connections[cache_key]


def _fetch_instance_credentials(user_jwt: str, graph_name: str) -> tuple[str, str, str, str]:
    supabase_url = os.environ["SUPABASE_URL"]
    anon_key = os.environ["SUPABASE_ANON_KEY"]
    response = httpx.get(
        f"{supabase_url}/rest/v1/neo4j_instances",
        params={"select": "uri,username,password", "name": f"eq.{graph_name}"},
        headers={"Authorization": f"Bearer {user_jwt}", "apikey": anon_key},
        timeout=10,
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        raise ValueError(f"Graph {graph_name!r} not found or not accessible")
    row = rows[0]
    database = os.environ.get("NEO4J_DATABASE") or "neo4j"
    return row["uri"], row["username"], row["password"], database


def _list_graphs_via_supabase(user_jwt: str) -> list[dict[str, Any]]:
    supabase_url = os.environ["SUPABASE_URL"]
    anon_key = os.environ["SUPABASE_ANON_KEY"]
    # The read boundary lives on `looms`: RLS returns a loom iff the user is
    # product-approved AND (staff OR looms.user_domain='public' OR a loom_access
    # grant matches their email/domain). loom_access itself is staff-only, so we
    # can't read grants with a user token — but we don't need to: for a non-staff
    # user, a visible non-'public' loom is, by that policy, one they're granted.
    # So looms.user_domain is the entitled-vs-OSS signal (neo4j_instances.user_domain
    # is legacy and not the boundary). Graph name comes from the joined instance.
    response = httpx.get(
        f"{supabase_url}/rest/v1/looms",
        params={"select": "user_domain,neo4j_instances(name)"},
        headers={"Authorization": f"Bearer {user_jwt}", "apikey": anon_key},
        timeout=10,
    )
    response.raise_for_status()
    domains: dict[str, str] = {}
    for row in response.json():
        instance = row.get("neo4j_instances")
        name = instance.get("name") if instance else None
        if not name:
            continue
        domain = row.get("user_domain") or ""
        # If an instance is reachable via several looms, a non-'public' (granted)
        # one wins over a 'public' one.
        if name not in domains or domains[name] == "public":
            domains[name] = domain
    return [
        {"name": name, "user_domain": domain} for name, domain in sorted(domains.items())
    ]


# ---------------------------------------------------------------------------
# Usage activity ("who is using MCP, and how much")
#
# The server makes no LLM calls, so there is nothing to meter against the token
# budget — but we still want a cheap engagement signal. Each tool call bumps an
# in-process per-user counter (the hot path is a single dict update under a
# short lock). A background daemon drains all buffered users to Supabase every
# _ACTIVITY_FLUSH_INTERVAL_SECONDS, so tool latency is never gated on Supabase
# and a burst of calls is coalesced into one write carrying the accumulated
# count. record_mcp_activity is a SECURITY DEFINER RPC granted to
# `authenticated`, so we write with the caller's own JWT — no service_role key.
# Accuracy is intentionally best-effort: a restart drops the un-flushed tail
# (at most one interval's worth).
# ---------------------------------------------------------------------------

_ACTIVITY_FLUSH_INTERVAL_SECONDS = 30.0
_activity_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mcp-activity")
_activity_lock = threading.Lock()
# user_id -> {"count": int, "session_id": str, "jwt": str, "tool": str, "graph": str | None}
_activity_state: dict[str, dict[str, Any]] = {}
_activity_flusher_started = False


def _flush_activity(
    user_jwt: str, tool: str, graph: str | None, count: int, session_id: str
) -> None:
    """Best-effort write of aggregated MCP activity. Runs off the request path
    in a worker thread and never raises: telemetry must not break a query."""
    try:
        supabase_url = os.environ["SUPABASE_URL"]
        anon_key = os.environ["SUPABASE_ANON_KEY"]
        response = httpx.post(
            f"{supabase_url}/rest/v1/rpc/record_mcp_activity",
            json={
                "p_tool": tool,
                "p_graph": graph,
                "p_count": count,
                "p_session_id": session_id,
            },
            headers={"Authorization": f"Bearer {user_jwt}", "apikey": anon_key},
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:
        log.debug("activity flush failed (tool=%s): %s", tool, exc)


def _drain_activity() -> None:
    """Flush every user's buffered call count and evict idle users. Called on a
    timer by the flusher daemon (and directly in tests). Never raises."""
    pending: list[tuple[str, str, str | None, int, str]] = []
    with _activity_lock:
        for user_id in list(_activity_state.keys()):
            state = _activity_state[user_id]
            if state["count"] > 0:
                pending.append(
                    (
                        state["jwt"],
                        state["tool"],
                        state["graph"],
                        state["count"],
                        state["session_id"],
                    )
                )
                state["count"] = 0
            else:
                # No calls in the last interval: evict so _activity_state stays
                # bounded to recently-active users on a long-lived service.
                del _activity_state[user_id]
    for args in pending:
        _activity_executor.submit(_flush_activity, *args)


def _activity_flush_loop() -> None:
    while True:
        time.sleep(_ACTIVITY_FLUSH_INTERVAL_SECONDS)
        try:
            _drain_activity()
        except Exception as exc:  # pragma: no cover - defensive; _drain never raises
            log.debug("activity drain failed: %s", exc)


def _record_activity(tool: str, graph_name: str | None = None) -> None:
    """Count one MCP tool call for the current user without blocking the caller.

    The hot path is an in-memory increment; the actual Supabase write is done by
    a background daemon that drains all buffered users every
    _ACTIVITY_FLUSH_INTERVAL_SECONDS, so a burst is coalesced into one write with
    the accumulated count and no call is lost when the user then goes idle.
    No-ops when there is no authenticated caller (local mode). Never raises."""
    global _activity_flusher_started
    try:
        user_jwt = _current_user_jwt.get()
        user_id = _current_user_id.get()
        if user_jwt is None or user_id is None:
            return
        with _activity_lock:
            state = _activity_state.get(user_id)
            if state is None:
                state = {"count": 0, "session_id": str(uuid.uuid4())}
                _activity_state[user_id] = state
            state["count"] += 1
            # Refresh the fields the drain will send; the newest token wins so a
            # flush up to an interval later still carries a valid JWT.
            state["jwt"] = user_jwt
            state["tool"] = tool
            state["graph"] = graph_name
            start_flusher = not _activity_flusher_started
            if start_flusher:
                _activity_flusher_started = True
        if start_flusher:
            threading.Thread(
                target=_activity_flush_loop, name="mcp-activity-flush", daemon=True
            ).start()
    except Exception as exc:
        log.debug("activity record failed (tool=%s): %s", tool, exc)


# Machine-only properties with no value to an LLM consumer — dropped from payloads.
_TRIMMED_PROPERTIES = frozenset({"content_sha256"})

# Cap on any single string property value. A context response can carry hundreds
# of nodes (limit per group x several groups), and one oversized field (a long
# summary/description) can push the whole response past Cloud Run's limit and get
# it truncated. Generous enough that real names/paths/summaries pass untouched.
_MAX_PROPERTY_CHARS = 1000
_TRUNCATION_MARKER = "…[truncated]"


def _trim_property_value(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_PROPERTY_CHARS:
        return value[:_MAX_PROPERTY_CHARS] + _TRUNCATION_MARKER
    return value


def _trim_properties(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _trim_property_value(value)
        for key, value in properties.items()
        if key not in _TRIMMED_PROPERTIES
    }


def _node_properties(node: Any) -> dict[str, Any]:
    return _trim_properties(dict(node))


def _node_display(properties: dict[str, Any]) -> str | None:
    for key in ("qualified_name", "name", "file_path", "canonical_business_name"):
        value = properties.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _node_payload(node: Any, graph_name: str | None = None) -> dict[str, Any]:
    properties = _node_properties(node)
    payload: dict[str, Any] = {
        "id": node.element_id,
        "labels": sorted(node.labels),
        "properties": properties,
    }
    if graph_name is not None:
        # Provenance so a client comparing multiple graphs cannot misattribute a
        # node to the wrong graph (element_id is only unique within one graph).
        payload["graph"] = graph_name
    return payload


def _node_ref(node: Any) -> dict[str, Any]:
    properties = _node_properties(node)
    ref = {
        "id": node.element_id,
        "labels": sorted(node.labels),
    }
    display = _node_display(properties)
    if display is not None:
        ref["display"] = display
    return ref


def _edge_payload(rel: Any, graph_name: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": rel.element_id,
        "type": rel.type,
        "from": rel.start_node.element_id,
        "to": rel.end_node.element_id,
        "from_node": _node_ref(rel.start_node),
        "to_node": _node_ref(rel.end_node),
        "properties": _trim_properties(dict(rel)),
    }
    if graph_name is not None:
        payload["graph"] = graph_name
    return payload


def _collect_graph(
    value: Any, nodes: dict, edges: dict, graph_name: str | None = None
) -> bool:
    """Recursively extract Node/Relationship/Path objects. Returns True if any found."""
    if isinstance(value, Node):
        nodes[value.element_id] = _node_payload(value, graph_name)
        return True
    if isinstance(value, Relationship):
        nodes.setdefault(
            value.start_node.element_id, _node_payload(value.start_node, graph_name)
        )
        nodes.setdefault(
            value.end_node.element_id, _node_payload(value.end_node, graph_name)
        )
        edges[value.element_id] = _edge_payload(value, graph_name)
        return True
    if isinstance(value, Path):
        for node in value.nodes:
            nodes[node.element_id] = _node_payload(node, graph_name)
        for rel in value.relationships:
            edges[rel.element_id] = _edge_payload(rel, graph_name)
        return True
    if isinstance(value, list):
        return any(_collect_graph(item, nodes, edges, graph_name) for item in value)
    return False


def _serialize_records(
    records: list, graph_name: str | None = None
) -> dict[str, Any] | list[dict[str, Any]]:
    nodes: dict = {}
    edges: dict = {}
    scalars: list = []
    has_graph = False

    for record in records:
        scalar_row: dict = {}
        for key, value in record.items():
            if _collect_graph(value, nodes, edges, graph_name):
                has_graph = True
            else:
                scalar_row[key] = value
        if scalar_row and any(value is not None for value in scalar_row.values()):
            scalars.append(scalar_row)

    if has_graph:
        result: dict[str, Any] = {
            "nodes": list(nodes.values()),
            "edges": list(edges.values()),
        }
        if scalars:
            result["scalars"] = scalars
        return result
    return scalars


def _validate_limit(limit: int) -> int:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return limit


def _validate_depth(depth: int) -> int:
    if depth < 1:
        raise ValueError("depth must be >= 1")
    return depth


@dataclass(frozen=True)
class _Expansion:
    """One neighborhood-collection group of a context query.

    Every ``get*Context`` tool fans out from an anchor node along a fixed set of
    relationship groups, collects a bounded set of paths per group, and returns
    them alongside the anchor. This declares one such group so the shared shape
    lives in :func:`_build_context_query` instead of a hand-written Cypher blob
    per tool. Compiles to::

        CALL {
            WITH <anchor_var>
            [OPTIONAL MATCH <bind> ...]       # zero or more intermediate bindings
            OPTIONAL MATCH <var> = <collect>
            RETURN collect(DISTINCT <var>)[..$limit] AS <name>
        }

    Args:
        name: RETURN alias for the collected paths (e.g. ``"owned_paths"``).
        var: Path variable that is collected (e.g. ``"owned_path"``).
        collect: Path pattern to collect; references the anchor and/or any
            variable established by ``binds``.
        binds: Zero or more ``OPTIONAL MATCH`` patterns that establish
            intermediate variables (not collected) the ``collect`` pattern pivots
            through — e.g. reaching a file's owned functions, then the steps that
            use them, before collecting each step's entities. The bound paths
            themselves are deliberately not returned.
    """

    name: str
    var: str
    collect: str
    binds: tuple[str, ...] = ()


def _expansion_block(anchor_var: str, exp: _Expansion) -> str:
    # The path variable aliases the whole collected path (`var = <pattern>`), so it
    # must not also appear as a node/relationship variable inside the pattern —
    # Neo4j rejects `MATCH ui = (ui:X)-[...]->()`. Fail at build time (tests hit
    # this) rather than only when the query executes against a live graph.
    for pattern in (exp.collect, *exp.binds):
        if re.search(rf"[(\[]\s*{re.escape(exp.var)}[\s:)\]]", pattern):
            raise ValueError(
                f"expansion path variable {exp.var!r} is also used as a bound "
                f"variable in pattern {pattern!r}; use a distinct path variable or "
                f"make the node anonymous"
            )
    lines = [f"    WITH {anchor_var}"]
    for bind in exp.binds:
        lines.append(f"    OPTIONAL MATCH {bind}")
    lines.append(f"    OPTIONAL MATCH {exp.var} = {exp.collect}")
    lines.append(f"    RETURN collect(DISTINCT {exp.var})[..$limit] AS {exp.name}")
    body = "\n".join(lines)
    return f"CALL {{\n{body}\n}}"


def _build_context_query(
    anchor_clause: str,
    anchor_var: str,
    expansions: list[_Expansion],
) -> str:
    """Compile an anchor clause plus expansion groups into a context query.

    ``anchor_clause`` is the leading Cypher that binds ``anchor_var`` (a MATCH,
    optionally with WHERE/WITH/ORDER BY). Each expansion becomes an isolated CALL
    subquery, and the anchor plus every expansion alias is returned as one row
    per matched anchor. Paths are bounded per group by the ``$limit`` parameter,
    which the caller must supply.
    """
    blocks = "\n".join(_expansion_block(anchor_var, exp) for exp in expansions)
    returned = ", ".join([anchor_var, *(exp.name for exp in expansions)])
    return f"{anchor_clause.strip()}\n{blocks}\nRETURN {returned}"


def _evict_connection(graph_name: str) -> None:
    user_domain = _current_user_domain.get()
    cache_key: str | tuple = (user_domain, graph_name) if user_domain else "__default__"
    cached = _connections.pop(cache_key, None)
    if cached is None:
        return
    driver, _database = cached
    try:
        driver.close()
    except Exception as exc:
        log.warning(
            "Failed to close evicted connection for graph %r: %s",
            graph_name,
            exc,
        )


# These DeepMorph graphs run Neo4j 5, whose Cypher removed some syntax that
# models still reach for. Surfaced on a syntax error so the caller can self-correct.
_NEO4J5_CYPHER_HINT = (
    "This graph runs Neo4j 5 Cypher. Common removed/changed syntax: "
    "use `n.prop IS NOT NULL` not `EXISTS(n.prop)`; `elementId(n)` not `id(n)`; "
    "`CALL { ... }` subqueries import outer variables via `WITH`; "
    "quantifier is `*1..3` on the relationship, e.g. `-[:REL*1..3]->`."
)


def _run_query(
    graph_name: str,
    cypher: str,
    params: dict[str, Any] | None = None,
    limit: int = 100,
) -> list:
    validated_limit = _validate_limit(limit)
    for attempt in range(2):
        driver, database = _get_connection(graph_name)
        t0 = time.monotonic()
        try:
            with driver.session(database=database, default_access_mode=READ_ACCESS) as session:
                result = session.run(cypher, params or {})
                records = [record for _, record in zip(range(validated_limit), result)]
        except CypherSyntaxError as exc:
            # Not retryable — re-running the same bad query won't help. Return the
            # driver's message plus a dialect hint so the caller can fix the query.
            raise ValueError(f"Cypher syntax error: {exc}. {_NEO4J5_CYPHER_HINT}") from exc
        except (AuthError, ServiceUnavailable) as exc:
            if attempt == 0:
                log.warning("Connection error for graph %r, evicting cache and retrying: %s", graph_name, exc)
                _evict_connection(graph_name)
                continue
            raise
        elapsed_ms = (time.monotonic() - t0) * 1000
        log.info("  query returned %d records in %.0fms", len(records), elapsed_ms)
        return records
    raise RuntimeError("unreachable")


def _list_available_graphs() -> list[dict[str, Any]]:
    """Graphs the caller can see: {name, user_domain}. RLS scopes these to the
    user's loom_access grants plus any looms.user_domain='public' (OSS/demo)."""
    user_jwt = _current_user_jwt.get()
    if user_jwt is None:
        return [{"name": "local", "user_domain": "local"}]
    return _list_graphs_via_supabase(user_jwt)


def _list_available_graph_names() -> list[str]:
    return [g["name"] for g in _list_available_graphs()]


def _resolve_graph_choice(graphs: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
    """Pick a default graph, preferring the caller's entitled graphs over OSS.

    A visible graph is entitled (an org graph the user was granted via loom_access)
    unless it is only reachable through the broad public/OSS marker
    (user_domain='public'). Enterprise users see their org graphs plus every public
    graph, which would otherwise force a selection prompt they don't care about.
    Returns (auto_selected_name_or_None, candidate_names_to_offer):
    - one visible graph, or exactly one entitled graph -> auto-select it;
    - several entitled graphs -> prompt among those only (drop OSS noise);
    - no entitled graphs (OSS-only user) -> prompt among all visible graphs.
    """
    names = [g["name"] for g in graphs]
    if len(graphs) <= 1:
        return (names[0] if names else None), names
    entitled = [g["name"] for g in graphs if g.get("user_domain") != "public"]
    if len(entitled) == 1:
        return entitled[0], entitled
    if entitled:
        return None, entitled
    return None, names


_LIST_CODEBASES_CYPHER = """
MATCH (n:Code)
WHERE n.codebase_id IS NOT NULL AND n.codebase_id <> ''
RETURN DISTINCT n.codebase_id AS codebase_id
ORDER BY codebase_id
"""


def _fetch_graph_schema(graph_name: str) -> dict[str, Any] | list[dict[str, Any]]:
    records = _run_query(graph_name, _SCHEMA_CYPHER, params={}, limit=10_000)
    return _serialize_records(records, graph_name)


def _list_graph_codebases(graph_name: str) -> list[str]:
    rows = _run_query(
        graph_name,
        _LIST_CODEBASES_CYPHER,
        params={},
        limit=10_000,
    )
    # Defense in depth against the Cypher guard: unattributed stubs/ghosts can
    # carry codebase_id '' or null, and neither names a real codebase.
    return [row["codebase_id"] for row in rows if row["codebase_id"]]


@mcp.tool()
def bootstrap(graph_name: str | None = None) -> dict[str, Any]:
    """
    Resolve the initial graph context needed for DeepMorph Orchestrator queries.

    In the common single-graph case, this returns the selected graph name,
    a schema snapshot, and the available codebase IDs in one call so callers
    can issue their first real query immediately.

    Graph selection prefers the caller's entitled graphs (those granted to their
    email/domain via loom_access) over broadly-public OSS/demo graphs: an
    enterprise user with a single org graph is auto-selected even when public
    graphs are also visible, so no selection prompt appears. Selection is only
    required when several entitled graphs exist (the prompt then offers just
    those), or when the caller has none. Call bootstrap() again with graph_name
    set after the user picks one.

    Args:
        graph_name: Optional graph to resolve explicitly. Required only when
            multiple graphs are available and the caller has chosen one.
    """
    log.info("bootstrap requested_graph=%s", graph_name)
    _record_activity("bootstrap", graph_name)
    graphs = _list_available_graphs()
    names = [g["name"] for g in graphs]

    if not graphs:
        return {
            "graphs": [],
            "graph": None,
            "schema": None,
            "codebases": [],
            "requires_graph_selection": False,
        }

    if graph_name is None:
        selected_graph, candidates = _resolve_graph_choice(graphs)
        if selected_graph is None:
            return {
                "graphs": candidates,
                "graph": None,
                "schema": None,
                "codebases": None,
                "requires_graph_selection": True,
            }
    else:
        if graph_name not in names:
            raise ValueError(f"Graph {graph_name!r} not found or not accessible")
        selected_graph = graph_name

    return {
        "graphs": names,
        "graph": selected_graph,
        "schema": _fetch_graph_schema(selected_graph),
        "codebases": _list_graph_codebases(selected_graph),
        "requires_graph_selection": False,
    }


@mcp.tool()
def queryGraph(
    graph_name: str,
    cypher: str,
    params: dict[str, Any] | None = None,
    limit: int = 100,
) -> dict[str, Any] | list[dict[str, Any]]:
    """
    Run a raw read-only Cypher query against a DeepMorph knowledge graph.

    This is an escape hatch for questions no typed tool covers. Prefer the typed
    tools when one fits — getFileContext, getFeatureContext, getDataFlow,
    getCallChain — because they encode the correct traversal deterministically.

    Returns {nodes, edges} when the query returns graph objects (Node/Relationship/Path),
    or a flat list of records for scalar results.

    The graph runs Neo4j 5 Cypher: use `n.prop IS NOT NULL` (not `EXISTS(n.prop)`),
    `elementId(n)` (not `id(n)`), and `-[:REL*1..3]->` for bounded var-length hops.
    A syntax error is returned with the driver message plus this hint so the query
    can be fixed and retried.

    Args:
        graph_name: Name of the graph instance to query (looked up in neo4j_instance table).
        cypher: A read-only Cypher query. Write operations will be rejected by the database.
        params: Optional Cypher parameters passed to session.run().
        limit: Maximum number of records to return (default 100).
    """
    log.info(
        "queryGraph graph=%s limit=%d cypher=%s",
        graph_name,
        limit,
        cypher.strip()[:120],
    )
    _record_activity("queryGraph", graph_name)
    records = _run_query(graph_name, cypher, params=params, limit=limit)
    return _serialize_records(records, graph_name)


_SCHEMA_CYPHER = """
CALL db.schema.nodeTypeProperties()
YIELD nodeLabels, propertyName, propertyTypes
RETURN 'node' AS kind, nodeLabels AS labels, collect({property: propertyName, types: propertyTypes}) AS properties
UNION
CALL db.schema.relTypeProperties()
YIELD relType, propertyName, propertyTypes
RETURN 'relationship' AS kind, [relType] AS labels, collect({property: propertyName, types: propertyTypes}) AS properties
"""


@mcp.tool()
def getSchema(graph_name: str) -> dict[str, Any] | list[dict[str, Any]]:
    """
    Return the live schema of a DeepMorph knowledge graph.

    Args:
        graph_name: Name of the graph instance (looked up in neo4j_instance table).
    """
    log.info("getSchema graph=%s", graph_name)
    _record_activity("getSchema", graph_name)
    return _fetch_graph_schema(graph_name)


@mcp.tool()
def listCodebases(graph_name: str) -> list[str]:
    """
    Return all codebase_id values present in the graph.

    Args:
        graph_name: Name of the graph instance (looked up in neo4j_instance table).
    """
    log.info("listCodebases graph=%s", graph_name)
    _record_activity("listCodebases", graph_name)
    return _list_graph_codebases(graph_name)


@mcp.tool()
def listGraphs() -> dict[str, Any]:
    """
    Return the names of all Neo4j graphs available to the authenticated user,
    plus the shared schema fetched from the first graph.

    All DeepMorph graphs share the same schema, so a single schema fetch
    covers all graphs. The schema field contains the same output as
    getSchema() and can be used immediately to understand available node
    labels and relationship types before writing queries.

    In local mode (no SUPABASE_URL configured), graphs contains ["local"].
    In cloud mode, graphs lists the names from neo4j_instances for the
    authenticated user's domain.
    """
    log.info("listGraphs")
    _record_activity("listGraphs")
    names = _list_available_graph_names()

    schema = None
    if names:
        try:
            schema = _fetch_graph_schema(names[0])
        except Exception as exc:
            log.warning("Failed to fetch schema from graph %r: %s", names[0], exc)

    return {"graphs": names, "schema": schema}


@mcp.tool()
def getFileContext(
    graph_name: str,
    file_path: str,
    codebase_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any] | list[dict[str, Any]]:
    """
    Return file-centric code, caller, data, and business-step context.

    Fetches the File node plus five categories of connected subgraph:
    - Owned code: types, functions, and variables declared in the file
      (HAS_TYPE | HAS_FUNC | HAS_VAR up to 3 hops).
    - Inbound callers: functions in other files that call functions owned
      by this file (CALLS_FUNC).
    - Data links: DataEntity nodes that reference owned functions via
      lifecycle edges (CREATED_BY, USED_BY, TRANSFORMED_BY, SAVED_BY,
      VALIDATED_BY, FILTERED_BY).
    - Business step links: Step nodes that USES_FUNC any owned function.
    - Entity-step links: DataEntity nodes associated to those steps via
      USES_ENTITY.
    - UI pages: Business:UIComponent nodes grounded to this file via
      USES_RESOURCE (the deterministic page/surface layer). Lets a template
      or route file (e.g. .blade.php / .tsx) resolve to the page it defines.

    Matching is exact and case-sensitive on file_path. When codebase_id is
    omitted, the lookup spans every matching file path in the graph.

    Args:
        graph_name: Name of the graph instance (looked up in neo4j_instances table).
        file_path: Exact file path stored on the File node (e.g. "src/main/java/App.java").
            Matching is case-sensitive and exact.
        codebase_id: Optional codebase filter.
        limit: Maximum number of paths collected per category (default 50).
    """
    log.info(
        "getFileContext graph=%s codebase=%s file=%s",
        graph_name,
        codebase_id,
        file_path,
    )
    _record_activity("getFileContext", graph_name)
    owned_func = "(file)-[:HAS_TYPE|HAS_FUNC*1..3]->(owned_func:Code:Function)"
    cypher = _build_context_query(
        """
        MATCH (file:Code:File {file_path: $file_path})
        WHERE ($codebase_id IS NULL OR file.codebase_id = $codebase_id)
        """,
        "file",
        [
            _Expansion(
                name="owned_paths",
                var="owned_path",
                collect="(file)-[:HAS_TYPE|HAS_FUNC|HAS_VAR*1..3]->(:Code)",
            ),
            _Expansion(
                name="inbound_paths",
                var="inbound",
                collect="(caller:Code:Function)-[:CALLS_FUNC]->(owned_func)",
                binds=(owned_func,),
            ),
            _Expansion(
                name="data_paths",
                var="data_link",
                collect="(entity:Data:DataEntity)-[:CREATED_BY|USED_BY|TRANSFORMED_BY|SAVED_BY|VALIDATED_BY|FILTERED_BY]->(owned_func)",
                binds=(owned_func,),
            ),
            _Expansion(
                name="step_paths",
                var="step_link",
                collect="(step:Business:Step)-[:USES_FUNC]->(owned_func)",
                binds=(owned_func,),
            ),
            _Expansion(
                name="entity_step_paths",
                var="entity_step_link",
                collect="(step)-[:USES_ENTITY]->(:Data:DataEntity)",
                binds=(owned_func, "(step:Business:Step)-[:USES_FUNC]->(owned_func)"),
            ),
            _Expansion(
                name="ui_paths",
                var="ui_link",
                collect="(:Business:UIComponent)-[:USES_RESOURCE]->(file)",
            ),
        ],
    )
    records = _run_query(
        graph_name,
        cypher,
        params={"codebase_id": codebase_id, "file_path": file_path, "limit": limit},
        limit=limit,
    )
    return _serialize_records(records, graph_name)


@mcp.tool()
def getFeatureContext(
    graph_name: str,
    feature_name: str,
    limit: int = 50,
) -> dict[str, Any] | list[dict[str, Any]]:
    """
    Return feature, rule, scenario, step, function, entity, and UI context.

    Fetches the Feature node plus five categories of connected subgraph:
    - Feature tree: Rule, Scenario, and Step nodes reachable via
      HAS_RULE | HAS_SCENARIO | HAS_STEP (up to 3 hops).
    - Implementing functions: Code:Function nodes reached from any Step
      via USES_FUNC.
    - Linked data entities: Data:DataEntity nodes reached from any Step
      via USES_ENTITY.
    - Step order: NEXT_STEP edges between the feature's Steps. HAS_STEP
      collection order is meaningless — reconstruct step/flow order from
      these NEXT_STEP edges, never from the order steps appear in the tree.
    - UI pages: Business:UIComponent nodes the feature renders via USES_UI.

    Scenario nodes carry a journey_type property (happy_path, alternate_path,
    failure_path, recovery_path, background). When drawing a user-facing flow,
    gate on it: drop background (infrastructure) steps and keep the rest,
    rendering failure/recovery as branches.

    Note: the Business layer is an LLM projection and may be incomplete. An
    empty result here is NOT evidence the capability is absent — fall back to
    the Code layer (searchCode, then getFileContext / getCallChain) before
    concluding absence.

    feature_name is matched exactly and case-sensitively against the
    Feature node's name property.

    Args:
        graph_name: Name of the graph instance (looked up in neo4j_instances table).
        feature_name: Exact name of the Feature node (case-sensitive).
        limit: Maximum number of paths collected per category (default 50).
    """
    log.info("getFeatureContext graph=%s feature=%r", graph_name, feature_name)
    _record_activity("getFeatureContext", graph_name)
    tree = "(feature)-[:HAS_RULE|HAS_SCENARIO|HAS_STEP*1..3]->(:Business)"
    cypher = _build_context_query(
        "MATCH (feature:Business:Feature {name: $feature_name})",
        "feature",
        [
            _Expansion(name="feature_paths", var="feature_path", collect=tree),
            _Expansion(
                name="func_paths",
                var="func_path",
                collect=f"{tree}-[:USES_FUNC]->(:Code:Function)",
            ),
            _Expansion(
                name="entity_paths",
                var="entity_path",
                collect=f"{tree}-[:USES_ENTITY]->(:Data:DataEntity)",
            ),
            _Expansion(
                name="next_step_paths",
                var="next_step",
                collect=f"{tree}-[:NEXT_STEP]->(:Business:Step)",
            ),
            _Expansion(
                name="ui_paths",
                var="ui",
                collect="(feature)-[:USES_UI]->(:Business:UIComponent)",
            ),
        ],
    )
    records = _run_query(
        graph_name,
        cypher,
        params={"feature_name": feature_name, "limit": limit},
        limit=limit,
    )
    return _serialize_records(records, graph_name)


@mcp.tool()
def getDataFlow(
    graph_name: str,
    entity_name: str,
    codebase_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any] | list[dict[str, Any]]:
    """
    Return data entity lifecycle edges, function targets, and peer associations.

    Fetches one or more Data:DataEntity nodes whose name matches
    entity_name, plus two categories of connected subgraph:
    - Lifecycle paths: edges to Code:Function nodes via CREATED_BY,
      USED_BY, TRANSFORMED_BY, SAVED_BY, VALIDATED_BY, or FILTERED_BY.
    - Peer associations: undirected edges to other DataEntity nodes via
      ASSOCIATES, ASSOCIATES_ONE, ASSOCIATES_MANY, or CONTAINS_ENTITY.

    entity_name matching is case-insensitive and checks three fields in
    priority order: name, canonical_business_name, aliases[]. If multiple
    entities match, exact name matches are returned first.

    Unlike getFeatureContext and getCallChain, matching here is NOT
    case-sensitive — pass the name in any casing.

    Note: DataEntity is a curated projection (only persisted / boundary-crossing
    types are modeled) and may be incomplete. An empty result is NOT evidence the
    data/type is absent — fall back to the Code layer via searchCode before
    concluding absence.

    Args:
        graph_name: Name of the graph instance (looked up in neo4j_instances table).
        entity_name: Name to search for (case-insensitive; checked against
            name, canonical_business_name, and aliases).
        codebase_id: Optional codebase filter.
        limit: Maximum number of paths collected per category (default 50).
    """
    log.info(
        "getDataFlow graph=%s entity=%r codebase=%s",
        graph_name,
        entity_name,
        codebase_id,
    )
    _record_activity("getDataFlow", graph_name)
    cypher = _build_context_query(
        """
        MATCH (entity:Data:DataEntity)
        WHERE ($codebase_id IS NULL OR entity.codebase_id = $codebase_id)
          AND (
            toLower(entity.name) = toLower($entity_name)
            OR toLower(coalesce(entity.canonical_business_name, '')) = toLower($entity_name)
            OR any(alias IN coalesce(entity.aliases, []) WHERE toLower(alias) = toLower($entity_name))
          )
        WITH entity
        ORDER BY
          CASE
            WHEN toLower(entity.name) = toLower($entity_name) THEN 0
            WHEN toLower(coalesce(entity.canonical_business_name, '')) = toLower($entity_name) THEN 1
            ELSE 2
          END,
          entity.name
        """,
        "entity",
        [
            _Expansion(
                name="lifecycle_paths",
                var="lifecycle",
                collect="(entity)-[:CREATED_BY|USED_BY|TRANSFORMED_BY|SAVED_BY|VALIDATED_BY|FILTERED_BY]->(:Code:Function)",
            ),
            _Expansion(
                name="association_paths",
                var="association",
                collect="(entity)-[:ASSOCIATES|ASSOCIATES_ONE|ASSOCIATES_MANY|CONTAINS_ENTITY]-(:Data:DataEntity)",
            ),
        ],
    )
    params = {"entity_name": entity_name, "codebase_id": codebase_id, "limit": limit}
    records = _run_query(graph_name, cypher, params=params, limit=limit)
    return _serialize_records(records, graph_name)


@mcp.tool()
def getCallChain(
    graph_name: str,
    function_fqn: str,
    codebase_id: str | None = None,
    depth: int = 2,
    limit: int = 50,
) -> dict[str, Any] | list[dict[str, Any]]:
    """
    Return a function node with outbound call-chain hops and inbound callers.

    Fetches the Code:Function node identified by function_fqn, optionally
    scoped by codebase_id, plus two categories of paths:
    - Outbound: all CALLS_FUNC paths up to `depth` hops away from the
      function (i.e. functions it calls, transitively).
    - Inbound: direct CALLS_FUNC edges into the function (one hop only).

    function_fqn is matched exactly and case-sensitively against the
    qualified_name property (e.g. "com.example.Service.process" or
    "module.ClassName.method_name"). When codebase_id is omitted, the lookup
    spans every matching function in the graph.

    Args:
        graph_name: Name of the graph instance (looked up in neo4j_instances table).
        function_fqn: Fully-qualified name of the function (case-sensitive,
            exact match against the qualified_name property).
        codebase_id: Optional codebase filter.
        depth: Maximum number of outbound CALLS_FUNC hops to traverse (default 2).
        limit: Maximum number of paths collected per direction (default 50).
    """
    log.info(
        "getCallChain graph=%s codebase=%s fn=%r depth=%d",
        graph_name,
        codebase_id,
        function_fqn,
        depth,
    )
    _record_activity("getCallChain", graph_name)
    validated_depth = _validate_depth(depth)
    cypher = _build_context_query(
        """
        MATCH (fn:Code:Function {qualified_name: $function_fqn})
        WHERE ($codebase_id IS NULL OR fn.codebase_id = $codebase_id)
        """,
        "fn",
        [
            _Expansion(
                name="outbound_paths",
                var="outbound",
                collect=f"(fn)-[:CALLS_FUNC*1..{validated_depth}]->(:Code:Function)",
            ),
            _Expansion(
                name="inbound_paths",
                var="inbound",
                collect="(caller:Code:Function)-[:CALLS_FUNC]->(fn)",
            ),
        ],
    )
    records = _run_query(
        graph_name,
        cypher,
        params={"codebase_id": codebase_id, "function_fqn": function_fqn, "limit": limit},
        limit=limit,
    )
    return _serialize_records(records, graph_name)


@mcp.tool()
def searchCode(
    graph_name: str,
    query: str,
    codebase_id: str | None = None,
    limit: int = 25,
) -> dict[str, Any] | list[dict[str, Any]]:
    """
    Keyword-search the parser-derived Code layer (File / Function / Type / Variable).

    This is the symmetric, authoritative existence check. The Business /
    UIComponent layer is a lossy LLM projection, so a top-down query that returns
    nothing (getFeatureContext, getDataFlow) is NOT proof the capability is
    absent. Before concluding absence, search the Code layer here — the parser
    sees every file and function regardless of whether the abstraction layer
    modeled it.

    Matches are case-insensitive substring (CONTAINS) against a Code node's name,
    qualified_name, and file_path. Returns the matching Code nodes so callers can
    then drill in with getFileContext or getCallChain using an exact identifier.

    Args:
        graph_name: Name of the graph instance (looked up in neo4j_instances table).
        query: Case-insensitive substring to search for (e.g. "caption", "PaymentService").
        codebase_id: Optional codebase filter.
        limit: Maximum number of nodes to return (default 25).
    """
    log.info(
        "searchCode graph=%s codebase=%s query=%r",
        graph_name,
        codebase_id,
        query,
    )
    _record_activity("searchCode", graph_name)
    records = _run_query(
        graph_name,
        """
        MATCH (n:Code)
        WHERE ($codebase_id IS NULL OR n.codebase_id = $codebase_id)
          AND (
            toLower(coalesce(n.name, '')) CONTAINS toLower($query)
            OR toLower(coalesce(n.qualified_name, '')) CONTAINS toLower($query)
            OR toLower(coalesce(n.file_path, '')) CONTAINS toLower($query)
          )
        RETURN n
        ORDER BY coalesce(n.qualified_name, n.name, n.file_path)
        LIMIT $limit
        """,
        params={"query": query, "codebase_id": codebase_id, "limit": limit},
        limit=limit,
    )
    return _serialize_records(records, graph_name)


_jwks_client: jwt.PyJWKClient | None = None


def _get_jwks_client() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        supabase_url = os.environ["SUPABASE_URL"]
        _jwks_client = jwt.PyJWKClient(f"{supabase_url}/auth/v1/.well-known/jwks.json")
    return _jwks_client


def _validate_jwt(token: str) -> dict:
    header = jwt.get_unverified_header(token)
    algorithm = header.get("alg")
    jwt_secret = os.environ.get("SUPABASE_JWT_SECRET")
    if algorithm == "HS256" and jwt_secret:
        return jwt.decode(
            token, jwt_secret, algorithms=["HS256"], options={"verify_aud": False}
        )
    signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256", "ES256"],
        options={"verify_aud": False},
    )


def _base_url(scope: dict) -> str:
    headers = dict(scope.get("headers", []))
    host = headers.get(b"host", b"").decode()
    return f"https://{host}" if host else ""


def _mcp_resource_url(scope: dict) -> str:
    configured = os.environ.get("MCP_RESOURCE_URL")
    if configured:
        return configured

    base = _base_url(scope)
    if not base:
        return ""

    path = os.environ.get("MCP_RESOURCE_PATH", "/mcp")
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path}"


def _supabase_auth_issuer() -> str:
    return f"{os.environ.get('SUPABASE_URL', '')}/auth/v1"


async def _send_401(scope: dict, send: Any) -> None:
    base = _base_url(scope)
    resource_metadata = f"{base}/.well-known/oauth-protected-resource" if base else ""
    www_auth = 'Bearer realm="deepmorph-orchestrator"'
    if resource_metadata:
        www_auth += f', resource_metadata="{resource_metadata}"'
    www_auth += ', scope="openid email profile"'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                [b"www-authenticate", www_auth.encode()],
                [b"content-type", b"application/json"],
            ],
        }
    )
    await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})


class AuthMiddleware:
    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        if not os.environ.get("SUPABASE_URL"):
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode()

        if not auth_header.startswith("Bearer "):
            if scope.get("type") == "http" and scope.get("path", "").startswith("/.well-known"):
                await self._app(scope, receive, send)
                return
            await _send_401(scope, send)
            return

        token = auth_header[7:]
        try:
            payload = _validate_jwt(token)
        except Exception as exc:
            log.warning("JWT validation failed: %s", exc)
            await _send_401(scope, send)
            return

        email = payload.get("email", "")
        user_domain = email.split("@")[1] if "@" in email else ""
        user_id = payload.get("sub") or None

        domain_tok = _current_user_domain.set(user_domain)
        jwt_tok = _current_user_jwt.set(token)
        id_tok = _current_user_id.set(user_id)
        try:
            await self._app(scope, receive, send)
        finally:
            _current_user_domain.reset(domain_tok)
            _current_user_jwt.reset(jwt_tok)
            _current_user_id.reset(id_tok)


async def _handle_oauth_protected_resource(scope: dict, receive: Any, send: Any) -> None:
    body = json.dumps(
        {
            "resource": _mcp_resource_url(scope),
            "authorization_servers": [_supabase_auth_issuer()],
            "scopes_supported": ["openid", "email", "profile"],
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _handle_oauth_metadata(scope: dict, receive: Any, send: Any) -> None:
    issuer = _supabase_auth_issuer()
    body = json.dumps(
        {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "jwks_uri": f"{issuer}/.well-known/jwks.json",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "scopes_supported": ["openid", "email", "profile"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
                "none",
            ],
            "code_challenge_methods_supported": ["S256"],
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _build_app() -> Any:
    auth_mcp = AuthMiddleware(mcp.streamable_http_app())

    async def dispatch(scope: dict, receive: Any, send: Any) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path == "/.well-known/oauth-protected-resource":
            await _handle_oauth_protected_resource(scope, receive, send)
        elif path == "/.well-known/oauth-authorization-server":
            await _handle_oauth_metadata(scope, receive, send)
        else:
            await auth_mcp(scope, receive, send)

    return dispatch


def _run_http(host: str = "0.0.0.0", port: int = 8000) -> None:
    uvicorn.run(_build_app(), host=host, port=port)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    _run_http(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
