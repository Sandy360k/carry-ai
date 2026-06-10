"""
carry-ai/modes/local_mode.py — Local LLM Runner
=================================================

Manages offline LLM inference using llama.cpp with bundled GGUF models.
Auto-selects the best model based on available system RAM.

Model Selection Tiers (by available RAM):
    32GB+ → Qwen3 30B Q6          (top quality, best reasoning)
    24GB+ → Llama 4 Scout 17B Q6  (Meta's latest, multimodal)
    20GB+ → Qwen3 14B Q8          (high quality, tool calling)
    16GB+ → Gemma 4 12B Q4        (Google's latest, multimodal)
    12GB+ → Qwen3 14B Q4          (great reasoning, fits tight)
    10GB+ → Qwen3 8B Q8           (best 8B quality at high quant)
     8GB+ → Qwen3 8B Q4           (solid general purpose)
     6GB+ → Gemma 4 E4B Q4        (multimodal + reasoning)
     5GB+ → Qwen3.5 4B Q4         (edge-optimized)
     4GB+ → Phi-4-mini Q4         (native function calling)
     3GB+ → Gemma 4 E2B Q4        (ultra-light)
    <3GB  → Gemma 3 1B Q4         (emergency fallback)

Architecture:
    1. Scan models/ directory for available .gguf files
    2. Match available models against RAM tiers
    3. Select best available model that fits in RAM
    4. Launch llama.cpp server as subprocess (localhost:8081)
    5. Expose OpenAI-compatible /v1/chat/completions endpoint
    6. Forward agent requests to local server
    7. Kill llama.cpp subprocess on shutdown
"""

import json
import logging
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

try:
    import psutil
except ImportError:
    psutil = None

log = logging.getLogger("carry-ai.modes.local")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ===================================================================
# Model tier definitions
# ===================================================================

@dataclass
class ModelTier:
    """Maps a RAM threshold to a preferred model."""
    min_ram_gb: float
    name: str
    # Substrings to match against .gguf filenames (checked in order)
    match_patterns: list[str]
    context_size: int = 4096
    # Estimated model size in GB (for RAM headroom calculation)
    estimated_size_gb: float = 0.0
    supports_tools: bool = False
    multimodal: bool = False


MODEL_TIERS: list[ModelTier] = [
    # --- 32 GB tier ---
    ModelTier(
        min_ram_gb=32, name="Qwen3 30B Q6_K",
        match_patterns=["qwen3-30b", "qwen3_30b", "qwen-3-30b"],
        context_size=16384, estimated_size_gb=24.0,
        supports_tools=True,
    ),
    # --- 24 GB tier ---
    ModelTier(
        min_ram_gb=24, name="Llama 4 Scout 17B Q6_K",
        match_patterns=["llama-4-scout", "llama4-scout", "llama_4_scout"],
        context_size=16384, estimated_size_gb=14.0,
        supports_tools=True, multimodal=True,
    ),
    # --- 20 GB tier ---
    ModelTier(
        min_ram_gb=20, name="Qwen3 14B Q8_0",
        match_patterns=["qwen3-14b.*q8", "qwen3_14b.*q8", "qwen-3-14b.*q8"],
        context_size=12288, estimated_size_gb=15.0,
        supports_tools=True,
    ),
    # --- 16 GB tier ---
    ModelTier(
        min_ram_gb=16, name="Gemma 4 12B Q4_K_M",
        match_patterns=["gemma-4-12b", "gemma4-12b", "gemma_4_12b"],
        context_size=12288, estimated_size_gb=8.0,
        supports_tools=True, multimodal=True,
    ),
    # --- 12 GB tier ---
    ModelTier(
        min_ram_gb=12, name="Qwen3 14B Q4_K_M",
        match_patterns=["qwen3-14b.*q4", "qwen3_14b.*q4", "qwen-3-14b.*q4"],
        context_size=8192, estimated_size_gb=8.5,
        supports_tools=True,
    ),
    # --- 10 GB tier ---
    ModelTier(
        min_ram_gb=10, name="Qwen3 8B Q8_0",
        match_patterns=["qwen3-8b.*q8", "qwen3_8b.*q8", "qwen-3-8b.*q8"],
        context_size=8192, estimated_size_gb=8.5,
        supports_tools=True,
    ),
    # --- 8 GB tier ---
    ModelTier(
        min_ram_gb=8, name="Qwen3 8B Q4_K_M",
        match_patterns=["qwen3-8b.*q4", "qwen3_8b.*q4", "qwen-3-8b.*q4"],
        context_size=8192, estimated_size_gb=4.5,
        supports_tools=True,
    ),
    # --- 6 GB tier ---
    ModelTier(
        min_ram_gb=6, name="Gemma 4 E4B Q4_K_M",
        match_patterns=["gemma-4-e4b", "gemma4-e4b", "gemma_4_e4b"],
        context_size=8192, estimated_size_gb=3.5,
        multimodal=True,
    ),
    # --- 5 GB tier ---
    ModelTier(
        min_ram_gb=5, name="Qwen3.5 4B Q4_K_M",
        match_patterns=["qwen3.5-4b", "qwen3_5-4b", "qwen3.5_4b"],
        context_size=4096, estimated_size_gb=2.5,
        supports_tools=True,
    ),
    # --- 4 GB tier ---
    ModelTier(
        min_ram_gb=4, name="Phi-4-mini Q4_K_M",
        match_patterns=["phi-4-mini", "phi4-mini", "phi_4_mini"],
        context_size=4096, estimated_size_gb=2.2,
        supports_tools=True,
    ),
    # --- 3 GB tier ---
    ModelTier(
        min_ram_gb=3, name="Gemma 4 E2B Q4_K_M",
        match_patterns=["gemma-4-e2b", "gemma4-e2b", "gemma_4_e2b"],
        context_size=4096, estimated_size_gb=1.5,
    ),
    # --- Emergency fallback ---
    ModelTier(
        min_ram_gb=0, name="Gemma 3 1B Q4_K_M",
        match_patterns=["gemma-3-1b", "gemma3-1b", "gemma_3_1b"],
        context_size=2048, estimated_size_gb=0.8,
    ),
]


