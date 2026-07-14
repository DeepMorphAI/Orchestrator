# DeepMorph Orchestrator

DeepMorph's read-only MCP server for querying Neo4j code knowledge graphs.

Supports two modes:

- **Local** — connects directly to a Neo4j instance via environment variables.
- **Cloud** — authenticates users via Supabase JWT, then looks up per-user Neo4j
  credentials from the `neo4j_instances` table.

## Quickstart (local)

Copy `.env.example` to `.env` and fill in your Neo4j credentials:

```env
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=deepmorphdotai
NEO4J_DATABASE=neo4j
```

Start the server:

```bash
uv run python main.py --host 127.0.0.1 --port 8000
```

## Cloud deployment (Cloud Run)

```bash
./deploy.sh [PROJECT_ID] [REGION]
```

The script regenerates `requirements.txt` from `ark-mcp/uv.lock`, copies the
source tree (resolving symlinks) into a temp build context, and deploys to Cloud
Run via `gcloud run deploy`. The following secrets must exist in Secret Manager
before deploying:

| Secret name                  | Value                          |
|------------------------------|--------------------------------|
| `next_public_supabase_url`   | `https://<project>.supabase.co`|
| `next_public_supabase_anon_key` | Supabase anon key           |
| `supabase_jwt_secret`        | Supabase JWT secret            |

After deployment, the MCP endpoint is:

```
https://<service>-<hash>-<region>.run.app/mcp
```

## Authentication

When `SUPABASE_URL` is set, the server requires a Bearer JWT on every request.
The JWT is validated against the Supabase JWKS endpoint (RS256/ES256) or the
`SUPABASE_JWT_SECRET` (HS256). Requests without a valid token receive `401`.

Two discovery endpoints are served unauthenticated for MCP OAuth:

- `GET /.well-known/oauth-protected-resource` — RFC 9728 resource metadata
- `GET /.well-known/oauth-authorization-server` — OAuth 2.0 server metadata

## Installing in Claude Code

Install the DeepMorph plugin from this repository's marketplace:

```text
/plugin marketplace add DeepMorphAI/Orchestrator
/plugin install orchestrator@deepmorph
```

The plugin configures the hosted MCP server and provides the `/orchestrator:context`
skill. Claude Code opens a browser OAuth flow on first use and stores the token
automatically.

To configure the MCP server without the plugin, add this to a project-level
`.mcp.json`:

```json
{
  "mcpServers": {
    "orchestrator": {
      "type": "http",
      "url": "https://<service>.run.app/mcp",
      "oauth": {
        "authServerMetadataUrl": "https://<project>.supabase.co/auth/v1/.well-known/openid-configuration"
      }
    }
  }
}
```

## Querying model

The graph has four layers: **Code** (parser-derived files, functions, types, and
variables — the ground truth), **Business** (features, rules, scenarios, steps),
**UIComponent** (pages / surfaces), and **Data** (domain entities and their
lifecycle). The Business, UIComponent, and Data layers are LLM projections *over*
the Code layer and may be incomplete — so an empty result there is not proof of
absence. Confirm against the Code layer (`searchCode`, then `getFileContext` /
`getCallChain`) before concluding a capability is missing.

Prefer the **typed tools**: they encode the correct traversal for a question
deterministically — reconstructing flow order from `NEXT_STEP`, reaching the UI
layer via `USES_UI`, grounding a page to its file via `USES_RESOURCE`.
`queryGraph` is an **escape hatch** for questions no typed tool covers; it runs
raw, model-authored Cypher and is the least reliable path.

A typical session: call `bootstrap()` to resolve the graph and its schema, then a
typed tool (`getFeatureContext`, `getFileContext`, `getDataFlow`, `getCallChain`,
or `searchCode`) to pull a focused subgraph.

## Tools

