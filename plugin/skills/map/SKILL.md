---
name: map
description: Use DeepMorph Orchestrator to map a coding task onto the code knowledge graph for repo and task context. Call at the start of any bug fix, feature implementation, or non-trivial edit before reading files or writing code.
disable-model-invocation: true
allowed-tools: mcp__orchestrator__bootstrap mcp__orchestrator__listGraphs mcp__orchestrator__listCodebases mcp__orchestrator__getFileContext mcp__orchestrator__getFeatureContext mcp__orchestrator__getDataFlow mcp__orchestrator__getCallChain mcp__orchestrator__getImpact mcp__orchestrator__searchCode mcp__orchestrator__getSchema mcp__orchestrator__queryGraph
---

# /deepmorphai:map

Use DeepMorph Orchestrator to query the code knowledge graph before starting work.

## Working principle: you have the source, the graph has what grep can't

You are running inside a coding agent with full access to the repository. So
split the work by what each side is best at:

- **Your own tools (read / grep / glob) own the source.** Reading a file,
  searching for text, and listing symbols are faster, current, and complete
  locally. Do not use the graph as a grep substitute.
- **The graph owns the cross-cutting, graph-only knowledge** a local search
  cannot cheaply reconstruct: who calls a function (reverse edges), transitive
  call and data-lifecycle chains, and the business/behavior layer (which
  feature / rule / scenario a piece of code serves). That is what you query it
  for.
- **Reconcile against the source.** The graph was built at a specific commit
  (`bootstrap().builds[].git_commit_hash`); your working tree may be ahead of
  it. Treat graph facts about files you have locally changed as possibly stale
  and confirm them against the actual source before relying on them.

## Behavior

1. Resolve `graph_name` with `bootstrap` first:
   - Call `bootstrap()`.
   - Graph selection:
     - `requires_graph_selection = false` and `graph` is present → use it silently.
     - `requires_graph_selection = true` → present `graphs` as a numbered list and ask the user, then call `bootstrap(graph_name=<selection>)`.
     - Zero graphs → tell the user no graphs are configured and stop.
   - Treat `bootstrap.schema` as the default first-pass graph shape reference.
   - Treat `bootstrap.codebases` as the default codebase discovery result for the selected graph.
   - Note `bootstrap.builds` — each codebase's `git_commit_hash` is the commit the graph was built from. If your working tree is ahead of it, graph facts about locally-changed files may be stale; confirm those against the source.
   - Call `getSchema(graph_name)` only if `bootstrap.schema` is empty or you need a refreshed live schema for a specific graph.

2. Use `codebase_id` only as a disambiguation filter:
   - Do not start with `listCodebases` by default.
   - Most tasks can call `getFileContext`, `getCallChain`, or `getDataFlow` without specifying a codebase.
   - Prefer `bootstrap.codebases` when available.
   - Call `listCodebases(graph_name)` only when you need a refreshed list or the selected graph was not bootstrapped yet.

3. Query the graph using the named tools (see Query strategy below).

4. Before any repo/filesystem inspection after graph lookup, load the shell tool schema first:
   - Call `ToolSearch` with query `select:Bash`.
   - When calling the shell tool, use the required `command` parameter name, not `cmd`.

5. Synthesize results into a short task-oriented summary, then proceed with the actual work.

## Schema grounding

- Treat `bootstrap.schema` / `getSchema` as the live storage shape for the selected graph.
- Prefer typed tools because they encode the supported graph traversals.
- Use `queryGraph` only when the live schema shows that no typed tool covers the question.

## Graph selection example

When `bootstrap()` returns `requires_graph_selection = true`:

> Multiple graphs are available for your account:
> 1. petclinic
> 2. billing-service
>
> Which graph should I use for this task?

Wait for the user's reply before proceeding.

## Query strategy

Map task signals directly to tool calls — no intermediate classification step.

| Signal in the task | Primary tool | Notes |
|---|---|---|
| About to change a file/function — what behavior depends on it | `getImpact` | Blast radius: forward trace to the Features / Scenarios / Rules it reaches. The graph-only "what breaks if I touch this" check |
| File path or file name | `getFileContext` | Reverse/cross-layer context (callers, data, steps, UI); the file's own declarations you can just read. Pass `codebase_id` only when needed |
| Function / method name | `getCallChain` | Transitive callers/callees. Pass `codebase_id` only when needed |
| Data entity / model name | `getDataFlow` | `codebase_id` is optional |
| Feature / scenario name | `getFeatureContext` | |
| Need the exact graph identifier for a loosely-known term | `searchCode` | Resolve a term to an exact `file_path` / `qualified_name`, then call the typed tool. Also the authoritative parser-layer existence check. For plain-text source search, prefer your own grep |
| Question not covered by a typed tool | `queryGraph` | Escape hatch; use the schema from `bootstrap` to write the query |

Multiple signals → call the corresponding tools **in parallel** in one batch.

Example: a bug trace that mentions a file and a function calls
`getFileContext` + `getCallChain` simultaneously, not sequentially. Before an
edit, add `getImpact` on the file/function you are about to change.

### Anchor misses (candidates)

The anchored tools (`getFileContext`, `getFeatureContext`, `getDataFlow`,
`getCallChain`) match an exact identifier. On a miss they return
`{"matched": false, "candidates": [...]}` rather than empty. If a candidate
names what you meant, retry with it. Do **not** read a miss as absence.

### File path resolution

If the exact file path is unknown, resolve it with your own tools (you have the
source) or call `searchCode` with the filename/path fragment. Use the exact
`file_path` with `getFileContext`.

If still empty, tell the user and continue with code-only analysis.

### EXPLORE / no clear signal

Start with `searchCode` using the task's most specific domain terms. Use the
schema already returned by `bootstrap` and `queryGraph` only when no typed tool
covers the question.

## Empty results

If a query returns no useful data:

1. If it was an anchored miss, retry with one of the returned `candidates`.
2. Otherwise try one broader `searchCode` lookup against the parser-derived Code layer.
3. Tell the user exactly what lookup failed.
4. Fall back to your own tools (read / grep) — you have the source — and continue with code-only analysis.

Do not silently proceed as if the graph confirmed something it did not, and do
not read an empty graph result as proof the code is absent.

## Synthesis format

```md
## Graph Context for: <task>

**Relevant code**
- <function> in <file>

**Who calls this code**
- <caller> -> <callee>

**Blast radius** (from `getImpact`, when editing)
- <feature / scenario / rule> reached in <n> hops

**Data entities involved**
- <entity> — created by <fn>, used by <fn>

**Business context**
- <feature / rule / step>

**Recommended approach**
- <1-3 concrete implications for the implementation or bug fix>
```

Only include sections that have content. Always include `Recommended approach`.
