---
name: map
description: Use DeepMorph Orchestrator to map a coding task onto the code knowledge graph for repo and task context. Call at the start of any bug fix, feature implementation, or non-trivial edit before reading files or writing code.
disable-model-invocation: true
allowed-tools: mcp__orchestrator__bootstrap mcp__orchestrator__queryGraph mcp__orchestrator__exploreKnowledgeGraph mcp__orchestrator__namespaceDirectory mcp__orchestrator__traceFlow mcp__orchestrator__searchCode mcp__orchestrator__getDataFlow mcp__orchestrator__getCallChain
---

# /deepmorph:map

Use DeepMorph Orchestrator to query the code knowledge graph before starting
work.

## Working principle: source for truth, graph for relationships

You are running inside a coding agent with full access to the repository. Split
the work by what each side is best at:

- **Your own read / grep / glob tools own the source.** They are current and
  complete locally. Do not use the graph as a plain-text grep substitute.
- **The graph owns cross-cutting relationships** that local search cannot
  cheaply reconstruct: transitive call chains, data lifecycles, modeled
  behavioral flows, and connections across Code, Data, and Business layers.
- **Reconcile graph evidence against source.** The graph reflects the commit in
  `bootstrap().builds[].git_commit_hash`; the working tree may be ahead of it.
  Confirm claims about locally changed files in source.
- **Calibrate negative claims by layer.** The parser-derived Code layer covers
  every indexed codebase. Data, Business, and UI projections are inferred and
  can be incomplete. A missing inferred node or relationship is not evidence
  that a capability is absent.

## Behavior

1. Resolve `graph_name` with `bootstrap` first:
   - Call `bootstrap()`.
   - When `requires_graph_selection` is false and `graph` is present, use it
     silently.
   - When `requires_graph_selection` is true, present `graphs` as a numbered
     list, ask the user to choose, then call
     `bootstrap(graph_name=<selection>)`.
   - When no graphs are available, tell the user and stop graph lookup.
   - Use `bootstrap.schema` as the live storage-shape reference and
     `bootstrap.codebases` as the complete codebase scope for the selected
     graph.
   - Note each entry in `bootstrap.builds`. If the working tree is ahead of its
     `git_commit_hash`, verify affected graph facts in source.

2. Scope only when the question requires it:
   - Omit codebase filters for a single-codebase graph or a graph-wide question.
   - For comparisons or ambiguous names, pass the relevant IDs from
     `bootstrap.codebases` to tools that accept `codebase_ids`, or the single
     relevant ID to tools that accept `codebase_id`.

3. Choose tools using the strategy below. Run independent lookups in parallel
   when the client supports parallel MCP calls.

4. Before repo/filesystem inspection after graph lookup, load the shell tool
   schema first:
   - Call `ToolSearch` with query `select:Bash`.
   - When calling the shell tool, use the required `command` parameter name,
     not `cmd`.

5. Summarize only task-relevant evidence, then continue with the actual coding
   work. Do not stop after graph orientation.

## Schema grounding

- Treat `bootstrap.schema` as the live Neo4j storage shape for the selected
  graph.
- Prefer typed tools because they encode supported graph traversals and
  bounded result behavior.
- Use `queryGraph` only when no typed tool covers the question. Keep the query
  read-only, parameterized, and grounded in `bootstrap.schema`.

## Graph selection example

When `bootstrap()` returns `requires_graph_selection = true`:

> Multiple graphs are available for your account:
> 1. petclinic
> 2. billing-service
>
> Which graph should I use for this task?

Wait for the user's reply before proceeding.

## Query strategy

Map task signals directly to tools:

