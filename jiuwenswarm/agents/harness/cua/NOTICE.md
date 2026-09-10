# CUA backport provenance

Source: https://github.com/zuiho-kai/agent-core/tree/98a60864a9e8912fb789d10fe5ae906c453e9b36

The Apache-2.0 licensed CUA capability catalog, prompts, perception/recovery
rails, screenshot retention rail and corresponding capability/rail tests are
adapted from that revision. Original copyright notices and the license are
retained. The source paths are:

- `openjiuwen/harness/tools/cua/cua_capabilities.py`
- `openjiuwen/harness/tools/cua/rails.py`
- `openjiuwen/harness/subagents/cua_agent.py` (prompt constants only)
- `openjiuwen/harness/rails/multimodal_context_summarizer_rail.py`
- `tests/unit_tests/harness/tools/cua/`

Jiuwen-specific changes:

- Keep Jiuwen's pinned agent-core dependency; do not merge unrelated SDK changes.
- Expose a bounded `cua_task` tool instead of modifying SDK subagent factory dispatch.
- Own the MCP connection and driver session inside each invocation, with explicit
  cleanup and a cross-process desktop lease. The upstream runtime registration
  rail is not included.
- Forward MCP `structuredContent` and opt-in images through locally wrapped tools;
  no changes to global MCP clients or SDK packages.
- Instantiate fresh rails/history per task. A no-error progress heuristic is
  labelled `unverified`, never an authoritative completion claim.
- Preserve host tool policies, deny unresolved nested approval requests, and
  expose only the operator-selected capability catalog.
- After merging Xiangyu at dac9611ba, resolve effective Global / User / Session
  permissions before each invocation using the host composition function. The
  SDK pin follows Xiangyu (14a7fe2d0a2bf0cf2a25abd90f05f5ae6a9bf2d5).

Upstream's persistent CUA resume protocol and desktop demo suite are not included.
This implementation's lifecycle, configuration and MCP bridge are maintained in
Jiuwen; see `docs/zh/cua-integration.md` in the repository root.
