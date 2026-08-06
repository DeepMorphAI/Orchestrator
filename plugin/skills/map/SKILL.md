---
name: map
description: Use DeepMorph Orchestrator to map a coding task onto the code knowledge graph for repo and task context. Call at the start of any bug fix, feature implementation, or non-trivial edit before reading files or writing code.
disable-model-invocation: true
allowed-tools: mcp__orchestrator__bootstrap mcp__orchestrator__queryGraph mcp__orchestrator__exploreKnowledgeGraph mcp__orchestrator__namespaceDirectory mcp__orchestrator__traceFlow mcp__orchestrator__searchCode mcp__orchestrator__getDataFlow mcp__orchestrator__getCallChain
---

# /deepmorph:map

Query the code knowledge graph before starting work, and again whenever the
task asks a relational question.

## What the graph knows that grep cannot

The graph is one connected model of every indexed codebase, in four layers.
None of these relationships are recoverable from file contents:

- **Code** (parser-derived, complete for indexed codebases): transitive call
  chains (`CALLS_FUNC`), type dependencies (`EXTENDS_TYPE`, `DEPENDS_TYPE`,
  `INPUTS_TYPE`, `OUTPUTS_TYPE`, `THROWS_TYPE`), and code-to-artifact links
  (`RENDERS`, `READS_CONFIG`, `USES_QUERY`). Nodes: `Code:File`, `Code:Type`,
  `Code:Function`, `Code:Variable`, `Code:Artifact`.
- **UI**: `Business:UIComponent` nodes carry a `role` (`page`, `screen`,
  `surface`). `HAS_UI` is containment (page contains form contains button),
  `NAVIGATES_TO` is ordered navigation between surfaces, `USES_RESOURCE`
  links a component to the source file implementing it, `USES_ARTIFACT` to a
  template, `USES_TYPE` to the domain types it shows.
- **Business**: journeys, scenarios, and ordered steps (`NEXT_SCENARIO`,
  `NEXT_STEP`) plus rules and features, linked to implementing functions
  (`USES_FUNC`), to entities (`USES_ENTITY`), and to surfaces (`USES_UI`).
- **Data**: `Data:DataEntity` lifecycles — the functions that create, use,
  transform, validate, filter, and save each entity (`CREATED_BY`, `USED_BY`,
  `TRANSFORMED_BY`, `VALIDATED_BY`, `FILTERED_BY`, `SAVED_BY`) — plus entity
  associations (`ASSOCIATES_ONE`, `ASSOCIATES_MANY`).

The layers are connected, so navigation works top-down as well as bottom-up.
You can enter at the Business layer — a journey, scenario, feature, or rule
named in product terms — and descend through `USES_FUNC`, `USES_UI`, and
`USES_ENTITY` to the exact functions, surfaces, and entities that implement
it, before opening a single file. When a task arrives in business language
with no code anchor yet, start there (`namespaceDirectory`, `traceFlow`)
instead of guessing grep terms.

Grouping, ownership, navigation, behavioral attribution, data responsibility,
and impact are therefore graph questions. Local tools cannot answer them: a
hand-built import graph over-attributes every shared module and sees none of
the UI, Business, or Data edges above.

## Working principle: source for facts, graph for relationships

- **Local read / grep / glob own lexical and implementation facts** — exact
  strings, file contents, final verification. Do not use the graph as grep.
- **Finding source is the beginning of relational analysis, not the end.** A
  grep hit locates a label or identifier; it cannot justify grouping pages,
  assigning behavioral ownership, or describing data responsibility. Carry
  the matched paths, symbols, and type names into the graph as anchors.
- **Expect graph output to be less predictable than grep output.** That is
  what makes it informative. Preferring the tool whose result you can already
  predict is a bias toward what you control, not a judgment about what is
  useful — and it is how known-wrong groupings get shipped. The graph-usage
  line in the synthesis format exists to catch exactly this.
- **Reconcile graph evidence against source.** The graph reflects the commit
  in `bootstrap().builds[].git_commit_hash`; the working tree may be ahead of
  it. Confirm claims about locally changed files in source.
- **Calibrate negative claims by layer.** Code is parser-complete for indexed
  codebases; UI, Business, and Data are inferred projections and can be
  incomplete. A missing inferred node or edge is never evidence of absence.

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
   - Treat `bootstrap.flow_directory` as the complete unscoped journey and
     namespace index. Review its journeys, multi-scenario namespaces,
     singletons, and unclassified identifiers before deciding which behavioral
     areas are relevant.
   - Note each entry in `bootstrap.builds`. If the working tree is ahead of
     its `git_commit_hash`, verify affected graph facts in source.

2. Scope only when the question requires it:
   - Omit codebase filters for a single-codebase graph or a graph-wide
     question.
   - For comparisons or ambiguous names, pass the relevant IDs from
     `bootstrap.codebases` to tools that accept `codebase_ids`, or the single
     relevant ID to tools that accept `codebase_id`.

