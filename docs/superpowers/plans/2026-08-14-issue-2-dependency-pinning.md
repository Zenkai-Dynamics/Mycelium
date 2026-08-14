# Issue #2 — Dependency & Version Pinning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Task 5 is the exception** — it requires live SSH credentials from the
> operator and must run in the main/orchestrating session, not a dispatched
> subagent. See Task 5's header for details.

**Goal:** Pin Ray and vLLM (and their transitive stack) to exact, reproducible versions for Phase 0's node extra, document the CUDA/driver/GPU compatibility those pins assume, and verify the pinned install actually succeeds on a real GPU machine.

**Architecture:** `pyproject.toml`'s `node` extra gets two explicit pins (`ray[llm]==2.57.0`, `vllm[audio]==0.25.1`) plus a `[tool.uv].environments` marker locking resolution to `linux`/`x86_64` regardless of the machine `uv lock` runs on (Phase 0 nodes are Linux HPC/VPN machines; the dev machine used to generate the lock may not be). `uv.lock` is the reproducibility source of truth; a generated `requirements-node-lock.txt` is a bare-`pip`-installable fallback for machines without `uv`. `docs/dependencies.md` records the CUDA/driver/GPU facts and, once run, the real-hardware verification result.

**Tech Stack:** Python 3.10+, `uv` (lockfile/export tooling) on top of the existing `setuptools` build backend, no new runtime code.

**Spec:** [docs/superpowers/specs/2026-08-14-issue-2-dependency-pinning-design.md](../specs/2026-08-14-issue-2-dependency-pinning-design.md)

## Global Constraints

- Pins: `ray[llm]==2.57.0` and `vllm[audio]==0.25.1`, both declared explicitly in `pyproject.toml`'s `node` extra — not left to resolve transitively through `ray[llm]` alone
- `coordinator` and `client` extras stay `[]` — out of scope for this issue
- Lock target platform: `sys_platform == 'linux' and platform_machine == 'x86_64'` via `[tool.uv].environments` in `pyproject.toml` — this is what makes `uv lock` resolvable from a non-Linux dev machine and is a hard requirement, not optional, since the actual node hardware is Linux-only
- `uv.lock` is the single source of truth; `requirements-node-lock.txt` is generated from it (`uv export --extra node --no-emit-project -o requirements-node-lock.txt`) and must never be hand-edited — regenerate both together whenever pins change
- No `tests/` directory, no `pytest` dependency — matches issue #1's precedent. Verification is executable shell/`python3 -c` commands run before (red) and after (green) each step, same as the issue #1 plan
- New doc: `docs/dependencies.md` — CUDA 12.9, minimum driver 550.54.14 (Linux), GPU compute capability ≥7.5, Python 3.10–3.14
- Real-hardware verification (Task 5) requires SSH access the operator provides at that point in execution — do not fabricate or skip this step silently if access isn't available; stop and ask

---

### Task 1: Populate `node` extra pins + lock-target platform marker

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `pyproject.toml` with `project.optional-dependencies.node = ["ray[llm]==2.57.0", "vllm[audio]==0.25.1"]` and `tool.uv.environments = ["sys_platform == 'linux' and platform_machine == 'x86_64'"]` — Task 2's `uv lock` depends on both being present.

- [ ] **Step 1: Verify current state (red)**

Run:
```bash
python3 -c "
import tomllib
with open('pyproject.toml', 'rb') as f:
    data = tomllib.load(f)
print(data['project']['optional-dependencies']['node'])
print('tool' in data and 'uv' in data.get('tool', {}))
"
```
Expected:
```
[]
False
```

- [ ] **Step 2: Edit `pyproject.toml`**

Change:
```toml
[project.optional-dependencies]
node = []
coordinator = []
client = []
```
To:
```toml
[project.optional-dependencies]
node = [
    "ray[llm]==2.57.0",
    "vllm[audio]==0.25.1",
]
coordinator = []
client = []
```

Add a new table at the end of the file (after `[tool.setuptools.packages.find]`):
```toml

[tool.uv]
environments = [
    "sys_platform == 'linux' and platform_machine == 'x86_64'",
]
```

- [ ] **Step 3: Verify the edit (green)**

Run:
```bash
python3 -c "
import tomllib
with open('pyproject.toml', 'rb') as f:
    data = tomllib.load(f)
assert data['project']['optional-dependencies']['node'] == ['ray[llm]==2.57.0', 'vllm[audio]==0.25.1']
assert data['tool']['uv']['environments'] == [\"sys_platform == 'linux' and platform_machine == 'x86_64'\"]
print('OK')
"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "feat: pin ray[llm] and vllm[audio] for the node extra"
```

---

### Task 2: Generate and commit `uv.lock`

**Files:**
- Create: `uv.lock`

**Interfaces:**
- Consumes: `pyproject.toml`'s `node` extra pins and `tool.uv.environments` marker (Task 1).
- Produces: `uv.lock` containing a resolved `vllm` entry at version `0.25.1` and a resolved `ray` entry at version `2.57.0` — Task 3's export and Task 5's real-hardware `uv sync` both depend on this file existing and being current.

