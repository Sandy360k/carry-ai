"""
carry-ai/integrations/scrapling_tools.py -- Adaptive Web Scraping
==================================================================

Integrates Scrapling (https://github.com/D4Vinci/Scrapling) as
agent tools for intelligent web scraping with anti-bot bypass.

Features (from Scrapling):
    - Adaptive element tracking: auto-relocates elements after site changes
    - Anti-bot bypass: Cloudflare Turnstile, TLS fingerprint impersonation
    - Three fetcher tiers:
      * Fetcher: Fast HTTP with TLS impersonation (lightest)
      * StealthyFetcher: Headless browser + fingerprint spoofing + CF bypass
      * DynamicFetcher: Full Playwright/Chromium for JS-heavy sites
    - Rich selectors: CSS (with Scrapy pseudo-elements), XPath, regex
    - Session support: persistent cookies across requests
    - Spider framework: concurrent crawling with checkpoint pause/resume

Tools registered:
    - web_fetch (upgraded): Uses Scrapling Fetcher with stealthy headers
    - scrape: Extract structured data via CSS/XPath selectors
    - scrape_stealth: Stealth mode for anti-bot protected sites

Fallback:
    If Scrapling is not installed, falls back to requests.get() gracefully.

Reference: https://github.com/D4Vinci/Scrapling
"""

import json
import logging

log = logging.getLogger("carry-ai.integrations.scrapling")

# Check Scrapling availability at import time
_SCRAPLING_AVAILABLE = False
_SCRAPLING_STEALTH = False

try:
    from scrapling.fetchers import Fetcher
    _SCRAPLING_AVAILABLE = True
    try:
        from scrapling.fetchers import StealthyFetcher
        _SCRAPLING_STEALTH = True
    except ImportError:
        pass
    log.info("Scrapling available (stealth=%s)", _SCRAPLING_STEALTH)
except ImportError:
    log.debug("Scrapling not installed -- web scraping will use requests fallback")


def _fetch_with_scrapling(url: str, stealth: bool = False, timeout: int = 30) -> str:
    """Fetch a URL using Scrapling with optional stealth mode.

    Args:
        url: URL to fetch.
        stealth: Use StealthyFetcher for anti-bot bypass.
        timeout: Request timeout in seconds.

    Returns:
        Page text content.
    """
    if stealth and _SCRAPLING_STEALTH:
        from scrapling.fetchers import StealthyFetcher
        page = StealthyFetcher.fetch(url, headless=True, solve_cloudflare=True)
    elif _SCRAPLING_AVAILABLE:
        from scrapling.fetchers import Fetcher
        page = Fetcher.get(url, stealthy_headers=True, timeout=timeout)
    else:
        # Fallback to requests
        import requests
        resp = requests.get(url, timeout=timeout,
                           headers={"User-Agent": "carry-ai/1.0 (Scrapling fallback)"})
        resp.raise_for_status()
        return resp.text

    # Extract clean text from Scrapling page object
    if hasattr(page, "get_all_text"):
        return page.get_all_text()
    elif hasattr(page, "text"):
        return page.text
    return str(page)


def _tool_web_fetch_enhanced(url: str, max_length: int = 50000,
                              stealth: bool = False) -> str:
    """Fetch a URL using Scrapling with adaptive anti-bot bypass.

    Enhanced version of web_fetch that uses Scrapling for better
    success rates on protected sites. Falls back to basic requests
    if Scrapling is not installed.

    Args:
        url: URL to fetch.
        max_length: Maximum content length to return.
        stealth: Use stealth mode for anti-bot protected sites.
    """
    try:
        text = _fetch_with_scrapling(url, stealth=stealth)

        # Try to extract meaningful text from HTML
        if _SCRAPLING_AVAILABLE and "<html" in text.lower()[:200]:
            try:
                from scrapling import Adaptor
                page = Adaptor(text)
                # Remove script and style tags
                for elem in page.css("script, style, nav, footer, header"):
                    try:
                        elem.remove()
                    except Exception:
                        pass
                text = page.get_all_text(separator="\n") if hasattr(page, "get_all_text") else text
            except Exception:
                pass  # Use raw text

        if len(text) > max_length:
            text = text[:max_length] + f"\n\n[Truncated at {max_length} chars]"

        return text
    except Exception as e:
        return f"Error fetching {url}: {e}"


def _tool_scrape(url: str, selector: str, selector_type: str = "css",
                 stealth: bool = False, max_results: int = 50) -> str:
    """Scrape a URL and extract elements using CSS or XPath selectors.

    Uses Scrapling's adaptive element tracking for robust extraction
    that survives site layout changes.

    Args:
        url: URL to scrape.
        selector: CSS or XPath selector expression.
            CSS examples: ".title::text", "a::attr(href)", "#main .item"
            XPath examples: "//h1/text()", "//a/@href"
        selector_type: "css" or "xpath" (default: "css").
        stealth: Use stealth mode for anti-bot protected sites.
        max_results: Maximum number of results to return.
    """
    try:
        if not _SCRAPLING_AVAILABLE:
            return ("Error: Scrapling not installed. Install with:\n"
                    "  pip install scrapling\n"
                    "  scrapling install  # for stealth/dynamic fetchers")

        # Fetch the page
        if stealth and _SCRAPLING_STEALTH:
            from scrapling.fetchers import StealthyFetcher
            page = StealthyFetcher.fetch(url, headless=True, solve_cloudflare=True)
        else:
            from scrapling.fetchers import Fetcher
            page = Fetcher.get(url, stealthy_headers=True)

        # Extract with selector
        if selector_type == "xpath":
            results = page.xpath(selector)
        else:
            results = page.css(selector)

        # Convert to text list
        items = []
        for r in results:
            if hasattr(r, "text"):
                items.append(r.text.strip())
            else:
                items.append(str(r).strip())
            if len(items) >= max_results:
                break

        if not items:
            return f"No matches for selector '{selector}' on {url}"

        output = json.dumps(items, indent=2, ensure_ascii=False)
        return f"Found {len(items)} result(s):\n{output}"

    except Exception as e:
        return f"Error scraping {url}: {e}"