3. Minimum engagement. Before concluding, check: does the answer claim
   anything about grouping, ownership, flow, navigation, dependency, data, or
   impact? Every such claim needs a graph lookup behind it — at least one
   query beyond `bootstrap`, re-aimed once through the empty-result ladder if
   the first result is thin. One thin result is a prompt to change tool or
   anchors, not permission to fall back to local reconstruction. If the graph
   is skipped for such a claim anyway, the graph-usage line must say so and
   give the reason. Run independent lookups in parallel when the client
   supports parallel MCP calls.
   Questions about rules, valid values, eligibility, compatibility,
   exclusions, conditions, or state-specific behavior must first be matched
   against every plausible entry in `bootstrap.flow_directory`; expand each
   plausible entry separately with `traceFlow` before answering.

4. Before repo/filesystem inspection after graph lookup, load the shell tool
   schema first:
   - Call `ToolSearch` with query `select:Bash`.
   - When calling the shell tool, use the required `command` parameter name,
     not `cmd`.

5. Summarize only task-relevant evidence, then continue with the actual
   coding work. Do not stop after graph orientation.

## Graph selection example

When `bootstrap()` returns `requires_graph_selection = true`:

> Multiple graphs are available for your account:
> 1. petclinic
> 2. billing-service
>
> Which graph should I use for this task?

Wait for the user's reply before proceeding.

## Worked example: grouping and ownership

Task: a grep for UI label strings found 14 component files; the task asks how
the pages group and what behavior each group serves.

1. Keep the exact file paths and component names from grep as anchors.
2. Call `exploreKnowledgeGraph(goal="page grouping and ownership for <area>",
   seed_terms=[<paths and component names>])` to surface the matching
   `UIComponent` nodes and their connected Business/Code context.
3. Where the sample does not expose the needed relationship, run targeted
   `queryGraph` traversals grounded in `bootstrap.schema`. Bind and return
   paths and relationships so `queryGraph` preserves the grouping structure.
   For containment and implementation:

   ```cypher
   MATCH path=(p:Business:UIComponent {role: 'page'})-[:HAS_UI*0..3]->(c:Business:UIComponent)
   MATCH (c)-[resource:USES_RESOURCE]->(f:Code:File)
   WHERE f.file_path IN $paths
   RETURN path, resource, f
   ```

   For behavioral ownership from the Feature, Scenario, or Step side:

   ```cypher
   MATCH (owner:Business)-[uses:USES_UI]->(surface:Business:UIComponent)
   MATCH path=(surface)-[:HAS_UI*0..3]->(c:Business:UIComponent)
   MATCH (c)-[resource:USES_RESOURCE]->(f:Code:File)
   WHERE f.file_path IN $paths
   RETURN owner, uses, path, resource, f
   ```
4. Reconcile the resulting grouping against source, especially files changed
   locally since the graph's build commit.

The same shape applies to any relational task: anchors from source → typed
tool or exploration → targeted traversal → reconcile against source.

## Query strategy

Prefer typed tools; they encode supported traversals and bounded results. Use
`queryGraph` only when no typed tool covers the question, keeping it
read-only, parameterized, and grounded in `bootstrap.schema`.

| Signal | Primary tool | How to use it |
|---|---|---|
| Rules, valid values, eligibility, compatibility, exclusions, conditions, or state-specific behavior | `bootstrap.flow_directory`, then `traceFlow` | Match every plausible journey, namespace, singleton, and listed unclassified identifier. Expand each plausible entry separately; an unexpanded entry is unlooked-at, not irrelevant. |
| Task stated in product/business terms with no code anchor yet | `bootstrap.flow_directory`, then `traceFlow` and descend | Match plausible journeys and namespaces from bootstrap first; use `namespaceDirectory` only for a scoped refresh. Expand the flow, then follow `USES_FUNC`, `USES_UI`, and `USES_ENTITY` links into implementing code. |
| Unfamiliar subsystem, architecture, integration, or broad task | `exploreKnowledgeGraph` | First-pass orientation only. Choose the closest mode and provide specific seed terms. Its balanced sample is not a complete inventory. |
| Focused flow, journey, sequence, stages, or branches | `traceFlow` | Expand the real `NEXT_SCENARIO` branches and `NEXT_STEP` order. Retry with a directory entry when the term is unresolved or diffuse. A `family` resolution intentionally expands every matching namespace under the prefix. |
| Whole-flow coverage, parity, or cross-system flow comparison | `bootstrap.flow_directory`, then `traceFlow` | Use the complete bootstrap index, or call `namespaceDirectory` for a codebase-scoped refresh. Expand every relevant entry separately, then compare expansions. Never infer steps from a name. |
| Function or method name | `getCallChain` | Exact qualified names resolve directly. A loose name returns token-scored candidates; retry the intended candidate for outbound transitive calls and direct inbound callers. |
| Data entity or model name | `getDataFlow` | Returns lifecycle-function links and peer entity associations. Matching is case-insensitive; retry a returned candidate on a miss. |
| Existence, surface parity, or likely implementation paths | `searchCode` | Search concept terms across every relevant codebase. Returns file-path counts and samples, not contents or proof of behavior. Inspect positive paths in source; treat zero as unconfirmed, never absent. |
| Source search found files/components and the task asks how they group, what owns them, or what behavior they implement | `exploreKnowledgeGraph`, then targeted `queryGraph` | Follow the worked example above: seed with exact paths and symbols, follow the returned Business/UI/Data/Code connections, then traverse. |
| File/function impact, cross-layer trace, exact inventory, or a question no typed tool covers | `queryGraph` | Write a targeted read-only query from `bootstrap.schema`. Start from exact Code anchors and traverse the connected Business, UI, and Data edges. Use count queries when completeness matters. |

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
complete inventory. Do not use `exploreKnowledgeGraph` for completeness,
parity, or absence claims.

