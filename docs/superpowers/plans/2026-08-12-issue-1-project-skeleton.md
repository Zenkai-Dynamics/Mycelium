# Issue #1 — Project Skeleton & Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an installable `mycelium` Python package with stub entry points for the node agent, coordinator, and client — nothing functional, just the shape every later Phase 0 ticket builds into.

**Architecture:** One `src`-layout package, `mycelium`, with three subpackages (`mycelium.node`, `mycelium.coordinator`, `mycelium.client`), each holding a `cli.py:main()` stub wired to a `[project.scripts]` console entry point. `setuptools` is the build backend so `pip install -e .` works with no extra tooling.

**Tech Stack:** Python 3.10+, setuptools (build backend), stdlib only (no runtime dependencies).

**Spec:** [docs/superpowers/specs/2026-08-12-issue-1-project-skeleton-design.md](../specs/2026-08-12-issue-1-project-skeleton-design.md)

## Global Constraints

- Python requirement: `>=3.10`
- Build backend: `setuptools` (not `uv_build`, not Poetry/hatchling) — must install via bare `pip install -e .` with no other tools required
- Layout: `src/mycelium/...` (src-layout), not flat
- License: MIT. New `LICENSE` file, copyright holder "Varun Gambhir"; `pyproject.toml` `license = {file = "LICENSE"}` plus an MIT classifier
- Subpackage names: `mycelium.node`, `mycelium.coordinator`, `mycelium.client` — no `_agent` suffix
- CLI entry points: stdlib only, no click/typer/argparse framework — each `main()` just prints its own name and version
- Optional-dependency extras `node`, `coordinator`, `client` are declared in `pyproject.toml` but left empty (`[]`) — populated by later tickets
- **No `tests/` directory, no `pytest` dependency, no ruff/mypy config** — this was an explicit decision in the spec (issue's own acceptance criteria are the verification bar). TDD discipline in this plan is applied via **executable shell/`python -c` verification commands run before and after each implementation step** (red: command fails or errors out because the code doesn't exist yet; green: command succeeds with the expected output) — not via a permanent automated test suite. Do not add `pytest` or a `tests/` folder while executing this plan.
- The repo's readme file is named exactly `Readme.md` (capital R) — reference it as such in `pyproject.toml`, not `README.md`

---

### Task 1: Base package — LICENSE, pyproject.toml, `mycelium/__init__.py`

**Files:**
- Create: `LICENSE`
- Create: `pyproject.toml`
- Create: `src/mycelium/__init__.py`

**Interfaces:**
- Produces: `mycelium.__version__` (str, `"0.1.0"`) — later tasks' `cli.py` stubs import this.
- Produces: `pyproject.toml` with `[project.scripts]` table present but empty — later tasks add one line each to it.

- [ ] **Step 1: Verify the package doesn't exist yet (red)**

Run: `python3 -c "import mycelium"`
Expected: `ModuleNotFoundError: No module named 'mycelium'`

- [ ] **Step 2: Create the LICENSE file**

```text
MIT License

Copyright (c) 2026 Varun Gambhir

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 3: Create `src/mycelium/__init__.py`**

```python
"""Mycelium: a framework for running LLM inference across GPUs volunteered by geographically separated users."""

__version__ = "0.1.0"
```

- [ ] **Step 4: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "mycelium"
dynamic = ["version"]
description = "A framework for running LLM inference across GPUs volunteered by geographically separated users."
readme = "Readme.md"
requires-python = ">=3.10"
license = {file = "LICENSE"}
authors = [{name = "Varun Gambhir"}]
classifiers = [
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
]

[project.optional-dependencies]
node = []
coordinator = []
client = []

[project.scripts]

[tool.setuptools.dynamic]
version = {attr = "mycelium.__version__"}

[tool.setuptools.packages.find]
where = ["src"]
```

- [ ] **Step 5: Install and verify (green)**

Run: `pip install -e .`
Expected: completes with `Successfully installed mycelium-0.1.0`

Run: `python3 -c "import mycelium; print(mycelium.__version__)"`
Expected: prints `0.1.0`

- [ ] **Step 6: Commit**

```bash
git add LICENSE pyproject.toml src/mycelium/__init__.py
git commit -m "feat: base mycelium package skeleton with MIT license"
```

---

### Task 2: Node agent CLI stub

**Files:**
- Create: `src/mycelium/node/__init__.py`
- Create: `src/mycelium/node/cli.py`
- Modify: `pyproject.toml` (`[project.scripts]` table)

**Interfaces:**
- Consumes: `mycelium.__version__` (from Task 1).
- Produces: console script `mycelium-node` → `mycelium.node.cli:main`.

- [ ] **Step 1: Verify the command doesn't exist yet (red)**

Run: `mycelium-node`
Expected: `command not found: mycelium-node` (or equivalent shell error)

- [ ] **Step 2: Create `src/mycelium/node/__init__.py`**

```python
"""Mycelium node agent: registers with the coordinator and serves inference requests (stub)."""
```

- [ ] **Step 3: Create `src/mycelium/node/cli.py`**

```python
"""CLI entry point for the Mycelium node agent (Phase 0 skeleton — no behavior yet)."""

from mycelium import __version__


def main() -> None:
    print(f"mycelium-node {__version__}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Add the console script to `pyproject.toml`**

Change:

```toml
[project.scripts]
```

To:

```toml
[project.scripts]
mycelium-node = "mycelium.node.cli:main"
```

- [ ] **Step 5: Reinstall and verify (green)**

Run: `pip install -e .`
Expected: completes with `Successfully installed mycelium-0.1.0`

Run: `mycelium-node`
Expected: prints `mycelium-node 0.1.0`

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/node pyproject.toml
git commit -m "feat: add mycelium-node CLI stub"
```

