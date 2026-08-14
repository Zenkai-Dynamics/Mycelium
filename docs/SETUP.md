# Mycelium — Developer Setup

Related: [Phase 0 — Foundation](phases/phase-0-foundation.md) · [Dependency & hardware compatibility](dependencies.md)

This walks through getting from a clean checkout to a working Mycelium
development environment. It's split into two parts:

1. **Base setup** — works on any OS, no GPU required. This is what you
   need to develop against the coordinator/client code or run the CLI
   stubs.
2. **Node / GPU setup** — only needed if you're setting up a real Phase 0
   GPU node. Linux x86_64 only.

## Prerequisites

- **git**
- **Python 3.11 or newer**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** (recommended). If you don't have it:

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

  Don't have `uv` and don't want it? Skip to the plain-`pip` fallback in
  step 2 of Base setup below — everything else in this guide still
  applies.

## Base setup

Works on macOS, Linux, or Windows — no GPU needed.

1. Clone the repo and enter it:

   ```bash
   git clone https://github.com/Zenkai-Dynamics/Mycelium.git
   cd Mycelium
   ```

2. Create a virtual environment and install the package in editable mode:

   ```bash
   uv venv
   uv pip install -e .
   ```

   Use `uv venv` + `uv pip install -e .`, not `uv sync` or `uv run` —
   `pyproject.toml` locks dependency *resolution* to Linux x86_64 (for the
   GPU node stack below), so `uv sync`/`uv run` fail outright on any other
   platform, even with no extras requested. `uv pip install -e .` resolves
   directly against `pyproject.toml` instead of the Linux-only lockfile, so
   it works everywhere.

   No `uv`? Plain `pip` works too:

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -e .        # Windows: .venv\Scripts\pip install -e .
   ```

3. Verify the install by running all three CLI stubs:

   ```bash
   .venv/bin/mycelium-node        # Windows: .venv\Scripts\mycelium-node
   .venv/bin/mycelium-coordinator # Windows: .venv\Scripts\mycelium-coordinator
   .venv/bin/mycelium-client      # Windows: .venv\Scripts\mycelium-client
   ```

   Expected output:

   ```
   mycelium-node 0.1.0
   mycelium-coordinator 0.1.0
   mycelium-client 0.1.0
   ```

   If you see those three lines, your environment is set up correctly.
   These are Phase 0 skeleton stubs (see
   [phase-0-foundation.md](phases/phase-0-foundation.md)) — they don't do
   anything functional yet beyond confirming they run.

## Node / GPU setup

Only needed if you're setting up a real Phase 0 GPU node — not required
for developing against the coordinator or client.

The node agent wraps Ray + vLLM (the `node` optional-dependency extra in
`pyproject.toml`), which only resolves on **Linux x86_64** with a
CUDA-capable GPU. See [dependencies.md](dependencies.md) for:

- the exact pinned `ray`/`vllm` versions and why
- the install commands (`uv sync --extra node`, or the
  `requirements-node-lock.txt` plain-pip fallback)
- minimum CUDA/driver version and required GPU compute capability
