# Issue #6 — Validate the Phase 0 Model via vLLM — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the resolved Phase 0 model choice in `docs/phases/phase-0-foundation.md`, replacing its "exact model TBD" note and "pending real hardware specs" open risk with the actual chosen model and a pointer to the real verification evidence.

**Architecture:** This is a documentation-only plan. The substantive work for issue #6 — choosing `Qwen/Qwen2.5-7B-Instruct`, installing the pinned `node` extra on a real GPU node (`a6000`), debugging two real dependency/packaging bugs found along the way (a `nvidia-cuda-nvcc`/`nvidia-cuda-runtime` version drift in `uv.lock`, fixed permanently; a `flashinfer`/CCCL packaging inconsistency, worked around via vLLM's own supported `VLLM_USE_FLASHINFER_SAMPLER=0` fallback), and actually serving the model and getting two correct completions back — is **already complete**, performed directly against real infrastructure (not delegated to a subagent, for the same reason issue #4's and #5's live verification steps weren't: it required live SSH access, GPU hardware, and real-time debugging judgment a stateless subagent isn't positioned for). That work is fully recorded in:

- `docs/superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md` (design rationale + the full "Live verification & debugging" narrative)
- `docs/dependencies.md` (the `nvidia-cuda-nvcc` pin, and a new real-hardware verification entry for the actual serve+prompt test)
- `pyproject.toml`/`uv.lock`/`requirements-node-lock.txt` (the dependency fix itself, already committed)

This plan's one task transcribes the now-settled facts into the last remaining place issue #6's acceptance criteria requires: the phase doc.

**Tech Stack:** Markdown docs only. No code, no new dependencies (already handled).

## Global Constraints

- Chosen model: `Qwen/Qwen2.5-7B-Instruct`.
- Why it fits: ~15 GB in bf16, comfortably under a single 48 GB A6000 GPU's VRAM budget, with large headroom for KV cache.
- Verified via: `vllm serve Qwen/Qwen2.5-7B-Instruct --port 8811` (with `CUDA_VISIBLE_DEVICES=0`, `VLLM_USE_FLASHINFER_SAMPLER=0`) on a real `a6000` node — `Application startup complete`, and a real prompt (*"What is the capital of France? Answer in one word."*) returned the correct completion `"Paris"`.
- Do not re-derive or re-verify any of these facts — they're already established and committed. Do not touch `pyproject.toml`, `uv.lock`, `requirements-node-lock.txt`, or `docs/dependencies.md` — already correct, already committed.
- Exact current text being replaced (`docs/phases/phase-0-foundation.md`, verified present at the time this plan was written — confirm it's still there before editing, in case something else changed it first):
  - Line ~37 (under `## In scope`): `- One HF model, chosen to fit comfortably on the smallest node's VRAM (exact model TBD against real hardware specs, not fixed here)`
  - Line ~68 (under `## Open risks / unresolved decisions`): `- **Exact model choice is pending real hardware specs** for the HPC/VPN nodes the operator will use.`

---

### Task 1: Resolve the model choice in `docs/phases/phase-0-foundation.md`

**Files:**
- Modify: `docs/phases/phase-0-foundation.md` (the "Related:" line near the top, the "In scope" bullet, and the "Open risks / unresolved decisions" bullet)

**Interfaces:**
- Consumes: nothing from other tasks — this is the plan's only task.
- Produces: nothing consumed elsewhere — last task in the plan.

- [ ] **Step 1: Confirm the current text is still present (red)**

Run:

```bash
grep -n "exact model TBD" docs/phases/phase-0-foundation.md
grep -n "Exact model choice is pending real hardware specs" docs/phases/phase-0-foundation.md
```

Expected: one matching line each — the doc hasn't been updated yet.

- [ ] **Step 2: Add the issue #6 design doc to the "Related:" line**

Find this line near the top of `docs/phases/phase-0-foundation.md`:

```markdown
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [ADR-0002 — node transport model](../adr/0002-node-transport-model.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md) · [Dependency & hardware compatibility](../dependencies.md)
```

Replace it with (appending the new entry at the end, after the existing dependency-compatibility link):

```markdown
Related: [ADR-0001 — project name](../adr/0001-project-name.md) · [ADR-0002 — node transport model](../adr/0002-node-transport-model.md) · [Phase 0 design rationale](../superpowers/specs/2026-08-12-mycelium-phase0-design.md) · [Dependency & hardware compatibility](../dependencies.md) · [Model choice & vLLM validation](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md)
```

- [ ] **Step 3: Replace the "In scope" model bullet**

Find this bullet under `## In scope`:

```markdown
- One HF model, chosen to fit comfortably on the smallest node's VRAM (exact model TBD against real hardware specs, not fixed here)
```

Replace it with:

```markdown
- One HF model — [`Qwen/Qwen2.5-7B-Instruct`](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct), chosen to fit comfortably on the smallest node's VRAM (~15 GB in bf16, well under a single 48 GB A6000 GPU) and confirmed to serve real completions correctly via `vllm serve` on real target hardware — see [the model-choice design doc](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md) for the selection rationale and the real-hardware verification
```

- [ ] **Step 4: Replace the "Open risks" model-choice bullet**

Find this bullet under `## Open risks / unresolved decisions`:

```markdown
- **Exact model choice is pending real hardware specs** for the HPC/VPN nodes the operator will use.
```

Replace it with:

```markdown
- **Exact model choice: resolved.** `Qwen/Qwen2.5-7B-Instruct` was confirmed to run correctly via `vllm serve` on a real target node (`a6000`, single RTX A6000 GPU, pinned via `CUDA_VISIBLE_DEVICES=0`) — a real prompt returned the correct completion. Getting there required fixing a real dependency-version drift (`nvidia-cuda-nvcc` vs. `nvidia-cuda-runtime`, now corrected in `pyproject.toml`/`uv.lock`) and working around a `flashinfer`/CCCL packaging inconsistency via vLLM's own supported `VLLM_USE_FLASHINFER_SAMPLER=0` fallback — both are permanent, carried-forward facts for whoever builds the node agent next (issue #7 will need to set the same env var when it starts vLLM). See [the design doc](../superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md) for the full investigation.
```

Do not touch the "Node auth mechanism is a placeholder" or "Outbound egress from node environments is unverified" bullets in the same section — they stay exactly as they are.

- [ ] **Step 5: Verify the update (green)**

Run:

```bash
grep -n "exact model TBD" docs/phases/phase-0-foundation.md
grep -n "Exact model choice is pending real hardware specs" docs/phases/phase-0-foundation.md
```

Expected: no output for either — the old, unresolved wording is gone.

```bash
grep -n "Qwen2.5-7B-Instruct" docs/phases/phase-0-foundation.md
grep -c "Model choice & vLLM validation" docs/phases/phase-0-foundation.md
```

Expected: the first returns 2 matching lines (the "In scope" bullet and the "Open risks" bullet); the second returns `1`.

```bash
ls docs/superpowers/specs/2026-08-14-issue-6-validate-model-vllm-design.md
```

Expected: the file exists — the new "Related:" link and both new bullet links point at a real file.

- [ ] **Step 6: Read the whole file once, end to end**

Run: `cat docs/phases/phase-0-foundation.md`

Confirm: the document reads coherently — no duplicate headings, no dangling references, the "Node auth mechanism" and "Outbound egress" open-risk bullets are still present and unchanged, and the two other unrelated "In scope" bullets are untouched.

- [ ] **Step 7: Commit**

```bash
git add docs/phases/phase-0-foundation.md
git commit -m "docs: resolve Phase 0 model choice (Qwen2.5-7B-Instruct)"
```