| Tool | Description |
|------|-------------|
| `bootstrap(graph_name?)` | Returns the initial Orchestrator query context — resolves the graph, schema snapshot, and codebases in one call. Selection prefers the caller's *entitled* graphs (those granted to their email/domain via `loom_access`) over broadly-public OSS/demo graphs, so an enterprise user with a single org graph is auto-selected even when public graphs are also visible. A selection prompt appears only when several entitled graphs exist (offering just those) or the caller has none. |
| `listGraphs()` | Lists Neo4j graphs available to the authenticated user and includes a convenience schema snapshot from the first graph. Returns `["local"]` in local mode. |
| `queryGraph(graph_name, cypher, params, limit)` | Escape hatch for read-only Cypher no typed tool covers — prefer the typed tools below. Returns `{nodes, edges}` for graph results or a flat list for scalars. Surfaces a Neo4j-5 dialect hint on syntax errors. |
| `getSchema(graph_name)` | Returns live node labels and relationship types with their properties. |
| `listCodebases(graph_name)` | Returns distinct non-empty `codebase_id` values from `Code` nodes. Mainly useful as a low-level refresh or ambiguous cross-codebase lookup helper. |
| `searchCode(graph_name, query, codebase_id?, limit)` | Case-insensitive substring search over the Code layer (name / qualified_name / file_path). The symmetric existence check: use it before concluding a capability is absent when a Business-layer query returns nothing. |
| `getFileContext(graph_name, file_path, codebase_id?, limit)` | File node + owned code, inbound callers, linked data entities, business steps, and UI pages grounded to the file (`USES_RESOURCE`). `codebase_id` is an optional scope filter. |
| `getFeatureContext(graph_name, feature_name, limit)` | Feature + rules, scenarios, steps, implementing functions, linked data entities, `NEXT_STEP` ordering, and UI pages (`USES_UI`). |
| `getDataFlow(graph_name, entity_name, codebase_id?, limit)` | Data entity lifecycle edges to functions and peer entity associations. `codebase_id` is optional. |
| `getCallChain(graph_name, function_fqn, codebase_id?, depth, limit)` | Function + outbound `CALLS_FUNC` paths and inbound callers. `codebase_id` is an optional scope filter. |


## Usage activity

Every tool call is recorded as lightweight per-user activity (a "who is using MCP,
and how much" signal — the server makes no LLM calls, so there is nothing to meter
against the app's token budget). Recording never touches the request path: each
call bumps an in-process per-user counter, and a background daemon drains all
buffered users to Supabase every 30s via the `record_mcp_activity` RPC, coalescing
a burst of calls into one write that carries the accumulated count. Idle users are
evicted on drain so the in-memory state stays bounded. The RPC is `SECURITY
DEFINER` and granted to `authenticated`, so the write uses the caller's own JWT (no
service-role key). It is best-effort by design: failures are swallowed and a
restart drops the un-flushed tail (at most one interval's worth). Only cloud mode
records activity; local mode (no `SUPABASE_URL`) is a no-op. See
`ark-app/supabase/migrations/046_mcp_activity.sql`.

## Notes

- All tools open Neo4j sessions with `READ_ACCESS`; write operations are rejected by the database.
- Use `params` with `queryGraph` instead of interpolating values into Cypher strings.
- Every returned node and edge carries a `graph` field (the source `graph_name`), so results from multi-graph comparisons cannot be misattributed.
- Node/edge properties are trimmed to keep responses within Cloud Run's size limit: machine-only fields (e.g. `content_sha256`) are dropped and any string value over 1000 chars is truncated with a `…[truncated]` marker. This applies to every tool that returns nodes/edges, `queryGraph` included. To read a raw, untrimmed value, return it as a scalar column via `queryGraph` (e.g. `RETURN n.content_sha256`) — scalar columns are passed through untouched.
- Start with `bootstrap()` for graph discovery, first-pass schema, and codebase discovery in one call.
- Use `listGraphs()` as a lower-level discovery helper when you only need the graph list and a convenience schema snapshot.
- Call `getSchema(graph_name)` when you need a refreshed live schema for a specific graph.
- In most single-codebase graphs, you can omit `codebase_id` and only use `listCodebases()` for disambiguation.
- `graph_name` is passed to every tool and used to look up the correct Neo4j instance in cloud mode.