# ===================================================================
# Model scanning & selection
# ===================================================================

def scan_available_models(models_dir: Path) -> list[Path]:
    """Find all .gguf files in the models directory.

    Returns list sorted by file size descending (largest = likely best quality).
    """
    if not models_dir.is_dir():
        log.warning("Models directory does not exist: %s", models_dir)
        return []

    models = sorted(
        models_dir.glob("*.gguf"),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )

    for m in models:
        size_gb = m.stat().st_size / (1024 ** 3)
        log.info("  Found model: %s (%.2f GB)", m.name, size_gb)

    return models


def _normalize(name: str) -> str:
    """Normalize a model filename for fuzzy matching."""
    return name.lower().replace("-", "").replace("_", "").replace(" ", "")


def select_model(available_ram_gb: float, available_models: list[Path],
                  use_llmfit: bool = True) -> tuple[ModelTier | None, Path | None]:
    """Pick the best model that fits in available RAM.

    Strategy:
        1. If llmfit is available, use hardware-aware multi-dimensional scoring
           (Quality, Speed, Fit, Context) with GPU detection for optimal selection.
        2. Fall back to RAM-tier matching: walk MODEL_TIERS top-down (best quality
           first), match available models by filename pattern.

    See: https://github.com/AlexsJones/llmfit for the scoring algorithm.

    Returns:
        (matched_tier, model_path) or (None, smallest_model) as fallback,
        or (None, None) if no models at all.
    """
    if not available_models:
        return None, None

    # --- Strategy 1: llmfit hardware-aware selection ---
    if use_llmfit:
        try:
            from integrations.llmfit_advisor import enhanced_select_model, detect_hardware
            hw = detect_hardware()
            result = enhanced_select_model(available_models, hardware=hw)
            if result and result.get("model_path"):
                model_path = result["model_path"]
                # Try to match against tiers for metadata
                matched_tier = _match_path_to_tier(model_path)
                if matched_tier:
                    log.info("llmfit selection: %s → %s (GPU layers=%d, ~%.0f tok/s)",
                             matched_tier.name, model_path.name,
                             result.get("gpu_layers", 0),
                             result.get("estimated_tps", 0))
                else:
                    log.info("llmfit selection: %s (GPU layers=%d, ~%.0f tok/s)",
                             model_path.name,
                             result.get("gpu_layers", 0),
                             result.get("estimated_tps", 0))
                return matched_tier, model_path
        except ImportError:
            log.debug("llmfit integration not available, using RAM-tier fallback")
        except Exception as e:
            log.debug("llmfit selection failed (%s), using RAM-tier fallback", e)

    # --- Strategy 2: Static RAM-tier matching (original) ---
    # Build normalized name → path lookup
    norm_lookup: dict[str, Path] = {}
    for m in available_models:
        norm_lookup[_normalize(m.stem)] = m

    for tier in MODEL_TIERS:
        if available_ram_gb < tier.min_ram_gb:
            continue

        # Check each match pattern against available model filenames
        # Patterns may contain .* for regex-like matching (e.g. "qwen3-8b.*q8")
        import re
        for pattern in tier.match_patterns:
            norm_pattern = _normalize(pattern)
            for norm_name, model_path in norm_lookup.items():
                # Support .* in patterns for quant-aware matching
                if ".*" in norm_pattern:
                    if re.search(norm_pattern, norm_name):
                        match_found = True
                    else:
                        match_found = False
                else:
                    match_found = norm_pattern in norm_name
                if match_found:
                    log.info("Model selection: %s → %s (RAM=%.1fGB, need≥%.0fGB)",
                             tier.name, model_path.name, available_ram_gb, tier.min_ram_gb)
                    return tier, model_path

    # No tier match — fall back to smallest available model
    smallest = min(available_models, key=lambda p: p.stat().st_size)
    log.info("No tier match for %.1f GB RAM. Falling back to: %s", available_ram_gb, smallest.name)
    return None, smallest