def _tool_scrape_stealth(url: str, selector: str = "",
                          selector_type: str = "css",
                          solve_cloudflare: bool = True) -> str:
    """Scrape an anti-bot protected site using headless browser.

    Uses Scrapling's StealthyFetcher with Cloudflare bypass,
    TLS fingerprint impersonation, and advanced spoofing.

    Requires: pip install 'scrapling[fetchers]' && scrapling install

    Args:
        url: URL to scrape (works with Cloudflare-protected sites).
        selector: Optional CSS/XPath selector to extract specific elements.
        selector_type: "css" or "xpath".
        solve_cloudflare: Attempt to bypass Cloudflare Turnstile.
    """
    if not _SCRAPLING_STEALTH:
        return ("Error: StealthyFetcher not available. Install with:\n"
                "  pip install 'scrapling[fetchers]'\n"
                "  scrapling install")

    try:
        from scrapling.fetchers import StealthyFetcher
        page = StealthyFetcher.fetch(
            url,
            headless=True,
            solve_cloudflare=solve_cloudflare,
        )

        if selector:
            if selector_type == "xpath":
                results = page.xpath(selector)
            else:
                results = page.css(selector)

            items = []
            for r in results:
                text = r.text.strip() if hasattr(r, "text") else str(r).strip()
                if text:
                    items.append(text)

            if not items:
                return f"No matches for '{selector}'. Page title: {page.css('title::text').get('')}"

            return json.dumps(items[:50], indent=2, ensure_ascii=False)

        # No selector -- return full page text
        text = page.get_all_text() if hasattr(page, "get_all_text") else page.text
        if len(text) > 50000:
            text = text[:50000] + "\n\n[Truncated]"
        return text

    except Exception as e:
        return f"Error (stealth scrape) {url}: {e}"


def register_scrapling_tools():
    """Register Scrapling-based tools in the agent tool registry.

    Upgrades the existing web_fetch tool and adds scrape/scrape_stealth.
    """
    from agent.tools import register_tool, TOOL_REGISTRY

    # Upgrade web_fetch with Scrapling backend
    register_tool(
        name="web_fetch",
        description=(
            "Fetch a URL and return text content. Uses Scrapling for adaptive "
            "anti-bot bypass when available. Set stealth=true for Cloudflare-"
            "protected sites. Falls back to basic HTTP if Scrapling not installed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "max_length": {"type": "integer", "description": "Max response length", "default": 50000},
                "stealth": {"type": "boolean", "description": "Use stealth mode for anti-bot bypass", "default": False},
            },
            "required": ["url"],
        },
        execute_fn=_tool_web_fetch_enhanced,
    )

    # Add scrape tool
    register_tool(
        name="scrape",
        description=(
            "Scrape a URL and extract structured data using CSS or XPath selectors. "
            "CSS examples: '.title::text', 'a::attr(href)', '#main .item'. "
            "XPath examples: '//h1/text()', '//a/@href'. "
            "Use stealth=true for anti-bot protected sites."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to scrape"},
                "selector": {"type": "string", "description": "CSS or XPath selector"},
                "selector_type": {"type": "string", "enum": ["css", "xpath"], "default": "css"},
                "stealth": {"type": "boolean", "description": "Use stealth mode", "default": False},
                "max_results": {"type": "integer", "description": "Max elements to return", "default": 50},
            },
            "required": ["url", "selector"],
        },
        execute_fn=_tool_scrape,
    )

    # Add stealth scrape tool
    register_tool(
        name="scrape_stealth",
        description=(
            "Scrape an anti-bot protected site using headless browser with "
            "Cloudflare bypass, TLS fingerprint impersonation, and advanced "
            "spoofing. Requires: pip install 'scrapling[fetchers]' && scrapling install. "
            "Optionally extract elements with CSS/XPath selector."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to scrape (handles Cloudflare, etc.)"},
                "selector": {"type": "string", "description": "Optional CSS/XPath selector", "default": ""},
                "selector_type": {"type": "string", "enum": ["css", "xpath"], "default": "css"},
                "solve_cloudflare": {"type": "boolean", "description": "Attempt Cloudflare bypass", "default": True},
            },
            "required": ["url"],
        },
        execute_fn=_tool_scrape_stealth,
    )

    log.info("Registered Scrapling tools (scrapling=%s, stealth=%s)",
             _SCRAPLING_AVAILABLE, _SCRAPLING_STEALTH)
