# Issue #3 — Developer Setup Guide — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `docs/SETUP.md`, a single documented path from a clean checkout to a working Mycelium dev environment, covering the platform-agnostic base install (clone → install → run the three stub CLIs from issue #1) and a short pointer to the Linux/GPU node-extra install already documented in `docs/dependencies.md` (issue #2).

**Architecture:** One new doc, two sections. "Base setup" is verified to work on any OS without GPU hardware — this is the section a first-time contributor completes. "Node / GPU setup" doesn't duplicate `docs/dependencies.md`'s commands/tables; it explains what the section is for and links out, since that file is the established single source of truth for pinned versions and hardware requirements. `Readme.md`'s Documentation Map (§5) gets one new line pointing to the new file.

**Tech Stack:** Markdown docs only. No code, no new dependencies, no test suite (none exists yet in this repo — see `pyproject.toml`).

## Global Constraints

- Base-setup commands must work on any OS without GPU hardware — this is graded by literally running them, not by inspection.
- Do **not** use `uv sync` or `uv run <cli>` for the base install path. `pyproject.toml`'s `[tool.uv].environments = ["sys_platform == 'linux' and platform_machine == 'x86_64'"]` restricts the whole project's lockfile resolution (not just the `node` extra), so both commands fail outright on non-Linux with "The current Python platform is not compatible with the lockfile's supported environments" — confirmed by live testing on macOS during design. Use `uv venv` + `uv pip install -e .` instead (resolves directly against `pyproject.toml`, bypassing the lockfile), and invoke each CLI via its direct binary path (`.venv/bin/mycelium-node`, etc.) rather than `uv run`.
- Show literal expected CLI output in verification steps (e.g. `mycelium-node 0.1.0`), not a paraphrase.
- The installed package version is `0.1.0` (from `src/mycelium/__init__.py`'s `__version__`) — use this exact string in expected-output blocks.
- The node/GPU section must not duplicate install commands or the hardware-requirements table from `docs/dependencies.md` — link to it instead.
- Repo clone URL to use in examples: `https://github.com/Zenkai-Dynamics/Mycelium.git`.
- New doc lives at `docs/SETUP.md`, following the existing pattern of `docs/dependencies.md` (a dedicated file, not a `Readme.md` section).

---

### Task 1: `docs/SETUP.md` — Prerequisites + Base setup section

**Files:**
- Create: `docs/SETUP.md`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `docs/SETUP.md` containing a `# Mycelium — Developer Setup` top-level heading, a `## Prerequisites` section, and a `## Base setup` section. Task 2 appends a `## Node / GPU setup` section to the same file below this one — do not add a top-level closing note or "that's it!" line at the end of the Base setup section that would read oddly once Task 2's section follows it.

- [ ] **Step 1: Confirm the doc doesn't exist yet (red)**

Run: `ls docs/SETUP.md`
Expected: `ls: docs/SETUP.md: No such file or directory` (or platform equivalent) — nothing to verify against yet.

- [ ] **Step 2: Write `docs/SETUP.md` with this exact content**

```markdown
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
- **Python 3.10 or newer**
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
   .venv/bin/mycelium-node
   .venv/bin/mycelium-coordinator
   .venv/bin/mycelium-client
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
```

- [ ] **Step 3: Commit**

```bash
git add docs/SETUP.md
git commit -m "docs: add developer setup guide (prerequisites + base setup)"
```

- [ ] **Step 4: Verify for real, in a scratch clone (green)**

Run, from inside your repo checkout, cloning from the local working tree
(not GitHub — your branch isn't pushed yet, so a local clone is the only
way to get a scratch copy of what you just committed). This must happen
*after* Step 3's commit — `git clone` reads committed history, not
uncommitted working-tree changes, so cloning before committing would
silently test stale content:

```bash
REPO_DIR=$(pwd)
cd /tmp && rm -rf mycelium-setup-check && git clone "$REPO_DIR" mycelium-setup-check
cd mycelium-setup-check
uv venv
uv pip install -e .
.venv/bin/mycelium-node
.venv/bin/mycelium-coordinator
.venv/bin/mycelium-client
```

Expected: the three commands print exactly `mycelium-node 0.1.0`, `mycelium-coordinator 0.1.0`, `mycelium-client 0.1.0`. This has already been confirmed to work on both macOS and a real Linux GPU node during design — this step is re-confirming the doc's exact copy-pasted commands still produce that result, not exploring new territory. Also run the plain-pip fallback block (`python3 -m venv .venv2 && .venv2/bin/pip install -e . && .venv2/bin/mycelium-node`) at least once to confirm it too still works. Clean up the scratch clone afterward (`cd /tmp && rm -rf mycelium-setup-check`). If verification fails, fix `docs/SETUP.md` and amend the Step 3 commit rather than leaving a broken doc committed.

---

### Task 2: `docs/SETUP.md` Node/GPU section + `Readme.md` doc-map entry

**Files:**
- Modify: `docs/SETUP.md` (append a new `## Node / GPU setup` section after Task 1's `## Base setup` section)
- Modify: `Readme.md` (§5 "Documentation Map" bullet list)

**Interfaces:**
- Consumes: `docs/SETUP.md` as created in Task 1 (appends below its `## Base setup` section; do not alter Task 1's content).
- Produces: the finished `docs/SETUP.md`, referenced by `Readme.md`'s Documentation Map from this task on.

- [ ] **Step 1: Confirm the section doesn't exist yet (red)**

Run: `grep -c "## Node / GPU setup" docs/SETUP.md`
Expected: `0`

- [ ] **Step 2: Append this exact content to the end of `docs/SETUP.md`**

```markdown

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
```

- [ ] **Step 3: Add the `docs/SETUP.md` entry to `Readme.md`'s Documentation Map**

In `Readme.md`, find the "## 5. Documentation Map" section's bullet list (it currently ends with the `docs/dependencies.md` bullet). Add this as a new bullet, placed first in the list since it's the entry point a new contributor reads before any of the others:

```markdown
- `docs/SETUP.md` — developer setup guide: clean checkout to a working dev environment, covering the platform-agnostic base install and the Linux/GPU node extra.
```

- [ ] **Step 4: Verify the reachability/content this section links to (green)**

Confirm `docs/dependencies.md` actually contains the three things the new section claims it does:

```bash
grep -n "ray\[llm\]==2.57.0\|vllm\[audio\]==0.25.1" docs/dependencies.md
grep -n "uv sync --extra node\|requirements-node-lock.txt" docs/dependencies.md
grep -n "Minimum NVIDIA driver\|compute capability" docs/dependencies.md
```

Expected: each `grep` returns at least one matching line — confirming the pointer isn't linking to content that doesn't actually exist there.

Separately, confirm the real Phase 0 node this section describes is still reachable and the node extra still installs cleanly, since that's the concrete thing "Linux x86_64 with a CUDA-capable GPU" refers to:

```bash
ssh -o ConnectTimeout=8 -o BatchMode=yes a6000 'echo ok'
```

Expected: `ok`. (This alias is the operator's local SSH config, not something to hardcode into the doc — the doc itself only ever says "a Linux x86_64 machine with a CUDA-capable GPU," never a specific hostname.) The full `uv sync --extra node` install and `ray`/`vllm` import were already verified end-to-end against this exact node during design (`ray: 2.57.0`, `vllm: 0.25.1`, matching `docs/dependencies.md`) — this step is a lighter reachability re-check, not a re-run of the ~10-minute full install, since nothing in this task changes the node extra itself.

- [ ] **Step 5: Read the whole file once, end to end**

Run: `cat docs/SETUP.md`

Confirm: the Base setup section (Task 1) and Node/GPU section (this task) read as one coherent document — no duplicate headings, no dangling references, Markdown link syntax renders correctly (relative links `phases/phase-0-foundation.md` and `dependencies.md` resolve correctly from `docs/SETUP.md`'s own location).

- [ ] **Step 6: Commit**

```bash
git add docs/SETUP.md Readme.md
git commit -m "docs: add node/GPU setup section and link setup guide from Readme"
```