def _match_path_to_tier(model_path: Path) -> ModelTier | None:
    """Try to match a model path against known tiers for metadata."""
    import re
    norm_stem = _normalize(model_path.stem)
    for tier in MODEL_TIERS:
        for pattern in tier.match_patterns:
            norm_pattern = _normalize(pattern)
            if ".*" in norm_pattern:
                if re.search(norm_pattern, norm_stem):
                    return tier
            elif norm_pattern in norm_stem:
                return tier
    return None


# ===================================================================
# llama.cpp server binary discovery
# ===================================================================

def find_llama_server() -> Path | None:
    """Locate the llama-server (or llama-server.exe) binary.

    Search order:
        1. PROJECT_ROOT/bin/llama-server[.exe]
        2. PROJECT_ROOT/../bin/llama-server[.exe]  (USB root level)
        3. System PATH (shutil.which)
    """
    is_windows = platform.system().lower() == "windows"
    binary_name = "llama-server.exe" if is_windows else "llama-server"
    alt_names = ["llama-server", "llama_server", "server"]
    if is_windows:
        alt_names = [n + ".exe" for n in alt_names] + alt_names

    search_dirs = [
        PROJECT_ROOT / "bin",
        PROJECT_ROOT.parent / "bin",
        PROJECT_ROOT,
        PROJECT_ROOT.parent,
    ]

    for d in search_dirs:
        for name in [binary_name] + alt_names:
            candidate = d / name
            if candidate.is_file():
                log.info("Found llama-server: %s", candidate)
                return candidate

    # Try system PATH
    which_result = shutil.which("llama-server") or shutil.which("llama_server")
    if which_result:
        log.info("Found llama-server on PATH: %s", which_result)
        return Path(which_result)

    log.warning("llama-server binary not found. Place it in carry-ai/bin/ or add to PATH.")
    return None


# ===================================================================
# llama.cpp server lifecycle
# ===================================================================

@dataclass
class ServerConfig:
    """Configuration for the llama.cpp server process."""
    model_path: Path
    host: str = "127.0.0.1"
    port: int = 8081
    context_size: int = 4096
    n_gpu_layers: int = 0           # 0 = CPU only (portable default)
    threads: int = 0                # 0 = auto-detect
    batch_size: int = 512
    parallel: int = 1               # Concurrent request slots
    flash_attention: bool = False
    extra_args: list[str] = field(default_factory=list)


