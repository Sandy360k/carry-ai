"""
carry-ai/mcp/ — Model Context Protocol (MCP) Package
======================================================

Enables carry-ai to connect to external MCP servers, exposing their
tools to the agent. This follows the MCP specification for tool
discovery, invocation, and resource access.

Inspired by claw-code's MCP integration (mcp_client.rs, mcp_tool_bridge.rs)
and Onyx's MCP tool implementation.

Submodules:
    client   — Transport layer: connect to MCP servers via stdio, SSE, HTTP
    registry — Global tool registry: tracks all MCP-provided tools
    config   — MCP server configuration loading and validation

MCP Tool Naming Convention:
    mcp__{server_name}__{tool_name}
    Example: mcp__github__get_issue, mcp__postgres__query
"""
