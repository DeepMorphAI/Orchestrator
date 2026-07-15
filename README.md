# DeepMorph Orchestrator

DeepMorph Orchestrator connects coding agents to DeepMorph code knowledge graphs
through a read-only, hosted MCP server. This repository ships the Claude Code
plugin that connects to it — the server itself is hosted, so nothing here needs
to run locally.

## Install in Claude Code

```text
/plugin marketplace add DeepMorphAI/Orchestrator
/plugin install deepmorphai@orchestrator
/reload-plugins
```

Run `/deepmorphai:map` at the start of a bug fix, feature, or non-trivial edit
to map the task onto the code knowledge graph before reading files or writing
code. On first use, Claude Code opens a browser OAuth flow and stores the token
automatically.

## What it does

The `/deepmorphai:map` skill queries a three-layer knowledge graph — **Code**
(files, functions, types), **Data** (entities and their lifecycle), and
**Business** (features, rules, scenarios, steps) — and synthesizes a focused,
task-oriented context summary to ground the work that follows.

## Repository layout

- `plugin/` — the Claude Code plugin: the MCP connection (`.mcp.json`) and the
  `/deepmorphai:map` context skill.
- `.claude-plugin/marketplace.json` — publishes the plugin through this
  repository's marketplace.