class LlamaServer:
    """Manages a llama.cpp server subprocess."""

    def __init__(self, binary_path: Path, config: ServerConfig):
        self.binary_path = binary_path
        self.config = config
        self.process: subprocess.Popen | None = None
        self._base_url = f"http://{config.host}:{config.port}"

    def build_command(self) -> list[str]:
        """Build the llama-server command line."""
        cmd = [
            str(self.binary_path),
            "--model", str(self.config.model_path),
            "--host", self.config.host,
            "--port", str(self.config.port),
            "--ctx-size", str(self.config.context_size),
            "--batch-size", str(self.config.batch_size),
            "--parallel", str(self.config.parallel),
        ]

        if self.config.threads > 0:
            cmd.extend(["--threads", str(self.config.threads)])

        if self.config.n_gpu_layers > 0:
            cmd.extend(["--n-gpu-layers", str(self.config.n_gpu_layers)])

        if self.config.flash_attention:
            cmd.append("--flash-attn")

        cmd.extend(self.config.extra_args)
        return cmd

    def start(self) -> None:
        """Launch the llama-server process."""
        cmd = self.build_command()
        log.info("Starting llama-server: %s", " ".join(cmd))

        # Suppress llama.cpp's verbose output unless in debug mode.
        # stdout must not be PIPE: nothing drains it, so the server would
        # block once the OS pipe buffer fills.
        output_target = None if log.isEnabledFor(logging.DEBUG) else subprocess.DEVNULL

        self.process = subprocess.Popen(
            cmd,
            stdout=output_target,
            stderr=output_target,
            # Don't inherit stdin — server doesn't need it
            stdin=subprocess.DEVNULL,
        )
        log.info("llama-server started (PID=%d).", self.process.pid)

    def wait_until_ready(self, timeout: float = 120.0, poll_interval: float = 1.0) -> bool:
        """Poll the /health endpoint until the server is ready.

        Returns True if server became ready within timeout, False otherwise.
        """
        if requests is None:
            log.error("'requests' library not installed. Cannot health-check llama-server.")
            # Blind wait as fallback
            time.sleep(5)
            return True

        deadline = time.monotonic() + timeout
        health_url = f"{self._base_url}/health"

        log.info("Waiting for llama-server to become ready (timeout=%.0fs)...", timeout)

        while time.monotonic() < deadline:
            # Check if process died
            if self.process and self.process.poll() is not None:
                rc = self.process.returncode
                log.error("llama-server exited unexpectedly (code=%d).", rc)
                return False

            try:
                resp = requests.get(health_url, timeout=2)
                if resp.status_code == 200:
                    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                    status = body.get("status", "ok")
                    if status in ("ok", "no slot available"):
                        log.info("llama-server is ready (status=%s).", status)
                        return True
                    # "loading model" — keep waiting
                    log.debug("llama-server status: %s", status)
            except requests.ConnectionError:
                pass
            except requests.Timeout:
                pass
            except Exception as e:
                log.debug("Health check error: %s", e)

            time.sleep(poll_interval)

        log.error("llama-server did not become ready within %.0fs.", timeout)
        return False

    def is_running(self) -> bool:
        """Check if the server process is still running."""
        if self.process is None:
            return False
        return self.process.poll() is None

    def shutdown(self, timeout: float = 10.0) -> None:
        """Gracefully stop the llama-server process."""
        if self.process is None or not self.is_running():
            log.debug("llama-server is not running.")
            return

        pid = self.process.pid
        log.info("Stopping llama-server (PID=%d)...", pid)

        # Try graceful termination first
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
            log.info("llama-server terminated gracefully.")
        except subprocess.TimeoutExpired:
            log.warning("llama-server did not stop in %.0fs. Killing.", timeout)
            self.process.kill()
            self.process.wait(timeout=5)
            log.info("llama-server killed.")

        self.process = None


# ===================================================================
# Chat completion (OpenAI-compatible)
# ===================================================================

def _build_chat_payload(messages: list[dict], **kwargs) -> dict:
    """Build a /v1/chat/completions request body."""
    payload = {
        "messages": messages,
        "stream": kwargs.get("stream", False),
        "temperature": kwargs.get("temperature", 0.7),
        "max_tokens": kwargs.get("max_tokens", 2048),
    }

    # Optional fields
    if "top_p" in kwargs:
        payload["top_p"] = kwargs["top_p"]
    if "stop" in kwargs:
        payload["stop"] = kwargs["stop"]
    if "tools" in kwargs:
        payload["tools"] = kwargs["tools"]
    if "tool_choice" in kwargs:
        payload["tool_choice"] = kwargs["tool_choice"]
    if "response_format" in kwargs:
        payload["response_format"] = kwargs["response_format"]

    return payload


# ===================================================================
# LocalRunner — main public interface
# ===================================================================

