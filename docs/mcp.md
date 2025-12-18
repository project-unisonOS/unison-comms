# MCP Tool Surface (unison-comms)

`unison-comms` exposes a lightweight MCP-style tool surface (HTTP) so the Capability Resolver can invoke comms actions as manifest-declared capabilities.

## Endpoints

- Registry: `GET /mcp/registry`
  - Returns a `servers[]` list with `base_url` and `tools[]` entries.
- Tool calls: `POST /tools/{tool_name}`
  - Body: `{ "arguments": { ... } }`
  - Returns the tool result as JSON.

## Tool names

- `comms.check`
- `comms.summarize`
- `comms.reply`
- `comms.compose`
- `comms.join_meeting`
- `comms.prepare_meeting`
- `comms.debrief_meeting`

## Notes

- In UnisonOS, planners should not call comms services directly; they should resolve and run comms capabilities through `unison-capability` per the planner contract.
- Network egress and allowlists are enforced by the resolver at runtime.

