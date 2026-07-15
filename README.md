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

The `/deepmorph:map` skill queries a three-layer knowledge graph — **Code**
(files, functions, types), **Data** (entities and their lifecycle), and
**Business** (features, rules, scenarios, steps) — and synthesizes a focused,
task-oriented context summary to ground the work that follows.

It is built for a coding agent that already has your source. Rather than
duplicating what a local grep does, it leans on what the graph knows and your
filesystem doesn't:

- **Reverse and transitive relationships** — who calls a function, and the
  call/data-lifecycle chains that span many files.
- **Blast radius before an edit** — `getImpact` traces forward from the code you
  are about to change to the business features, scenarios, and rules that depend
  on it, so you know what a change touches.
- **The business/behavior layer** — the features, rules, and scenarios a piece
  of code serves.

The skill reads your files and searches text with your own tools, uses the graph
for the cross-cutting knowledge above, and reconciles the graph (built at a
specific commit) against your working tree so stale facts don't slip through.

## Repository layout

- `plugin/` — the Claude Code plugin: the MCP connection (`.mcp.json`) and the
  `/deepmorph:map` context skill.
- `.claude-plugin/marketplace.json` — publishes the plugin through this
  repository's marketplace.
