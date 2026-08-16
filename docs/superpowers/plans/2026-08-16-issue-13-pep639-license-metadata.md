# Issue #13 — Migrate `pyproject.toml` License Metadata to PEP 639 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the four PEP 639 license-metadata changes issue #13 specifies to `pyproject.toml`, now that the gate it named (real-node setuptools support) is confirmed.

**Architecture:** This is a metadata-only change to a single file. The substantive work for issue #13 — confirming PEP 639 syntax actually builds on all three Phase 0 candidate nodes — is **already complete**, performed directly against real infrastructure (not delegated to a subagent, for the same reason issue #4's and #6's live verification steps weren't: it required live SSH access to real HPC/lab nodes and judgment about which install path to test). That work is fully recorded in `docs/superpowers/specs/2026-08-16-issue-13-pep639-license-metadata-design.md` (design rationale + the "Live verification" section with the real per-node results). This plan's one task transcribes the now-settled decision into the last remaining place: `pyproject.toml` itself.

**Tech Stack:** TOML edit only. No code, no new dependencies.

## Global Constraints

- Exact current `pyproject.toml` content being replaced (verified present at the time this plan was written — confirm it's still there before editing, in case something else changed it first):
  - `license = {file = "LICENSE"}` → becomes `license = "MIT"` plus a new `license-files = ["LICENSE"]` line
  - Classifier `"License :: OSI Approved :: MIT License",` (inside `classifiers = [...]`) → removed entirely
  - `requires = ["setuptools>=61"]` (in `[build-system]`) → becomes `requires = ["setuptools>=77"]`
- Do not touch anything else in `pyproject.toml` — `[project.optional-dependencies]`, `[project.scripts]`, `[tool.setuptools.*]`, `[tool.uv]`, `[dependency-groups]`, `[tool.pytest.ini_options]` all stay exactly as they are.
- Do not modify `docs/phases/phase-0-foundation.md` or `docs/dependencies.md` — per the design doc, issue #13 isn't one of Phase 0's tracked success criteria/open risks, and license metadata isn't a dependency pin. This design doc is the only decision record for this change.
- Do not add any new test or CI workflow — none exists for packaging today (confirmed in the design doc), and this issue doesn't change that.

---

### Task 1: Migrate license metadata in `pyproject.toml`

**Files:**
- Modify: `pyproject.toml` (`[build-system]` table and `[project]` table)

**Interfaces:**
- Consumes: nothing from other tasks — this is the plan's only task.
- Produces: nothing consumed elsewhere — last task in the plan.

- [ ] **Step 1: Confirm the current text is still present (red)**

Run:

```bash
grep -n 'license = {file = "LICENSE"}' pyproject.toml
grep -n 'License :: OSI Approved :: MIT License' pyproject.toml
grep -n 'requires = \["setuptools>=61"\]' pyproject.toml
```

Expected: one matching line each — the file hasn't been migrated yet.

- [ ] **Step 2: Bump the setuptools floor in `[build-system]`**

Find:

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"
```

Replace with:

```toml
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"
```

- [ ] **Step 3: Replace the `license` field and add `license-files`**

Find (in `[project]`):

```toml
license = {file = "LICENSE"}
```

Replace with:

```toml
license = "MIT"
license-files = ["LICENSE"]
```

- [ ] **Step 4: Remove the MIT classifier**

Find:

```toml
classifiers = [
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.11",
]
```

Replace with:

```toml
classifiers = [
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.11",
]
```

- [ ] **Step 5: Verify the old text is gone (green)**

Run:

```bash
grep -n 'license = {file = "LICENSE"}' pyproject.toml
grep -n 'License :: OSI Approved :: MIT License' pyproject.toml
grep -n 'requires = \["setuptools>=61"\]' pyproject.toml
```

Expected: no output for any of the three — all old text is gone.

```bash
grep -n 'license = "MIT"' pyproject.toml
grep -n 'license-files = \["LICENSE"\]' pyproject.toml
grep -n 'requires = \["setuptools>=77"\]' pyproject.toml
```

Expected: one matching line each — the new text is in place.

- [ ] **Step 6: Confirm the package still installs locally (green)**

Run:

```bash
uv venv /tmp/mycelium-pep639-local-check
uv pip install -e . --python /tmp/mycelium-pep639-local-check/bin/python
```

Expected: `Installed N packages` including `mycelium==0.1.0`, no errors — mirrors the real-node results already recorded in the design doc, confirming the local dev machine (also on the `setuptools>=77`-satisfying build isolation path) builds cleanly too.

Then clean up: `rm -rf /tmp/mycelium-pep639-local-check`

- [ ] **Step 7: Read the whole file once, end to end**

Run: `cat pyproject.toml`

Confirm: the document reads coherently — `[project.optional-dependencies]`, `[project.scripts]`, `[tool.setuptools.dynamic]`, `[tool.setuptools.packages.find]`, `[tool.uv]`, `[dependency-groups]`, and `[tool.pytest.ini_options]` are all present and byte-for-byte unchanged from before this task.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml
git commit -m "build: migrate license metadata to PEP 639 (issue #13)"
```
