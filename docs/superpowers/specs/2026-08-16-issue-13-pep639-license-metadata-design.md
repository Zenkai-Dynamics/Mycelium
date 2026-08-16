# Issue #13 — Migrate `pyproject.toml` License Metadata to PEP 639 — Design

Date: 2026-08-16
Status: Live-verified, gate resolved — migration approved to land
Issue: [#13 — Migrate pyproject.toml license metadata to PEP 639](https://github.com/Zenkai-Dynamics/Mycelium/issues/13)

This is the condensed record of the decisions made while brainstorming
issue #13, before implementation starts. It exists so the *reasoning*
behind the verification approach isn't lost, per the pattern established
in [the Phase 0 design doc](2026-08-12-mycelium-phase0-design.md).

## What issue #13 asks for

`pyproject.toml` currently declares license metadata using the classic,
now-deprecated setuptools form (`license = {file = "LICENSE"}` plus a
`License :: OSI Approved :: MIT License` classifier). The issue asks for
a migration to PEP 639 syntax (`license = "MIT"`, `license-files =
["LICENSE"]`, classifier removed, `requires = ["setuptools>=77"]`) —
required before setuptools makes the classic form a hard error
(2027-02-18).

The issue itself flags this as **not blocking Phase 0**, and gates the
migration on one condition: *"once the real HPC node's setuptools
version is confirmed and known to support it."* That condition was
chosen deliberately at issue #1 time — the classic syntax was picked
specifically for HPC-friendliness, since `pip install -e .`'s build
isolation fetches whatever setuptools version satisfies `requires`, and
Phase 0's candidate nodes (CDAC/IUAC HPC, VPN-gated lab machines) may
have restricted internet egress that build isolation depends on.

That gating fact — what actually happens when `pip install -e .` runs
with PEP 639 syntax on a real candidate node — is still unconfirmed
anywhere in the repo. `phase-0-foundation.md` already tracks the
adjacent, broader question ("Outbound egress from node environments is
unverified") as an open risk; this issue's narrower question (does *this
specific* metadata change build successfully) needs its own answer
before the edit lands.

## Decisions made

**Resolve the gate now, not later.** Rather than waiting for the
2027-02-18 deadline or for the broader egress-verification work to land
independently, live-test the migration against all three known Phase 0
candidate nodes now and apply it immediately if confirmed safe. Matches
the live-verification bar the rest of Phase 0's issues have used (#4,
#6, #8, #9) rather than reasoning about setuptools/pip behavior in the
abstract.

**Test against all three candidate nodes (`a6000`, `h100`,
`paramrudra`).** Not just one — `pyproject.toml` is shared across the
whole node pool, so any candidate node's build environment can gate the
change. Matches the bar issue #4's reachability investigation used
(checked every candidate, not a single representative).

**Verification method: actually run `pip install -e .`, not just
inspect versions.** Stage a checkout with the PEP 639 changes already
applied to a scratch directory on each node and run `pip install -e .`
in a fresh venv (default build isolation — the real path an operator
would use), verbose enough to show which setuptools version isolation
actually resolves. A cheaper "check preinstalled setuptools version and
PyPI reachability" approach was considered and rejected: build isolation
means the *preinstalled* setuptools version is not necessarily what gets
used, so only an actual install attempt gives a real answer, matching
how #6 caught the `nvidia-cuda-nvcc` drift that an import-only check
would have missed.

**`paramrudra`: login node only.** `paramrudra` has a separate HPC
compute node behind its login node, reachable only via job submission,
that ADR-0002 already flags as likely having *more* restricted egress
than the login node. Chasing the compute node here would fold the
already-tracked, broader "outbound egress unverified" open risk into
what is otherwise a small packaging-metadata cleanup. Out of scope for
this issue; the login node (`ssh paramrudra`) is the bound of this
verification.

**Decision rule: unanimous pass required.** If `pip install -e .`
succeeds with the new syntax on all three nodes, apply the four changes
from the issue's checklist exactly as specified and close #13. If it
fails on even one node, leave `pyproject.toml` unchanged (old syntax
stays), record which node(s) failed and why, and leave #13 open — a
single failing candidate node is disqualifying since the file is shared
across the whole pool.

**No update to `phase-0-foundation.md` or `dependencies.md`.** Issue #13
is not one of Phase 0's tracked success criteria or open risks (it
surfaced during issue #1's review as a standalone packaging-hygiene
item, explicitly marked "not blocking"), and license metadata isn't a
dependency pin. Neither doc's tracked list is the right home for this
decision record; this design doc is.

