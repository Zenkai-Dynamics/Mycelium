# Issue #3 — Developer Setup Guide — Design

Date: 2026-08-14
Status: Approved, not yet implemented
Issue: [#3 — Developer setup guide](https://github.com/Zenkai-Dynamics/Mycelium/issues/3)

This is the condensed record of the decisions made while brainstorming/
grilling issue #3, before implementation starts. It exists so the
*reasoning* behind each decision isn't lost, per the pattern established in
[the issue #1](2026-08-12-issue-1-project-skeleton-design.md) and
[issue #2](2026-08-14-issue-2-dependency-pinning-design.md) design docs.

## What issue #3 asks for

A single documented path from a clean checkout to a working development
environment, using the skeleton and CLI stubs from #1 and the pinned
dependencies from #2. It must be followable unassisted by someone who has
never touched the project, and must call out the hardware/CUDA
prerequisites documented in #2.

## Decisions made

**Location: `docs/SETUP.md`, not a Readme.md section.** Matches the
established pattern of `docs/dependencies.md` — a dedicated file for a
distinct concern, keeping `Readme.md` PRD-only (it explicitly frames
itself as "not an implementation plan"). `Readme.md`'s "Documentation Map"
(§5) gets one new line pointing to it, the same way `docs/dependencies.md`
was added there.

**Structure: split base setup from node/GPU setup.** The `node` extra
(Ray/vLLM/CUDA stack) only resolves on Linux x86_64 per
`pyproject.toml`'s `[tool.uv].environments` constraint, but the base
package — including all three stub CLIs from #1 — installs and runs on
any OS. The guide is two sections:

1. **Base setup** (any OS): clone → install → run all three stub CLIs.
   This is the section acceptance criterion #2 ("a person who has not
   touched the project before can follow it unassisted and end up with a
   working environment") is graded against, and it doesn't require GPU
   hardware to complete.
2. **Node / GPU setup** (Linux x86_64 only): installing the `node` extra
   for someone actually setting up a Phase 0 GPU node.

Rejected a single linear guide assuming a Linux GPU machine throughout —
it would make the guide impossible for a contributor without GPU access
to complete, which conflicts with acceptance criterion #2's "unassisted"
bar.

**Tooling: `uv` primary, `pip` as fallback — but not `uv sync`/`uv run`.**
`docs/dependencies.md` already established `uv.lock` as the project's
reproducibility source of truth. The original plan was `uv sync` +
`uv run mycelium-node` for the base path, but live verification on macOS
(§ below) found that `pyproject.toml`'s `[tool.uv].environments =
["sys_platform == 'linux' and platform_machine == 'x86_64'"]` (set in
issue #2 for the `node` extra's sake) restricts **the whole project's**
lockfile resolution, not just the `node` extra — `uv sync` and
`uv run <cli>` both fail outright on non-Linux with "The current Python
platform is not compatible with the lockfile's supported environments,"
even with zero extras requested. That breaks "base setup works on any
OS" if `uv sync`/`uv run` are the documented commands.

The verified working alternative, still uv-first: `uv venv` +
`uv pip install -e .` (resolves directly against `pyproject.toml`,
bypassing `uv.lock` and its environment restriction entirely — there's
nothing platform-specific to resolve for the base package), then invoke
each CLI via its direct binary path, `.venv/bin/mycelium-node` — not
`uv run` (which re-triggers the same lockfile check) and not
shell-specific activation (`.venv/bin/<cli>` is one invocation for
bash/zsh/fish alike). `pip install -e .` inside a plain
`python3 -m venv .venv` is documented as the one-line plain-pip fallback,
satisfying issue #1's original acceptance criterion that
`pip install -e .` succeeds.

A one-line "install uv if you don't have it" step (with a link to the
official installer) is included as a prerequisite, since acceptance
criterion #2 requires success without assuming any tooling beyond git and
Python is already present.

**Node/GPU section: points to `docs/dependencies.md`, doesn't duplicate
it.** `docs/dependencies.md` already fully documents the node-extra
install commands, the hardware requirements table, and real-hardware
verification, and is explicitly maintained as the single source of truth
("update in place as pins change"). Re-walking those commands/tables
inline in `docs/SETUP.md` would create a second copy that can drift out
of sync. The node/GPU section is a short pointer: what it's for, one
sentence of framing (Linux x86_64 only, real Phase 0 GPU nodes), and a
link.

**Verification: literal expected CLI output.** The base-setup section
shows the exact string each stub CLI prints (e.g. `mycelium-node 0.1.0`),
not a paraphrase like "it should print its name and version" — an exact
string to match is unambiguous for a reader completing the guide
unassisted.

**Live verification before/during implementation.** The base-setup
section is verified for real in a scratch clone on a non-Linux dev
machine (macOS) — proving the guide works without GPU hardware, matching
what most contributors will actually have. This verification is what
caught the `uv sync`/`uv run` incompatibility above; the commands
documented in the guide are the ones actually confirmed to work, not
ones assumed to work from reading `pyproject.toml`. The node/GPU section
is verified for real over SSH against the same real Phase 0 node used
for issue #2's hardware verification (`training-framework@iiitd`, the
`a6000` host alias in the operator's SSH config) — proving the
documented `node` extra path still works end to end, not just that it
reads plausibly.

## Explicitly out of scope for this issue

Any functional coordinator/node/client behavior — the CLIs remain #1's
stubs; this issue only documents installing and running them. Testing or
linting instructions (no test suite or dev-tooling group exists yet in
`pyproject.toml`). Windows support claims — the base section is expected
to work there (no OS-specific code) but is not tested on Windows as part
of this issue; the guide doesn't claim verified Windows support.
Re-deriving or re-verifying the CUDA/driver/hardware facts already
recorded in `docs/dependencies.md` — this issue links to them, it doesn't
re-confirm them.
