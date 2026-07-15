# DeepMorph Orchestrator

DeepMorph Orchestrator connects coding agents to DeepMorph code knowledge graphs
through a read-only MCP server.

## Install in Claude Code

```text
/plugin marketplace add DeepMorphAI/Orchestrator
/plugin install deepmorphai@orchestrator
/reload-plugins
```

Run `/deepmorphai:map` to gather graph context for a coding task.

## Repository layout

- `ark-plugin/` contains the Claude Code plugin, MCP connection, and context skill.
- `ark-mcp/` contains the hosted MCP server, deployment configuration, and tests.
- `.claude-plugin/marketplace.json` publishes the plugin through this repository.

See [ark-mcp/README.md](./ark-mcp/README.md) for local server configuration,
authentication, tools, and Cloud Run deployment.
