"""
carry-ai/agent/ — AI Agent Package
====================================

The agent layer sits between the LLM (local or API) and the host system.
It receives natural language instructions, breaks them into tool calls,
executes them, and returns results.

Submodules:
    agent — Main agent loop (message → LLM → tool call → result → repeat)
    tools — Tool definitions (shell, file, browser, screenshot, scraping)

Inspired by Open Interpreter's architecture and claw-code's tool system.
"""