- [ ] **Step 1: Verify the lock doesn't exist yet (red)**

Run: `test -f uv.lock && echo EXISTS || echo MISSING`
Expected: `MISSING`

- [ ] **Step 2: Generate the lock**

Run: `uv lock`

This resolves the full dependency graph for the `linux`/`x86_64` target declared in Task 1, regardless of the platform this command runs on. Expect it to print `Resolved <N> packages in <T>s` and exit 0. This can take up to a couple of minutes.

- [ ] **Step 3: Verify the lock (green)**

Run:
```bash
test -f uv.lock && echo "uv.lock exists"
grep -A1 '^name = "vllm"' uv.lock
grep -A1 '^name = "ray"' uv.lock
```
Expected:
```
uv.lock exists
name = "vllm"
version = "0.25.1"
name = "ray"
version = "2.57.0"
```

- [ ] **Step 4: Commit**

```bash
git add uv.lock
git commit -m "feat: lock node extra dependencies via uv"
```

---

### Task 3: Export bare-pip fallback lockfile

**Files:**
- Create: `requirements-node-lock.txt`

**Interfaces:**
- Consumes: `uv.lock` (Task 2) — this file is generated from it, never hand-edited.
- Produces: `requirements-node-lock.txt`, a hash-pinned, `pip install -r`-installable fallback for machines without `uv` — referenced by `docs/dependencies.md` (Task 4) and available to Task 5's real-hardware verification as an alternate install path.

- [ ] **Step 1: Verify the export doesn't exist yet (red)**

Run: `test -f requirements-node-lock.txt && echo EXISTS || echo MISSING`
Expected: `MISSING`

- [ ] **Step 2: Generate the export**

Run: `uv export --extra node --no-emit-project -o requirements-node-lock.txt`

(`--no-emit-project` excludes the local `mycelium` package itself from the export — this file pins third-party dependencies only, not an editable install of the project, which needs its own path context.)

- [ ] **Step 3: Verify the export (green)**

Run:
```bash
test -f requirements-node-lock.txt && echo "file exists"
head -2 requirements-node-lock.txt
grep "^vllm==0.25.1" requirements-node-lock.txt
grep "^ray==2.57.0" requirements-node-lock.txt
```
Expected:
```
file exists
# This file was autogenerated by uv via the following command:
#    uv export --extra node --no-emit-project -o requirements-node-lock.txt
vllm==0.25.1 ; platform_machine == 'x86_64' and sys_platform == 'linux' \
ray==2.57.0 ; platform_machine == 'x86_64' and sys_platform == 'linux' \
```

- [ ] **Step 4: Commit**

```bash
git add requirements-node-lock.txt
git commit -m "feat: export bare-pip fallback lockfile for the node extra"
```

---

### Task 4: Write `docs/dependencies.md`

**Files:**
- Create: `docs/dependencies.md`
- Modify: `docs/phases/phase-0-foundation.md:5` (the `Related:` line)

**Interfaces:**
- Consumes: the exact pins from Task 1 (`ray[llm]==2.57.0`, `vllm[audio]==0.25.1`) and the reproducibility mechanism from Tasks 2–3.
- Produces: `docs/dependencies.md` with a `## Real-hardware verification` section left in an explicit "not yet run" state — Task 5 replaces that section's content, it doesn't create the file.

- [ ] **Step 1: Verify the doc doesn't exist yet (red)**

Run: `test -f docs/dependencies.md && echo EXISTS || echo MISSING`
Expected: `MISSING`

- [ ] **Step 2: Create `docs/dependencies.md`**

```markdown
# Dependency & Hardware Compatibility

Related: [Phase 0 — Foundation](phases/phase-0-foundation.md) · [Issue #2 design](superpowers/specs/2026-08-14-issue-2-dependency-pinning-design.md)

This doc records the exact dependency versions Phase 0's node stack is
pinned to, and the CUDA/driver/GPU hardware compatibility those versions
assume. See the design doc above for the reasoning behind each choice.

## Pinned versions (`mycelium[node]`)

| Package | Version | Why this exact version |
|---|---|---|
| `ray[llm]` | 2.57.0 | Latest stable Ray release (as of 2026-08-14). |
| `vllm[audio]` | 0.25.1 | Not vLLM's own latest release (0.27.1) — `ray[llm]==2.57.0` hard-pins `vllm[audio]==0.25.1` internally so that `ray.serve.llm` works correctly, so vLLM is pinned to match rather than independently. |

**Reproducibility mechanism:** `uv.lock` (source of truth, generated by
`uv lock`) plus a generated fallback `requirements-node-lock.txt` (via
`uv export --extra node --no-emit-project`) for machines without `uv`.
Regenerate both together whenever a pin changes:

```bash
uv lock
uv export --extra node --no-emit-project -o requirements-node-lock.txt
```

Never hand-edit either file.

## Hardware requirements

- **CUDA:** 12.9 — the version `vllm==0.25.1` / `ray==2.57.0`'s prebuilt wheels target by default.
- **Minimum NVIDIA driver:** 550.54.14 (Linux).
- **GPU compute capability:** ≥ 7.5 required. Checked against Phase 0's candidate node types:
  - NVIDIA A6000 — compute capability 8.6 ✓
  - NVIDIA A100 — compute capability 8.0 ✓
  - NVIDIA H100 — compute capability 9.0 ✓
- **Python:** 3.10–3.14 (vLLM 0.25.1's supported range), within the project's existing `>=3.10` floor.
- **OS / architecture:** Linux, x86_64 only. `uv.lock` is generated for `sys_platform == 'linux' and platform_machine == 'x86_64'` specifically (see `[tool.uv].environments` in `pyproject.toml`) — it will not resolve or install on macOS/Windows dev machines by design, since Phase 0 nodes are Linux HPC/VPN machines only.

## Real-hardware verification

Not yet run. This section will record: the date, which real node (GPU
model, driver version) `uv sync --extra node` (or the
`requirements-node-lock.txt` fallback) was installed on, and confirmation
that the installed `ray`/`vllm` versions match the pins above.
```