---

### Task 3: Coordinator CLI stub

**Files:**
- Create: `src/mycelium/coordinator/__init__.py`
- Create: `src/mycelium/coordinator/cli.py`
- Modify: `pyproject.toml` (`[project.scripts]` table)

**Interfaces:**
- Consumes: `mycelium.__version__` (from Task 1).
- Produces: console script `mycelium-coordinator` → `mycelium.coordinator.cli:main`.

- [ ] **Step 1: Verify the command doesn't exist yet (red)**

Run: `mycelium-coordinator`
Expected: `command not found: mycelium-coordinator` (or equivalent shell error)

- [ ] **Step 2: Create `src/mycelium/coordinator/__init__.py`**

```python
"""Mycelium coordinator: the single, publicly-reachable service that routes client requests to a healthy node (stub)."""
```

- [ ] **Step 3: Create `src/mycelium/coordinator/cli.py`**

```python
"""CLI entry point for the Mycelium coordinator (Phase 0 skeleton — no behavior yet)."""

from mycelium import __version__


def main() -> None:
    print(f"mycelium-coordinator {__version__}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Add the console script to `pyproject.toml`**

Change:

```toml
[project.scripts]
mycelium-node = "mycelium.node.cli:main"
```

To:

```toml
[project.scripts]
mycelium-node = "mycelium.node.cli:main"
mycelium-coordinator = "mycelium.coordinator.cli:main"
```

- [ ] **Step 5: Reinstall and verify (green)**

Run: `pip install -e .`
Expected: completes with `Successfully installed mycelium-0.1.0`

Run: `mycelium-coordinator`
Expected: prints `mycelium-coordinator 0.1.0`

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/coordinator pyproject.toml
git commit -m "feat: add mycelium-coordinator CLI stub"
```

---

### Task 4: Client CLI stub

**Files:**
- Create: `src/mycelium/client/__init__.py`
- Create: `src/mycelium/client/cli.py`
- Modify: `pyproject.toml` (`[project.scripts]` table)

**Interfaces:**
- Consumes: `mycelium.__version__` (from Task 1).
- Produces: console script `mycelium-client` → `mycelium.client.cli:main`.

- [ ] **Step 1: Verify the command doesn't exist yet (red)**

Run: `mycelium-client`
Expected: `command not found: mycelium-client` (or equivalent shell error)

- [ ] **Step 2: Create `src/mycelium/client/__init__.py`**

```python
"""Mycelium client: sends a prompt to the coordinator and gets a completion back (stub)."""
```

- [ ] **Step 3: Create `src/mycelium/client/cli.py`**

```python
"""CLI entry point for the Mycelium client (Phase 0 skeleton — no behavior yet)."""

from mycelium import __version__


def main() -> None:
    print(f"mycelium-client {__version__}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Add the console script to `pyproject.toml`**

Change:

```toml
[project.scripts]
mycelium-node = "mycelium.node.cli:main"
mycelium-coordinator = "mycelium.coordinator.cli:main"
```

To:

```toml
[project.scripts]
mycelium-node = "mycelium.node.cli:main"
mycelium-coordinator = "mycelium.coordinator.cli:main"
mycelium-client = "mycelium.client.cli:main"
```

- [ ] **Step 5: Reinstall and verify (green)**

Run: `pip install -e .`
Expected: completes with `Successfully installed mycelium-0.1.0`

Run: `mycelium-client`
Expected: prints `mycelium-client 0.1.0`

- [ ] **Step 6: Commit**

```bash
git add src/mycelium/client pyproject.toml
git commit -m "feat: add mycelium-client CLI stub"
```

---

### Task 5: Clean-checkout verification

**Files:**
- None created or modified — this task only runs verification commands against the work from Tasks 1–4.

**Interfaces:**
- Consumes: the full package built by Tasks 1–4 (all three console scripts, `mycelium.__version__`).
- Produces: nothing new — confirms issue #1's acceptance criteria hold from a genuinely clean checkout, not just the dev working tree.

- [ ] **Step 1: Build a clean checkout in a scratch directory**

```bash
rm -rf /tmp/mycelium-clean-checkout
git clone /Users/varungambhir/Documents/Zenkai/Mycelium /tmp/mycelium-clean-checkout
cd /tmp/mycelium-clean-checkout
git checkout phase-0/issue-1-project-skeleton
```

Expected: clone succeeds, branch checked out.

- [ ] **Step 2: Create a fresh virtual environment and install**

```bash
python3 -m venv /tmp/mycelium-clean-venv
source /tmp/mycelium-clean-venv/bin/activate
pip install -e /tmp/mycelium-clean-checkout
```

Expected: completes with `Successfully installed mycelium-0.1.0`, no errors.

- [ ] **Step 3: Run all three entry points**

```bash
mycelium-node
mycelium-coordinator
mycelium-client
```

Expected: each prints its own name and `0.1.0`, each exits with status 0 (`echo $?` after each shows `0`).

- [ ] **Step 4: Clean up**

```bash
deactivate
rm -rf /tmp/mycelium-clean-venv /tmp/mycelium-clean-checkout
```

- [ ] **Step 5: No commit needed**

This task verifies existing commits from Tasks 1–4; it doesn't change tracked files. If any step failed, use `superpowers:systematic-debugging` to find why before considering issue #1 done — do not patch the clean checkout directly, fix the source in the main working tree and re-run this task.
