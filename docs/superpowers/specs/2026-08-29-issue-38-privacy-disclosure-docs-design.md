# Issue #38 — Prompt-Privacy and Content-Verification Disclosure Docs — Design

Date: 2026-08-29
Status: Approved, not yet implemented
Issue: [#38 — Prompt-privacy and content-verification disclosure docs](https://github.com/Zenkai-Dynamics/Mycelium/issues/38)
Parent: [#31 — Phase 1: Open node pool to public volunteers](https://github.com/Zenkai-Dynamics/Mycelium/issues/31) —
see [the Phase 1 design doc](2026-08-23-phase-1-open-network-design.md) for
the full phase-level rationale this ticket documents one slice of.

This is the condensed record of the decisions made while brainstorming
issue #38, before writing the actual doc changes. Docs-only ticket, no
code — this doc's "implementation" *is* the two content blocks decided
below, so no separate implementation plan follows this one (see the
Process-scope decision).

## What issue #38 asks for

Two accepted-risk properties of the Phase 1 open network are already
decided (see the design doc) but only documented as one-line bullets in
`docs/phases/phase-1-open-network.md`. Issue #38 asks for both to be
disclosed plainly somewhere an operator/client would actually read before
relying on the network: (1) a volunteer node necessarily sees the
plaintext prompt it serves, matching Folding@home's work-unit transparency
framing; (2) Phase 1 does not verify a node's output is correct — only
protocol-level health is checked, distinguishing "the node responded" from
"the response is trustworthy." No code changes.

## Decisions made

**Primary location: a new section in `docs/OPERATIONS.md`, not an
expansion of `docs/phases/phase-1-open-network.md`'s existing bullets.**
`OPERATIONS.md` is the doc a person actually reads while setting up and
running the system — the phase doc is a design record of *what was
decided and why*, read once when understanding the architecture, not
revisited "before relying on the network" the way the acceptance
criterion asks for. Writing the full disclosure twice (once per doc,
tailored to each audience) was considered and rejected: two copies of the
same warning drift out of sync over time, and this repo's established
pattern is one authoritative explanation plus a cross-reference, not
duplicated prose (see how `docs/phases/phase-1-open-network.md` already
points to the design doc and issue #31 rather than restating their
content).

**Section placement: immediately after the existing "## The trust model
in one paragraph" section, before "## Just testing vLLM on a node, no
coordinator."** That existing section explains *who* a node/client is
trusting (cert pinning, no CA); the new section explains *what that trust
does and doesn't cover* once established. Reading them back to back — who
you're trusting, then what happens once you do — is the natural order;
placing the new section earlier (e.g. before Step 1) would interrupt the
doc's setup flow with content that matters more once someone is deciding
whether to actually route real prompts through the network, which is
closer to where the trust-model section already sits.

**New section title: "What this network does not protect against."**
Direct and blunt, matching `OPERATIONS.md`'s existing voice (e.g. "**A
coordinator restart forgets every node's GitHub binding**", "**`kill -9`
does not**"). No euphemism ("privacy considerations," "known
limitations") — the acceptance criteria explicitly ask for plain
disclosure of two things that are true today and load-bearing for
someone's judgment about what to send through the network.

**Format: bold-led paragraphs, not a callout/admonition block.**
`OPERATIONS.md` already has an established convention for "important
caveat the reader must not miss" — a bold lead sentence opening a normal
paragraph (the two examples above, plus "**Running more than one node on
the same physical machine**," "**`mycelium-node`'s GitHub App `client_id`
ships as a placeholder**"). No blockquote, HTML `<div>`, or emoji warning
markers were introduced — this doc has no existing precedent for those,
and introducing new formatting for two paragraphs would be inconsistent
with everything around it. (`README.md` uses a `>` blockquote convention
for its own sidebar notes, but that is a different doc's local style, not
`OPERATIONS.md`'s.)

**Content, paragraph 1 (prompt privacy):** states plainly that a
volunteer node sees the prompt in plaintext because inference requires
it, names the Folding@home work-unit transparency comparison the
acceptance criterion explicitly asks for, and ends with the practical
takeaway ("don't send anything through Mycelium you wouldn't want a
volunteer operator to read") rather than stopping at the abstract fact —
matching how the acceptance criterion frames this as something a reader
needs before relying on the network, not just a fact for its own sake.

**Content, paragraph 2 (content verification):** states plainly that only
protocol-level health is checked, explicitly draws the "the node
responded" vs. "the response is trustworthy" distinction the acceptance
criterion names verbatim, and includes the *why* (LLM sampling is
stochastic, so redundant-computation-style validation doesn't map onto it
the way it does for deterministic BOINC/Folding@home-style compute) —
this repo's docs consistently explain reasoning, not just state
conclusions (e.g. Step 3's per-identity-cap and reputation-weighting
explanations), and omitting it here would read as an unexplained,
possibly-temporary gap rather than the deliberate, considered decision it
actually is.

**Cross-reference: extend the existing "see also" sentence at the bottom
of `docs/phases/phase-1-open-network.md`, don't touch the two bullets
themselves.** That doc already ends its "What's decided" list with one
sentence pointing to the design doc and issue #31 for further reading;
adding a third pointer to `OPERATIONS.md`'s new section (anchored to the
exact heading) to that same sentence is the smallest possible change that
prevents the two docs from silently drifting apart on explanation depth,
without restructuring or duplicating the bullets that already state the
decision tersely and correctly.

**Process scope: no separate implementation plan.** This ticket is pure
documentation with a fully concrete, small scope — one new section (two
paragraphs, already drafted and approved above) plus a one-sentence
cross-reference edit. A task-by-task implementation plan (the pattern
every code-bearing sub-issue in this phase used) would add process
overhead without adding clarity for a change this size, fully specified,
and easily reversible. Proceeding straight from this design doc to
writing the actual doc changes.

## Explicitly out of scope for this issue

Any code change (the issue's own acceptance criteria require none).
Any change to `README.md` (the PRD) — it explicitly stays overview-level
per its own "Note to reviewer," and already points readers to
`docs/OPERATIONS.md`/`docs/phases/` as the living sources of truth rather
than restating phase-specific detail itself. Rewriting or restructuring
`docs/phases/phase-1-open-network.md`'s existing two bullets — they
already state the decisions correctly and tersely; only a cross-reference
is added. Any new callout/admonition formatting convention — matches
`OPERATIONS.md`'s existing bold-lead-paragraph style exactly.