- [ ] **Step 3: Add a `Related:` link from the phase-0 doc**

In `docs/phases/phase-0-foundation.md`, change line 5 from:
```markdown
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md)
```
To:
```markdown
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md) · [Dependency & hardware compatibility](../dependencies.md)
```

- [ ] **Step 4: Verify (green)**

Run:
```bash
test -f docs/dependencies.md && echo "doc exists"
grep "550.54.14" docs/dependencies.md
grep "compute capability" docs/dependencies.md
grep "dependencies.md" docs/phases/phase-0-foundation.md
```
Expected: all four checks print a match, no errors.

- [ ] **Step 5: Commit**

```bash
git add docs/dependencies.md docs/phases/phase-0-foundation.md
git commit -m "docs: document CUDA/driver/GPU compatibility for pinned node deps"
```

---

### Task 5: Real-hardware verification (orchestrator-run, not subagent-dispatched)

**Do not dispatch this task to a subagent.** It requires live SSH
credentials handed over by the operator mid-conversation; relaying
credentials through an isolated subagent transcript is unnecessary
credential exposure. Run it directly in the main session.

**Files:**
- Modify: `docs/dependencies.md` (the `## Real-hardware verification` section written in Task 4)

**Interfaces:**
- Consumes: `pyproject.toml`, `uv.lock` from Tasks 1–2 (or `requirements-node-lock.txt` from Task 3 as the fallback path).
- Produces: a completed `## Real-hardware verification` section in `docs/dependencies.md` — this is what satisfies issue #2's acceptance criterion "Installing succeeds on at least one real GPU machine representative of a Phase 0 node."

- [ ] **Step 1: Get connection details**

If SSH access to a real target node (A6000/A100/H100) hasn't already been provided in this conversation, stop and ask the operator for it: host/IP, username, port, and auth method. Do not proceed on an assumption.

- [ ] **Step 2: Confirm GPU/driver facts on the node**

Run (substituting the real target):
```bash
ssh <target> "nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv"
```
Expected: a GPU name, driver version, and compute capability are printed. Compare the driver version against the 550.54.14 minimum documented in `docs/dependencies.md` — if it's lower, stop and report this rather than proceeding to install (per systematic-debugging: don't paper over a real environment mismatch).

- [ ] **Step 3: Get the pinned pyproject/lock onto the node and sync**

```bash
ssh <target> "command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh"
scp pyproject.toml uv.lock <target>:~/mycelium-dep-check/
ssh <target> "cd ~/mycelium-dep-check && uv sync --extra node"
```
Expected: `uv sync` completes with exit 0. This step downloads a large stack (PyTorch, CUDA libraries, vLLM) and may take several minutes.

If `uv` isn't available and can't be bootstrapped, fall back to:
```bash
scp requirements-node-lock.txt <target>:~/mycelium-dep-check/
ssh <target> "cd ~/mycelium-dep-check && python3 -m venv venv && ./venv/bin/pip install -r requirements-node-lock.txt"
```

- [ ] **Step 4: Verify installed versions match the pins**

```bash
ssh <target> "cd ~/mycelium-dep-check && uv run python3 -c \"import ray, vllm; print(ray.__version__, vllm.__version__)\""
```
Expected: `2.57.0 0.25.1`. If the fallback venv path was used instead, run the equivalent `./venv/bin/python3 -c "..."` check.

- [ ] **Step 5: Record the result**

Replace `docs/dependencies.md`'s `## Real-hardware verification` section (currently "Not yet run...") with the actual date, GPU model, driver version, and confirmed `ray`/`vllm` versions from Steps 2–4.

- [ ] **Step 6: Clean up and commit**

```bash
ssh <target> "rm -rf ~/mycelium-dep-check"
git add docs/dependencies.md
git commit -m "docs: record real-hardware dependency verification"
```
