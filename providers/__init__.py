"""
carry-ai/providers/ — LLM Provider Package
============================================

Each provider module implements the BaseProvider interface (from base.py)
to normalize chat completion requests across different cloud LLM APIs.

Providers:
    anthropic_provider  — Anthropic Claude (Messages API)
    google_oauth        — Google Gemini (OAuth2 + generateContent API)
    openai_provider     — OpenAI GPT (Chat Completions API)
    groq_provider       — Groq (OpenAI-compatible, free tier)
    openrouter_provider — OpenRouter (unified gateway, OpenAI-compatible)
"""