| Signal | Primary tool | How to use it |
|---|---|---|
| Unfamiliar subsystem, architecture, integration, or broad task | `exploreKnowledgeGraph` | First-pass orientation only. Choose the closest mode and provide specific seed terms. Its balanced sample is not a complete inventory. |
| Focused flow, journey, sequence, stages, or branches | `traceFlow` | Expand the real `NEXT_SCENARIO` branches and `NEXT_STEP` order. Retry with a directory entry when the term is unresolved or diffuse. A `family` resolution intentionally expands every matching namespace under the prefix. |
| Whole-flow coverage, parity, or cross-system flow comparison | `namespaceDirectory`, then `traceFlow` | Pull the complete grouped index first. Classify every journey and namespace, review the exact unclassified count and listed stable identifiers, expand every relevant entry separately, then compare expansions. Never infer steps from a name. |
| Function or method name | `getCallChain` | Exact qualified names resolve directly. A loose name intentionally returns token-scored qualified-name candidates; retry the intended candidate for outbound transitive calls and direct inbound callers. |
| Data entity or model name | `getDataFlow` | Returns lifecycle-function links and peer entity associations. Matching is case-insensitive; retry a returned candidate on a miss. |
| Existence, surface parity, or likely implementation paths | `searchCode` | Search concept terms across every relevant codebase. It returns file-path counts and samples, not file contents or proof of behavior. Inspect positive paths in source; treat zero as unconfirmed, never absent. |
| File/function impact, cross-layer trace, exact inventory, or a question no typed tool covers | `queryGraph` | Write a targeted read-only query from `bootstrap.schema`. Treat sparse Code-to-Business paths as leads, not a complete blast radius. Use count queries when completeness matters. |

### Exploration modes

Use the narrowest suitable `exploreKnowledgeGraph` mode:

- `broad_discovery`: general orientation
- `implementation_trace`: code-to-behavior implementation leads
- `data_flow`: entity lifecycle and transformation leads
- `architecture_inventory`: applications, features, modules, and technologies
- `dependency_surface`: caller, callee, and dependency leads
- `integration_surface`: APIs, clients, auth, webhooks, and external systems

Exploration is bounded by query and item budgets. Coverage flags and gaps say
which passes produced evidence; they do not turn the returned sample into a
complete inventory.

### Flow completeness

For a focused, already-known flow, call `traceFlow` directly. For broad flow
questions, coverage checks, or comparisons:

1. Call `namespaceDirectory` first.
2. Review every journey and namespace, including single-scenario entries. Also
   review the exact unclassified count and every stable identifier returned in
   its bounded listing; narrow with `codebase_ids` when that listing is sampled.
3. Treat unclear entries as in scope until expanded.
4. Call `traceFlow` once for every relevant entry and once per comparison side.
5. Compare ordered steps and alternate, failure, recovery, and background
   branches. Do not compare names alone.

An entry present in the directory but not expanded is unlooked-at, not absent.
`traceFlow` reports `resolution_mode`: `exact` is one selected grouping or stable
scenario identifier, `family` intentionally combines every namespace below a
prefix, and `token` is natural-language resolution. If a stage is missing from
the grouped directory or the bounded unclassified listing, search likely
configuration, route, wizard, page, and component terms with `searchCode`, then
inspect source before making a claim.

### Existence and parity

For whether-something-exists questions and cross-codebase surface comparisons:

1. Build a small set of specific concept terms and useful synonyms.
2. Call `searchCode` across every codebase involved.
3. Inspect returned sample paths in source before describing behavior.
4. Treat `zero_in_scope` as "not confirmed in indexed file paths," not as
   evidence of absence.
5. Use targeted `queryGraph` counts or source inspection when the claim must be
   exhaustive.

Do not use `exploreKnowledgeGraph` for completeness, parity, or absence claims.

### File and impact questions

There is no dedicated file-context or impact tool. Read the file in source,
then use the exact identifiers it contains with `getCallChain`, `getDataFlow`,
or a targeted `queryGraph` traversal. Code-to-behavior relationships are sparse;
report returned connections as affected-behavior leads and never interpret a
silent traversal as "unused" or "no impact."

## Empty and failed results

If a tool returns no useful evidence:

1. For `getCallChain` or `getDataFlow`, retry a matching returned candidate.
2. For an unresolved `traceFlow`, select a precise entry from its directory or
   call `namespaceDirectory` and retry.
3. Search broader concept synonyms with `searchCode` across every relevant
   codebase.
4. State which lookup remained unconfirmed and continue with source analysis.

A tool error is not an empty result. Report it as a failed lookup rather than
turning it into a negative finding.

## Synthesis format

```md
## Graph Context for: <task>

**Relevant code**
- <function or sampled path and why it matters>

**Call and data relationships**
- <caller -> callee, or entity lifecycle>

**Behavioral context**
- <ordered flow, branch, rule, or affected-behavior lead>

**Coverage and uncertainty**
- <scope checked and any evidence that remains unconfirmed>

**Recommended approach**
- <1-3 concrete implications for implementation or debugging>
```

Only include sections with content. Always include `Recommended approach`.
