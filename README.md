# DeepMorph Orchestrator

DeepMorph Orchestrator connects coding agents to DeepMorph code knowledge graphs
through a read-only, hosted MCP server. This repository ships the Claude Code
plugin that connects to it — the server itself is hosted, so nothing here needs
to run locally.

## Install in Claude Code

```text
/plugin marketplace add DeepMorphAI/Orchestrator
/plugin install deepmorph@orchestrator
/reload-plugins
```

Run `/deepmorph:map` at the start of a bug fix, feature, or non-trivial edit
to map the task onto the code knowledge graph before reading files or writing
code. On first use, Claude Code opens a browser OAuth flow and stores the token
automatically.

## What it does

The `/deepmorph:map` skill queries a four-layer knowledge graph:

- **Code** — parser-derived files, functions, types, and variables.
- **Business** — features, rules, scenarios, and ordered steps.
- **UIComponent** — pages, screens, and significant product surfaces.
- **Data** — domain entities and their lifecycle relationships.

It synthesizes a focused, task-oriented context summary, then continues with
the coding work.

It is built for a coding agent that already has your source. Rather than
duplicating what a local grep does, it leans on what the graph knows and your
filesystem doesn't:

- **Reverse and transitive relationships** — who calls a function, and the
  call/data-lifecycle chains that span many files.
- **Behavioral flow reconstruction** — real scenario branches and step order
  from `NEXT_SCENARIO` and `NEXT_STEP`, rather than guesses based on names.
- **Cross-layer orientation** — bounded exploration across application,
  business, data, code, and platform evidence.
- **Indexed code coverage** — per-codebase file-path counts and samples for
  existence and parity checks.

The skill reads your files and searches text with your own tools, uses the graph
for the cross-cutting knowledge above, and reconciles the graph (built at a
specific commit) against your working tree so stale facts don't slip through.
The Code layer is parser-derived; Business, UIComponent, and Data projections
may be incomplete, so the skill verifies negative claims against source.

## MCP tools

| Tool | Purpose |
|---|---|
| `bootstrap` | Select a graph and return its schema, codebases, and graph build provenance. |
| `exploreKnowledgeGraph` | Run bounded first-pass orientation across graph layers with coverage and gap reporting. |
| `namespaceDirectory` | Return the complete grouped flow index plus exact unclassified coverage and bounded stable identifiers. |
| `traceFlow` | Expand exact flows, namespace families, or natural-language matches into scenario branches and stable step order. |
| `searchCode` | Search parser-authoritative file paths and report per-codebase counts, samples, and zero-match scopes. |
| `getCallChain` | Trace outbound calls and direct inbound callers; loose names return qualified-name candidates for retry. |
| `getDataFlow` | Trace entity lifecycle functions and peer data relationships. |
| `queryGraph` | Run a targeted, parameterized, read-only Cypher query when no typed tool covers the question. |

`exploreKnowledgeGraph` is an orientation tool, not a complete inventory.
`searchCode` returns likely source locations, not file contents or proof of
behavior. Empty inferred-layer results and zero file-path matches are treated as
unconfirmed rather than proof that something is absent.

## Repository layout

- `plugin/` — the Claude Code plugin: the MCP connection (`.mcp.json`) and the
  `/deepmorph:map` context skill.
- `.claude-plugin/marketplace.json` — publishes the plugin through this
  repository's marketplace.