class LocalRunner:
    """Manages llama.cpp server lifecycle and provides chat completion API.

    Usage:
        runner = LocalRunner(models_dir=Path("models"), ram_gb=6.5)
        runner.start()          # Selects model, starts server, waits for ready
        response = runner.chat([{"role": "user", "content": "Hello"}])
        runner.shutdown()
    """

    def __init__(self, models_dir: Path | None = None, ram_gb: float | None = None,
                 model_path: Path | None = None, port: int = 8081,
                 context_size: int | None = None):
        """
        Args:
            models_dir: Directory to scan for .gguf files. Default: PROJECT_ROOT/models
            ram_gb: Available RAM in GB. Auto-detected if None.
            model_path: Force a specific model file (skips auto-selection).
            port: Port for llama-server. Default: 8081.
            context_size: Override context window size. Auto from tier if None.
        """
        self.models_dir = models_dir or (PROJECT_ROOT / "models")
        self.port = port
        self._context_size_override = context_size

        # Detect RAM if not provided
        if ram_gb is not None:
            self.ram_gb = ram_gb
        elif psutil is not None:
            self.ram_gb = psutil.virtual_memory().available / (1024 ** 3)
        else:
            self.ram_gb = 4.0

        # Model selection
        self.forced_model_path = model_path
        self.selected_tier: ModelTier | None = None
        self.selected_model: Path | None = None

        # Server
        self._server: LlamaServer | None = None
        self._base_url = f"http://127.0.0.1:{self.port}"

    @property
    def model_name(self) -> str:
        """Human-readable name of the selected model."""
        if self.selected_tier:
            return self.selected_tier.name
        if self.selected_model:
            return self.selected_model.stem
        return "none"

    @property
    def supports_tools(self) -> bool:
        """Whether the selected model supports function calling."""
        return self.selected_tier.supports_tools if self.selected_tier else False

    @property
    def is_multimodal(self) -> bool:
        """Whether the selected model supports image input."""
        return self.selected_tier.multimodal if self.selected_tier else False

    def select(self) -> Path:
        """Run model selection. Returns the chosen model path.

        Raises FileNotFoundError if no suitable model is found.
        """
        if self.forced_model_path:
            if not self.forced_model_path.is_file():
                raise FileNotFoundError(f"Forced model not found: {self.forced_model_path}")
            self.selected_model = self.forced_model_path
            # Try to match against tiers for metadata
            for tier in MODEL_TIERS:
                norm_stem = _normalize(self.forced_model_path.stem)
                if any(_normalize(p) in norm_stem for p in tier.match_patterns):
                    self.selected_tier = tier
                    break
            log.info("Using forced model: %s", self.selected_model.name)
            return self.selected_model

        available = scan_available_models(self.models_dir)
        if not available:
            raise FileNotFoundError(
                f"No .gguf models found in {self.models_dir}. "
                "Use --download-model to get one from HuggingFace."
            )

        self.selected_tier, self.selected_model = select_model(self.ram_gb, available)
        if self.selected_model is None:
            raise FileNotFoundError("Model selection returned no model.")

        return self.selected_model

    def start(self, timeout: float = 120.0) -> None:
        """Select model, start llama-server, and wait until ready.

        Raises:
            FileNotFoundError: If no model or llama-server binary found.
            RuntimeError: If server fails to start or become ready.
        """
        # 1. Select model
        model_path = self.select()

        # 2. Find binary
        binary = find_llama_server()
        if binary is None:
            raise FileNotFoundError(
                "llama-server binary not found. Place it in carry-ai/bin/ "
                "or install llama.cpp and add to PATH."
            )

        # 3. Build config
        ctx = self._context_size_override
        if ctx is None:
            ctx = self.selected_tier.context_size if self.selected_tier else 4096

        # Auto-detect thread count (leave some for OS + agent)
        threads = 0
        if psutil is not None:
            cpu_count = psutil.cpu_count(logical=True)
            if cpu_count and cpu_count > 2:
                threads = max(1, cpu_count - 2)

        config = ServerConfig(
            model_path=model_path,
            port=self.port,
            context_size=ctx,
            threads=threads,
        )

        # 4. Start server
        self._server = LlamaServer(binary, config)
        self._server.start()

        # 5. Wait for ready
        if not self._server.wait_until_ready(timeout=timeout):
            self._server.shutdown()
            raise RuntimeError("llama-server failed to become ready.")

        log.info("Local runner ready: %s on port %d (ctx=%d)", self.model_name, self.port, ctx)

    def is_running(self) -> bool:
        """Check if the llama-server is running."""
        return self._server is not None and self._server.is_running()

    def chat(self, messages: list[dict], **kwargs) -> dict:
        """Send a chat completion request to the local llama-server.

        Args:
            messages: OpenAI-format message list.
            **kwargs: temperature, max_tokens, top_p, stop, tools, etc.

        Returns:
            Normalized response dict:
            {
                "role": "assistant",
                "content": "...",
                "tool_calls": [...] | None,
                "provider": "local",
                "model": "model-name",
                "usage": {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
            }
        """
        if requests is None:
            raise ImportError("'requests' library required for local mode. pip install requests")

        if not self.is_running():
            raise RuntimeError("llama-server is not running. Call start() first.")

        url = f"{self._base_url}/v1/chat/completions"
        payload = _build_chat_payload(messages, **kwargs)

        log.debug("POST %s — %d messages", url, len(messages))

        resp = requests.post(url, json=payload, timeout=kwargs.get("timeout", 300))
        resp.raise_for_status()
        data = resp.json()

        # Normalize response
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        usage = data.get("usage", {})

        return {
            "role": message.get("role", "assistant"),
            "content": message.get("content", ""),
            "tool_calls": message.get("tool_calls"),
            "provider": "local",
            "model": self.model_name,
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

    def stream(self, messages: list[dict], **kwargs):
        """Stream a chat completion response from the local llama-server.

        Yields content chunks as strings. Final chunk may contain tool_calls.

        Args:
            messages: OpenAI-format message list.
            **kwargs: temperature, max_tokens, etc.

        Yields:
            dict with keys: {"type": "content"|"tool_call"|"done", "data": ...}
        """
        if requests is None:
            raise ImportError("'requests' library required. pip install requests")

        if not self.is_running():
            raise RuntimeError("llama-server is not running. Call start() first.")

        url = f"{self._base_url}/v1/chat/completions"
        payload = _build_chat_payload(messages, stream=True, **kwargs)

        log.debug("POST %s (stream) — %d messages", url, len(messages))

        with requests.post(url, json=payload, stream=True, timeout=kwargs.get("timeout", 300)) as resp:
            resp.raise_for_status()

            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue

                # SSE format: "data: {...}" or "data: [DONE]"
                if not line.startswith("data: "):
                    continue

                data_str = line[6:]  # Strip "data: "

                if data_str.strip() == "[DONE]":
                    yield {"type": "done", "data": None}
                    return

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    log.debug("Skipping unparseable SSE chunk: %s", data_str[:100])
                    continue

                choice = data.get("choices", [{}])[0]
                delta = choice.get("delta", {})

                content = delta.get("content")
                if content:
                    yield {"type": "content", "data": content}

                tool_calls = delta.get("tool_calls")
                if tool_calls:
                    yield {"type": "tool_call", "data": tool_calls}

                # Check for finish_reason
                finish = choice.get("finish_reason")
                if finish:
                    yield {"type": "done", "data": finish}
                    return

    def shutdown(self) -> None:
        """Stop the llama-server and release resources."""
        if self._server:
            self._server.shutdown()
            self._server = None
        log.info("Local runner shut down.")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
        return False

    def get_status(self) -> dict:
        """Return current status info for the UI."""
        return {
            "running": self.is_running(),
            "model": self.model_name,
            "model_path": str(self.selected_model) if self.selected_model else None,
            "tier": self.selected_tier.name if self.selected_tier else None,
            "port": self.port,
            "supports_tools": self.supports_tools,
            "multimodal": self.is_multimodal,
            "ram_gb": round(self.ram_gb, 1),
        }


# ===================================================================
# LocalAI integration
# ===================================================================
# LocalAI (https://github.com/mudler/LocalAI) is a drop-in alternative
# to running llama.cpp directly.  It provides:
#   - OpenAI-compatible REST API (/v1/chat/completions, /v1/models, ...)
#   - Multi-backend support (llama.cpp, whisper, bark, stable-diffusion)
#   - GGUF model support out of the box
#   - Optional GPU acceleration (CUDA, ROCm, Metal, Vulkan)
#   - Single self-contained binary (no Python / Docker needed)
#
# Usage:
#   runner = LocalAIRunner(models_dir=Path("models"), port=8081)
#   runner.start()      # generates YAML configs, starts binary, waits
#   runner.shutdown()

LOCALAI_VERSION = "v2.25.0"

LOCALAI_BINARIES: dict[str, str] = {
    "linux_x86_64": (
        f"https://github.com/mudler/LocalAI/releases/download/"
        f"{LOCALAI_VERSION}/local-ai-Linux-x86_64"
    ),
    "linux_arm64": (
        f"https://github.com/mudler/LocalAI/releases/download/"
        f"{LOCALAI_VERSION}/local-ai-Linux-arm64"
    ),
    "windows_x86_64": (
        f"https://github.com/mudler/LocalAI/releases/download/"
        f"{LOCALAI_VERSION}/local-ai-Windows-x86_64.exe"
    ),
    "darwin_arm64": (
        f"https://github.com/mudler/LocalAI/releases/download/"
        f"{LOCALAI_VERSION}/local-ai-Darwin-arm64"
    ),
}

# Minimal LocalAI YAML template for a GGUF model.
# Supports chat completions + tool calling for modern models.
_LOCALAI_YAML_TEMPLATE = """\
# Auto-generated by carry-ai — do not edit manually.
# Re-generated on each boot from the registry.
name: {model_name}
backend: llama
parameters:
  model: {gguf_filename}
  context_size: {context_size}
  threads: {threads}
  f16: true
template:
  chat: chatML
  chat_message: |
    {{{{if eq .RoleName "system"}}}}
    <|im_start|>system
    {{{{.Content}}}}<|im_end|>
    {{{{end}}}}
    {{{{if eq .RoleName "user"}}}}
    <|im_start|>user
    {{{{.Content}}}}<|im_end|>
    <|im_start|>assistant
    {{{{end}}}}
    {{{{if eq .RoleName "assistant"}}}}
    {{{{.Content}}}}<|im_end|>
    {{{{end}}}}
"""


def find_localai_binary() -> Path | None:
    """Locate the LocalAI binary.

    Search order:
        1. PROJECT_ROOT/bin/local-ai[.exe]
        2. PROJECT_ROOT/../bin/local-ai[.exe]  (USB root level)
        3. System PATH
    """
    is_windows = platform.system().lower() == "windows"
    names = ["local-ai.exe", "local-ai"] if is_windows else ["local-ai", "localai"]

    search_dirs = [
        PROJECT_ROOT / "bin",
        PROJECT_ROOT.parent / "bin",
        PROJECT_ROOT,
        PROJECT_ROOT.parent,
    ]
    for d in search_dirs:
        for name in names:
            candidate = d / name
            if candidate.is_file():
                log.info("Found LocalAI binary: %s", candidate)
                return candidate

    which_result = shutil.which("local-ai") or shutil.which("localai")
    if which_result:
        log.info("Found LocalAI on PATH: %s", which_result)
        return Path(which_result)

    return None


def generate_localai_config(gguf_path: Path, models_dir: Path,
                             context_size: int = 4096,
                             threads: int = 0) -> Path:
    """
    Create a LocalAI YAML model config alongside a GGUF file.

    LocalAI requires a YAML config per model to expose it on the API.
    This generates a minimal config that works for all ChatML-templated
    models (Qwen, Phi, Gemma, Llama, etc.).

    Args:
        gguf_path:    Path to the .gguf file.
        models_dir:   Directory where LocalAI looks for model configs.
        context_size: Context window size (tokens).
        threads:      CPU thread count (0 = auto).

    Returns:
        Path to the written YAML file.
    """
    # Model name: strip .gguf and sanitize (LocalAI uses this as the API model id)
    model_name = gguf_path.stem.lower()
    # Replace problematic chars with hyphens
    import re as _re
    model_name = _re.sub(r"[^a-z0-9.\-]", "-", model_name)
    model_name = _re.sub(r"-{2,}", "-", model_name).strip("-")

    auto_threads = threads
    if auto_threads == 0 and psutil is not None:
        cpu_count = psutil.cpu_count(logical=True) or 4
        auto_threads = max(1, cpu_count - 2)
    if auto_threads == 0:
        auto_threads = 4

    yaml_content = _LOCALAI_YAML_TEMPLATE.format(
        model_name=model_name,
        gguf_filename=gguf_path.name,
        context_size=context_size,
        threads=auto_threads,
    )

    yaml_path = models_dir / f"{gguf_path.stem}.yaml"
    yaml_path.write_text(yaml_content, encoding="utf-8")
    log.info("Generated LocalAI config: %s (model_name=%s)", yaml_path.name, model_name)
    return yaml_path


def generate_all_localai_configs(models_dir: Path) -> list[str]:
    """
    Generate YAML configs for every .gguf file in models_dir that
    does not already have an up-to-date config.

    Returns list of model names registered with LocalAI.
    """
    if not models_dir.is_dir():
        return []

    model_names: list[str] = []
    for gguf in sorted(models_dir.glob("*.gguf")):
        yaml_path = models_dir / f"{gguf.stem}.yaml"
        # Regenerate if yaml is missing or older than the gguf
        if (not yaml_path.exists() or
                yaml_path.stat().st_mtime < gguf.stat().st_mtime):
            try:
                yaml = generate_localai_config(gguf, models_dir)
                # Extract model name from YAML
                for line in yaml.read_text().splitlines():
                    if line.startswith("name:"):
                        model_names.append(line.split(":", 1)[1].strip())
                        break
            except Exception as e:
                log.warning("Failed to generate config for %s: %s", gguf.name, e)
        else:
            # Read existing model name
            try:
                for line in yaml_path.read_text().splitlines():
                    if line.startswith("name:"):
                        model_names.append(line.split(":", 1)[1].strip())
                        break
            except Exception:
                pass

    log.info("LocalAI: %d model config(s) ready", len(model_names))
    return model_names


class LocalAIRunner:
    """
    Manages a LocalAI server subprocess for GGUF model inference.

    Advantages over LocalRunner (raw llama.cpp):
      - Multi-model support: all models in models/ are loaded simultaneously
      - Whisper STT + TTS endpoints on the same server
      - Better concurrency (multiple parallel slots)
      - Auto-updates model list without restart
      - OpenAI /v1/models endpoint for model discovery

    Usage:
        runner = LocalAIRunner(models_dir=Path("models"), port=8081)
        runner.start()
        response = runner.provider.chat([{"role": "user", "content": "Hi"}])
        runner.shutdown()
    """

    def __init__(self, models_dir: Path | None = None, port: int = 8081,
                 host: str = "127.0.0.1", threads: int = 0,
                 context_size: int = 4096):
        """
        Args:
            models_dir:   Directory containing .gguf files and YAML configs.
                          Defaults to PROJECT_ROOT/models.
            port:         Port LocalAI listens on (default 8081).
            host:         Bind address (default 127.0.0.1 = localhost only).
            threads:      CPU threads (0 = auto-detect).
            context_size: Default context window for generated configs.
        """
        self.models_dir = models_dir or (PROJECT_ROOT / "models")
        self.port = port
        self.host = host
        self.threads = threads
        self.context_size = context_size

        self._binary: Path | None = None
        self._process: subprocess.Popen | None = None
        self._model_names: list[str] = []

        # Lazy-created provider
        self._provider: object | None = None  # LocalAIProvider

    @property
    def provider(self):
        """Return a LocalAIProvider configured for this runner's host:port."""
        if self._provider is None:
            try:
                from providers.localai_provider import LocalAIProvider
                self._provider = LocalAIProvider(host=self.host, port=self.port)
            except ImportError as e:
                log.error("Cannot import LocalAIProvider: %s", e)
        return self._provider

    def build_command(self) -> list[str]:
        """Build the LocalAI server command line."""
        cmd = [
            str(self._binary),
            "--models-path", str(self.models_dir),
            "--address", f"{self.host}:{self.port}",
            "--context-size", str(self.context_size),
            "--single-active-backend",   # save RAM — only one backend loaded
        ]
        if self.threads > 0:
            cmd.extend(["--threads", str(self.threads)])
        return cmd

    def is_running(self) -> bool:
        if self._process is None:
            return False
        return self._process.poll() is None

    def start(self, timeout: float = 120.0) -> None:
        """
        Prepare configs, locate binary, start LocalAI, wait until ready.

        Steps:
            1. Generate YAML configs for all .gguf files in models_dir
            2. Find the LocalAI binary
            3. Start it as a subprocess
            4. Poll /readyz until ready or timeout
        """
        # 1. Generate model configs
        log.info("Generating LocalAI model configs in %s ...", self.models_dir)
        self._model_names = generate_all_localai_configs(self.models_dir)
        if not self._model_names:
            raise FileNotFoundError(
                f"No .gguf models found in {self.models_dir}. "
                "Download a model first (flash_usb.py --add-models or the in-app Model Manager)."
            )

        # 2. Find binary
        self._binary = find_localai_binary()
        if self._binary is None:
            raise FileNotFoundError(
                "LocalAI binary not found. Download it with:\n"
                "  python flash_usb.py --add-localai --target <usb_path>\n"
                "Or place 'local-ai' in carry-ai/bin/"
            )

        # Ensure binary is executable on Linux/macOS
        if platform.system().lower() != "windows":
            try:
                import os as _os
                _os.chmod(self._binary, 0o755)
            except OSError:
                pass

        # 3. Start process
        cmd = self.build_command()
        log.info("Starting LocalAI: %s", " ".join(cmd))
        stderr_target = None if log.isEnabledFor(logging.DEBUG) else subprocess.DEVNULL
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=stderr_target,
            stdin=subprocess.DEVNULL,
        )
        log.info("LocalAI started (PID=%d). Models: %s", self._process.pid,
                 ", ".join(self._model_names))

        # 4. Wait for ready
        prov = self.provider
        if prov is None:
            time.sleep(5)
            return

        log.info("Waiting for LocalAI to become ready (timeout=%.0fs)...", timeout)
        if not prov.wait_until_ready(timeout=timeout):
            self.shutdown()
            raise RuntimeError(
                f"LocalAI did not become ready within {timeout:.0f}s. "
                "Check that the binary and models are correct."
            )

        log.info("LocalAI is ready on port %d. Models: %s",
                 self.port, ", ".join(self._model_names))

    def shutdown(self, timeout: float = 10.0) -> None:
        """Stop the LocalAI process."""
        if self._process is None or not self.is_running():
            return
        pid = self._process.pid
        log.info("Stopping LocalAI (PID=%d)...", pid)
        self._process.terminate()
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("LocalAI did not stop — killing.")
            self._process.kill()
            self._process.wait(timeout=5)
        self._process = None
        log.info("LocalAI stopped.")

    def get_status(self) -> dict:
        return {
            "running": self.is_running(),
            "backend": "localai",
            "models": self._model_names,
            "port": self.port,
            "binary": str(self._binary) if self._binary else None,
        }

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
        return False
