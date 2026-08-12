# Issue #1 — Project Skeleton & Packaging — Design

Date: 2026-08-12
Status: Approved, not yet implemented
Issue: [#1 — Project skeleton & packaging](../../../Readme.md)

This is the condensed record of the decisions made while brainstorming/
grilling issue #1, before implementation starts. It exists so the
*reasoning* behind each packaging decision isn't lost, per the pattern
established in [the Phase 0 design doc](2026-08-12-mycelium-phase0-design.md).

## What issue #1 asks for

An installable Python package skeleton for Mycelium: package layout with
clearly separated spots for node-agent, coordinator, and client code
(stubs only, nothing functional), three CLI entry points that each just
confirm they run, and `pip install -e .` succeeding from a clean checkout.

## Decisions made

**Package structure.** One installable package, `mycelium`, not three
separate packages. Subpackages `mycelium.node`, `mycelium.coordinator`,
`mycelium.client` hold each component's code. Naming matches the CLI
entry-point prefixes exactly (no "node_agent" stutter — "agent" is
already implied by context). Rejected three independent packages: more
packaging overhead for a Phase 0 skeleton, and the issue's acceptance
criteria describe a single `pip install -e .`.

**Dependency extras.** `pyproject.toml` declares optional-dependency
extras (`mycelium[node]`, `mycelium[coordinator]`, `mycelium[client]`)
mirroring the `ray[train]`-style pattern the operator has used before —
lets each component pull in only the (currently heavier, later) deps it
needs, e.g. vLLM/GPU deps for the node agent only. Extras are declared
but empty for now since nothing functional runs yet.

**Directory layout.** `src/mycelium/...` (src-layout), not a flat
`mycelium/...` at repo root. Prevents accidentally importing an
uninstalled local package instead of the pip-installed one — relevant
here since the acceptance criteria explicitly test `pip install -e .`
from a clean checkout.

**Build backend.** `setuptools`, not `uv`'s own build backend
(`uv_build`) or Poetry/hatchling. Chosen for HPC-friendliness: the phase-0
doc flags that candidate nodes sit behind a CDAC HPC environment and a
university VPN with likely-restricted internet access, so the backend
declared in `pyproject.toml` needs to work with a bare `pip install -e .`
and no extra tooling fetch. `uv` remains the recommended day-to-day dev
tool (venv + install) on top of that backend — it's already available on
the operator's machine — but isn't required for the package to install.

**Python version.** 3.10+. Modern enough for current typing syntax,
conservative enough not to assume an HPC module system has caught up to
3.11+.

**CLI entry points.** Stdlib only, no CLI framework (click/typer) yet.
Each entry point is a plain function in `<component>/cli.py:main` that
prints its own name/version and exits. Adding a framework is deferred to
whichever later ticket first needs real argument parsing — avoids a
dependency with no current use.

**License.** MIT. New `LICENSE` file (copyright "Varun Gambhir") plus a
`license` field in `pyproject.toml`. This is a departure from the
initial recommendation (omit license metadata, matching ADR-0001's
"provisional until public release" stance on the project name) — the
operator chose to settle it now rather than leave it open.

**Testing and dev tooling.** Explicitly deferred. No `tests/` scaffold,
no `pytest` dev-dependency, no ruff/mypy config in this ticket. The
issue's own acceptance criteria (`pip install -e .` succeeds, each entry
point runs) are the verification bar and are checkable by hand. Testing
infrastructure lands with whichever ticket first adds real behavior to
test — adding it now would be scope creep against the issue as written.

**Shared/common code.** No `mycelium.common` (or similar) subpackage yet.
Nothing currently needs to be shared between node/coordinator/client;
adding a shared spot before there's real code to put in it would be
speculative.

## Explicitly out of scope for this ticket

Any functional behavior in node/coordinator/client beyond a stub that
prints its name/version. CLI argument parsing. Populated dependency
extras (vLLM, Ray, HTTP framework, etc.) — those land with the tickets
that actually need them. Tests. Lint/type-check tooling. PyPI
publishing or package-name availability checks (ADR-0001 already flags
this as unresolved and out of scope until a public release is being
considered).