**No new automated test.** No CI workflow or packaging test currently
exercises `pip install -e .` (confirmed: `tests/` has no such coverage,
`.github/workflows/` doesn't exist) — consistent with issue #1's original
decision to keep packaging verification manual/live-checked rather than
add CI machinery ahead of need. This issue doesn't change that.

## Live verification (2026-08-16)

A minimal checkout (`pyproject.toml` with the four PEP 639 changes
already applied, `LICENSE`, `Readme.md`, and `src/mycelium/__init__.py`
— enough for `[tool.setuptools.packages.find]`/`[tool.setuptools.dynamic]`
to resolve, without the rest of the source tree) was copied to each of
the three candidate nodes and installed with `uv venv` + `uv pip install
-e .` — the primary install path `docs/SETUP.md` documents. (`SETUP.md`
also documents a plain-`pip` fallback for users without `uv`; see the
"Bare `pip install -e .`" verification below for that path.)

| Node | `uv` location | Python (via `uv venv`) | Result |
|---|---|---|---|
| `a6000` | `~/.local/bin/uv` (not on default non-interactive `PATH`) | 3.11.15 | `uv pip install -e .` — **succeeded**, exit 0 |
| `h100` | `~/.local/bin/uv` (not on default non-interactive `PATH`) | 3.11.14 | `uv pip install -e .` — **succeeded**, exit 0 |
| `paramrudra` (login node) | `/scratch/ldls-iiitd/.local/bin/uv`, already on `PATH` | 3.12.12 | `uv pip install -e .` — **succeeded**, exit 0 |

All three built and installed the editable package cleanly (`Installed 2
packages`: `mycelium==0.1.0`, `websockets==17.0.1`) with no PEP
639/setuptools-version errors — build isolation resolved a
`setuptools>=77`-satisfying version and built `license = "MIT"` /
`license-files = ["LICENSE"]` without complaint on every node, including
`paramrudra`'s login node, the one this issue's own wording specifically
worried about ("CDAC HPC environment"). Verbose (`-v`) output was
captured throughout; none of the three logs show any setuptools
resolution failure or fallback.

**Bare `pip install -e .` was also tested on `a6000` and `h100`**
(2026-08-16, added after final review flagged the gap below), using a
real `pip` binary — `uv venv --seed` to create the venv (working around
the same missing-`ensurepip` issue `docs/dependencies.md` already
documents for these nodes), then the venv's own `bin/pip install -e .`,
not `uv pip`. Both succeeded, exit 0, and the verbose log shows build
isolation resolving `setuptools>=77` for real: `a6000` — `Collecting
setuptools>=77` → `Using cached setuptools-84.0.0-py3-none-any.whl`;
`h100` — `Collecting setuptools>=77` → `Downloading
setuptools-84.0.0-py3-none-any.whl` (a genuine, non-cached fetch,
confirming build isolation actually reached
`https://files.pythonhosted.org` over the network on that node, not
just a local cache). Both installs finished `Successfully installed
mycelium-0.1.0 websockets-17.0.1`.

**Note on method:** an earlier version of this doc claimed
`docs/SETUP.md` "rejected" bare `pip`/`python3 -m venv` and that testing
it would be pointless — that was wrong. `docs/SETUP.md` documents plain
`pip` as a **supported fallback** for exactly this scenario ("No `uv`?
Plain `pip` works too: `python3 -m venv .venv` /
`.venv/bin/pip install -e .`"), and it's the literal path issue #13's
own wording named. The bare-`pip` check above closes that gap on
`a6000`/`h100`. `paramrudra`'s ancient system `pip` (9.0.3, under system
Python 3.6.8) still was not tested directly — it already can't satisfy
the project's `requires-python = ">=3.11"` floor regardless of license
syntax, a pre-existing, unrelated constraint `docs/SETUP.md` already
documents, not something this migration changes; the `uv`-provisioned
venv (real Python 3.12.12) is what a `paramrudra` user must use either
way, and that path is verified above.

**Scratch dirs (`~/mycelium-pep639-check`, `~/mycelium-barepip-check` on
each node, and the local staging directories) were removed after each
check** — these were disposable verifications, not permanent artifacts.

**Conclusion:** the gate issue #13 named is resolved — all three
candidate nodes build the PEP 639 syntax successfully via the documented
install path. The migration is approved to land as specified.

## Explicitly out of scope for this ticket

Resolving the broader "outbound egress from node environments is
unverified" open risk (that's tracked separately in
`phase-0-foundation.md` and gates the dial-out transport, not this
metadata change). Testing on `paramrudra`'s HPC compute node. Any other
`pyproject.toml` changes beyond the four the issue specifies. Adding CI
or automated packaging tests.