### Namespace anchors

A namespace is both the unit of modeled-flow completeness and reusable graph
vocabulary. Seeing a namespace in the directory is discovery, not coverage.
Before any complete/all/other-conditions claim:

1. Build a namespace coverage ledger from every plausible journey, namespace,
   singleton, and listed unclassified identifier in `bootstrap.flow_directory`.
2. Include plausible sibling namespaces, not only the closest lexical match.
3. Mark each entry `expanded` only after a separate `traceFlow` call, or
   `excluded` with graph or source evidence tied to the question.
4. If the unclassified list is truncated, narrow with a codebase-scoped
   `namespaceDirectory` call and verify the remaining scope in source. Do not
   make a definitive completeness claim while relevant entries are unaccounted.

After coverage is established, reuse each relevant namespace across tools:

1. Pass the exact namespace to `traceFlow` to expand its scenarios and ordered
   steps. For a namespace family, use a directory prefix that resolves with
   `resolution_mode: family`, and verify every returned namespace is relevant.
2. Pass the namespace and its meaningful leaf terms as `seed_terms` to
   `exploreKnowledgeGraph`. Use `implementation_trace` to connect the behavior
   to Business/UI/Code context or `data_flow` to connect it to entity lifecycle
   evidence.
3. Use the namespace as a parameter in `queryGraph` for exact or prefix-scoped
   traversal from `Scenario` nodes through `HAS_STEP`, `USES_FUNC`, `USES_UI`,
   and `USES_ENTITY`. Bind and return every relationship variable so the result
   preserves those connections.
4. Feed qualified function names found by the traversal into `getCallChain`,
   and entity names into `getDataFlow`.
5. `searchCode` searches file paths, not namespace properties. Use meaningful
   namespace segments and source synonyms as search terms, then inspect the
   returned paths; do not assume the dotted namespace itself is a file path.

### Flow completeness

For a focused, already-known flow, call `traceFlow` directly. For broad flow
questions, coverage checks, or comparisons, start with
`bootstrap.flow_directory`. Call `namespaceDirectory` only for a codebase-scoped
refresh or when the bootstrap directory is unavailable:

1. Review every journey and namespace, including single-scenario entries, the
   exact unclassified count, and every stable identifier in its bounded
   listing; narrow with `codebase_ids` when that listing is sampled.
2. Treat unclear entries as in scope until expanded. An entry present in the
   directory but not expanded is unlooked-at, not absent.
3. Call `traceFlow` once per relevant entry and once per comparison side, then
   compare ordered steps and alternate, failure, recovery, and background
   branches. Do not compare names alone.

`traceFlow` reports `resolution_mode`: `exact` is one selected grouping or
stable scenario identifier, `family` combines every namespace below a prefix,
`token` is natural-language resolution. If a stage is missing from the
directory, search likely configuration, route, wizard, page, and component
terms with `searchCode`, then inspect source before making a claim.

### Existence and parity

1. Build a small set of specific concept terms and useful synonyms.
2. Call `searchCode` across every codebase involved.
3. Inspect returned sample paths in source before describing behavior.
4. Treat `zero_in_scope` as "not confirmed in indexed file paths," never as
   evidence of absence.
5. Use targeted `queryGraph` counts or source inspection when the claim must
   be exhaustive.

### File and impact questions

There is no dedicated file-context or impact tool. Read the file in source,
then use the exact identifiers it contains with `getCallChain`, `getDataFlow`,
`exploreKnowledgeGraph`, or a targeted `queryGraph` traversal from the Code
anchor into its connected Business, UI, and Data context. If the traversal is
silent, fall back to source analysis and never interpret silence as "unused"
or "no impact."

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

**Namespace coverage**
- <for completeness claims: entries expanded, entries excluded with reasons,
  and unclassified/truncated coverage>

**Graph usage**
- <tools called with one-line yield each; tools deliberately skipped and the
  reason. "Local tools were faster/more familiar" is a bias, not a reason.>

**Recommended approach**
- <1-3 concrete implications for implementation or debugging>
```

Only include sections with content. Always include `Graph usage` and
`Recommended approach`. Include `Namespace coverage` for every completeness,
parity, rules, valid-values, eligibility, compatibility, exclusions, conditions,
or state-specific answer.
