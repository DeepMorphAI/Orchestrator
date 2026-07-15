---
name: map
description: Use DeepMorph Orchestrator to map a coding task onto the code knowledge graph for repo and task context. Call at the start of any bug fix, feature implementation, or non-trivial edit before reading files or writing code.
disable-model-invocation: true
allowed-tools: mcp__orchestrator__bootstrap mcp__orchestrator__listGraphs mcp__orchestrator__listCodebases mcp__orchestrator__getFileContext mcp__orchestrator__getFeatureContext mcp__orchestrator__getDataFlow mcp__orchestrator__getCallChain mcp__orchestrator__searchCode mcp__orchestrator__getSchema mcp__orchestrator__queryGraph
---

# /deepmorphai:map

Use DeepMorph Orchestrator to query the code knowledge graph before starting work.

## Behavior

1. Resolve `graph_name` with `bootstrap` first:
   - Call `bootstrap()`.
   - Graph selection:
     - `requires_graph_selection = false` and `graph` is present → use it silently.
     - `requires_graph_selection = true` → present `graphs` as a numbered list and ask the user, then call `bootstrap(graph_name=<selection>)`.
     - Zero graphs → tell the user no graphs are configured and stop.
   - Treat `bootstrap.schema` as the default first-pass graph shape reference.
   - Treat `bootstrap.codebases` as the default codebase discovery result for the selected graph.
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
| File path or file name | `getFileContext` | Pass `codebase_id` only when needed |
| Function / method name | `getCallChain` | Pass `codebase_id` only when needed |
| Data entity / model name | `getDataFlow` | `codebase_id` is optional |
| Feature / scenario name | `getFeatureContext` | |
| Keyword, capability, or unknown exact identifier | `searchCode` | Authoritative existence check against the parser-derived Code layer |
| Question not covered by a typed tool | `queryGraph` | Escape hatch; use the schema from `bootstrap` to write the query |

Multiple signals → call the corresponding tools **in parallel** in one batch.

Example: a bug trace that mentions a file and a function calls
`getFileContext` + `getCallChain` simultaneously, not sequentially.

### File path resolution

If the exact file path is unknown, skip `getSchema` and call `searchCode` with
the filename or path fragment. Use the returned exact `file_path` with
`getFileContext`.

If still empty, tell the user and continue with code-only analysis.

### EXPLORE / no clear signal

Start with `searchCode` using the task's most specific domain terms. Use the
schema already returned by `bootstrap` and `queryGraph` only when no typed tool
covers the question.

## Empty results

If a query returns no useful data:

1. Try one broader `searchCode` lookup against the parser-derived Code layer.
2. Tell the user exactly what lookup failed.
3. Continue with code-only analysis.

Do not silently proceed as if the graph confirmed something it did not.

## Synthesis format

```md
## Graph Context for: <task>

**Relevant code**
- <function> in <file>

**Who calls this code**
- <caller> -> <callee>

**Data entities involved**
- <entity> — created by <fn>, used by <fn>

**Business context**
- <feature / rule / step>

**Recommended approach**
- <1-3 concrete implications for the implementation or bug fix>
```

Only include sections that have content. Always include `Recommended approach`.
