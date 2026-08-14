# Issue #2 — Dependency & Version Pinning (Ray, vLLM, and the rest of the stack) — Design

Date: 2026-08-14
Status: Approved, not yet implemented
Issue: [#2 — Dependency & version pinning (Ray, vLLM, and the rest of the stack)](https://github.com/Zenkai-Dynamics/Mycelium/issues/2)

This is the condensed record of the decisions made while brainstorming/
grilling issue #2, before implementation starts. It exists so the
*reasoning* behind each pinning decision isn't lost, per the pattern
established in [the issue #1 design doc](2026-08-12-issue-1-project-skeleton-design.md).

## What issue #2 asks for

Reproducible dependency management for Ray, vLLM, and the rest of the core
stack, pinned/locked so a fresh install produces the same environment every
time. CUDA/driver/GPU compatibility written down. Install verified to
succeed on at least one real GPU machine representative of a Phase 0 node.

## Target hardware (as of this design)

The operator's candidate Phase 0 nodes include NVIDIA A6000 and A100
machines, with possible access to an H100 as well — a mixed Ampere/Hopper
pool, not yet down to exact CUDA driver versions per machine. This is
"partially known": enough to pin against, not yet confirmed against real
hardware (that confirmation is this issue's own acceptance criterion #4).

## Decisions made

**Scope: node extra only.** `pyproject.toml`'s `node` optional-dependency
extra (declared empty in issue #1) gets populated with Ray and vLLM.
`coordinator` and `client` extras stay empty — nothing functional needs
dependencies there yet; those get populated (and locked) by whichever
later ticket first adds real code to them (e.g. #5 transport, #10
routing). Pinning them now with nothing concrete to pin would be
speculative.

**Version pins: `ray[llm]==2.57.0` + `vllm[audio]==0.25.1`, not vLLM's own
latest.** Researched directly against the vLLM and Ray GitHub release APIs
and source (2026-08-14):

- vLLM's own latest stable is v0.27.1 (released 2026-08-11, three days
  before this design). Ray's own latest stable is 2.57.0 (also
  2026-08-11).
- However, Ray's `llm` extra (`ray[llm]`, used for `ray.serve.llm` /
  Ray Serve LLM) **hard-pins an exact vLLM version internally** —
  confirmed by reading `python/setup.py` at each Ray release tag:
  - `ray-2.57.0` → `vllm[audio]==0.25.1`
  - `ray-2.56.0` → `vllm[audio]==0.22.0`
  - `ray-2.55.x` → `vllm[audio]>=0.18.0`
- Pinning vLLM independently at 0.27.1 alongside `ray[llm]==2.57.0` would
  conflict with Ray's own resolver constraint. Since Phase 0's node agent
  uses Ray to orchestrate vLLM (Ray Serve LLM, matching the gateway/serve
  split referenced in the Phase 0 design doc), the pairing Ray itself
  tests against is the correct one to pin, not vLLM's newest release in
  isolation.
- Both packages are declared **explicitly** in `pyproject.toml` (not left
  to resolve transitively through `ray[llm]` alone) so the coupling is
  visible and self-documenting rather than an implicit side effect of
  installing Ray.
- Rejected "one version back for both" (vLLM 0.26.0 / Ray 2.56.1) as an
  extra-caution option once the harder constraint (Ray's internal pin)
  was found — the ray[llm]/vllm pairing problem exists at every Ray
  version, so freshness of Ray itself was the only remaining axis, and
  latest-stable Ray with its bundled vLLM pin was preferred.

**Lockfile mechanism: `uv.lock`, with a bare-pip fallback export.**
`uv` was already the recommended day-to-day dev tool per issue #1's
design (available on the operator's machine, works alongside the
`setuptools` build backend — the two are orthogonal). `uv lock` generates
`uv.lock`, committed to the repo, as the source-of-truth reproducibility
artifact. `uv sync --extra node` on a target machine installs exactly
what's locked.

Because issue #1 specifically chose `setuptools` so that a bare
`pip install -e .` needs no extra tooling on an HPC node, and a target
node might not have `uv` preinstalled, this issue also exports a plain
pinned `requirements-node-lock.txt` from `uv.lock`
(`uv export --extra node -o requirements-node-lock.txt`), installable via
plain `pip install -r`. `uv.lock` stays the single source of truth; the
export is regenerated from it, never hand-maintained, and is a fallback
path, not the primary one. Bootstrapping `uv` itself if absent
(`pip install uv` or the official installer) is a setup-doc detail for
issue #3, not this one — #2 only needs the mechanism and prerequisites
written down for #3 to link to.

**CUDA/driver/GPU compatibility: new `docs/dependencies.md`.** Records
what issue #2's acceptance criteria require to be written down, based on
`vllm==0.25.1` / `ray==2.57.0`'s actual documented requirements (verified
against vLLM's install docs, not assumed):

- CUDA: default prebuilt wheels target CUDA 12.9
- Minimum NVIDIA driver: 550.54.14 (Linux)
- GPU compute capability: ≥7.5 required — A6000 (8.6), A100 (8.0), H100
  (9.0) all qualify comfortably
- Python: 3.10–3.14 (vLLM's supported range), compatible with the
  project's existing `>=3.10` floor from issue #1

This is a new, separate doc from `docs/phases/phase-0-foundation.md`'s
"Exact model choice is pending real hardware specs" open risk — that risk
is about which HF model to serve and stays open for issue #6 to resolve;
`docs/dependencies.md` is scoped to the dependency stack itself.

**Real-hardware verification: SSH, at implementation time.** The
operator will provide SSH access to a real target node (A6000/A100,
possibly H100) when the implementation plan reaches that step. The plan
includes a task that runs `uv sync --extra node` (or the
`requirements-node-lock.txt` fallback) on that machine, confirms `ray`
and `vllm` import successfully and report their pinned versions, and
records the result in the issue/PR. This is **not** a full model-serving
test — running an actual model under vLLM on target hardware is issue
#6's job, not #2's. #2's bar is: the pinned stack installs and imports
correctly on real hardware.

## Explicitly out of scope for this issue

Populating `coordinator`/`client` extras. Choosing or validating the
Phase 0 HF model (#6). Actually wiring Ray Serve LLM into the node agent's
runtime code (#7) — this issue only pins the dependency, it doesn't write
orchestration code. A full `vllm serve`/model-serving smoke test on real
hardware (#6). Resolving `docs/phases/phase-0-foundation.md`'s node
network reachability risk (#4) or model-choice risk (#6) — only the
dependency-pinning-specific facts land in `docs/dependencies.md` here.
