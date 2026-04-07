"""
carry-ai/modes/ — Execution Mode Package
=========================================

Contains the two primary execution strategies:
    - local_mode: Runs LLM inference locally via llama.cpp + GGUF models
    - api_mode:   Routes requests to cloud LLM providers via encrypted API keys

The launcher selects one (or both in hybrid mode) based on available RAM,
network connectivity, and user preference.
"""
