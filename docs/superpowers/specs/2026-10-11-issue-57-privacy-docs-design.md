# Issue #57 — Document Phase 2's Privacy Mechanism and Its Limits — Design

Date: 2026-10-11
Status: Approved and implemented (docs/OPERATIONS.md)
Issue: [#57 — Document Phase 2's privacy mechanism and its limits](https://github.com/Zenkai-Dynamics/Mycelium/issues/57)
Parent: [#54 — Phase 2: Multi-LLM agentic flow](https://github.com/Zenkai-Dynamics/Mycelium/issues/54)
Related: [ADR-0004](../../adr/0004-explicit-per-hop-context.md) (the
aggregation decision this documents), [issue #63's design](2026-09-05-issue-63-exposure-handles-design.md)
(exposure handles), [issue #68's design](2026-10-10-issue-68-operator-token-design.md)
(the operator-only identity view this must not be confused with)

This is the condensed record of the decisions made while brainstorming
issue #57, before writing the actual doc change. Docs-only ticket, no
code — this doc's "implementation" *is* the content decided below, so no
separate implementation plan follows this one, matching issue #38's
precedent for a docs-only ticket of this size.

## What issue #57 asks for

Phase 2 introduces a privacy risk Phase 1 didn't have — aggregation: each
successive node in a flow could otherwise see the original task plus every
prior model's output, so exposure per volunteer grows with flow length
even though no single hop reveals more than Phase 1 already did. The issue
asks for this disclosed plainly, distinct from Phase 1's single-hop
disclosure; the explicit-per-hop-context mechanism (ADR-0004) explained,
including that it's structural rather than an optional setting; the
per-node exposure report documented as how a user checks what a node
actually received; a blunt, unhedged statement that none of this makes
inference private; and the hardware-TEE investigation recorded as
unavailable rather than overlooked. No code changes.

## Decisions made

**Primary location: extend `docs/OPERATIONS.md`'s existing "What this
network does not protect against" section. No change to
`docs/phases/phase-2-multi-llm-agentic.md`.** The issue names this section
directly as "the natural home," and it already carries the right blunt,
factual tone (two existing bullets: plaintext exposure, no output
verification). The phase doc's "What's decided" bullet on privacy already
summarizes this correctly at design-rationale depth — restating it there
too would duplicate prose that drifts out of sync, the same reasoning
issue #38's design doc gave for not touching the Phase 1 doc's bullets.
Considered adding a one-sentence cross-reference from the phase doc's
privacy bullet to the new section anyway (the way #38 extended Phase 1's
doc with a pointer) and rejected it: unlike #38, where the phase doc had
no pointer to `OPERATIONS.md` at all, Phase 2's doc already closes its
"What's decided" list with a general pointer to `OPERATIONS.md`'s
documentation map — a second, narrower pointer would be redundant rather
than filling a gap.

**Two new bullets, inserted immediately after the existing "A volunteer
node sees your prompt in plaintext" bullet, before "Mycelium does not
verify a node's output is correct."** The aggregation risk is a superset
of the plaintext-exposure risk (it only makes sense once a reader already
knows a single hop is visible in plaintext), so it belongs directly after
that bullet and before the unrelated output-verification topic — matching
this section's existing bullet-per-topic granularity rather than
compressing everything into one paragraph or splitting into more than two.

**Bullet 1 (aggregation + mechanism + no-privacy claim) covers four of the
five acceptance criteria in one bullet, because they're one argument, not
four facts:** the risk is stated distinct from Phase 1's ("Phase 1's risk
is per-hop... Phase 2 adds a different risk — aggregation"); the mechanism
is explained as structural, not a setting ("never carries context forward
implicitly... not as a setting you must remember to turn on"); the TEE
investigation is recorded as unavailable rather than rejected-as-
undesirable, with the specific hardware reason (Hopper H100+ required,
Ampere cards here cannot do it); and the bullet ends on the unhedged claim
the issue asks for verbatim in spirit — "none of this makes inference
private... it narrows how much each volunteer sees, not whether they see
it" — deliberately echoing ADR-0004's own Consequences section so the
user-facing doc and the internal decision record make the same claim in
the same words, rather than risking a weaker or stronger claim creeping in
through independent paraphrasing.

**Bullet 2 (exposure report) is kept separate and leads with what it does
*not* give you.** Grilling issue #68 immediately before this one surfaced
the risk precisely: `status_query` (operator-only, #68) resolves real
identities, while `Flow.exposure()` (client-facing, #63) hands back
16-character HMAC handles that cannot be resolved back to a node or GitHub
login, by design. Documenting the exposure report without naming that
contrast would let a reader assume "I can check what each node received"
means "I can find out who they are" — exactly the overclaiming the issue
warns against. The bullet names both `Flow.exposure()` and the reference
agent's printed report (`examples/draft_then_critique.py`) as the two ways
a user actually exercises this today, rather than describing the
mechanism in the abstract with no pointer to runnable code.

**Format: same bold-led-paragraph convention as the section's existing two
bullets.** No new callout/admonition style — same reasoning as issue #38's
design doc: this doc has no existing precedent for blockquotes or emoji
warning markers, and two more paragraphs don't justify introducing one.

**Process scope: no separate implementation plan**, for the same reason
issue #38 had none — pure documentation, fully concrete and already
drafted above, small enough that a task-by-task plan would add process
without adding clarity. Proceeding straight from this design doc to
editing `docs/OPERATIONS.md`.

## Explicitly out of scope for this issue

Any code change (the issue's acceptance criteria require none, and nothing
drafted above needs one — `Flow.exposure()` and the handle mechanism
already exist from #59/#63). Any change to
`docs/phases/phase-2-multi-llm-agentic.md` (see Decisions above — its
existing bullet and general pointer are left as written). Any change to
`README.md` — stays overview-level per its own "Note to reviewer,"
unaffected by a phase-level documentation ticket. Resolving or restating
issue #68's decision — this doc only needs to *describe* the resulting
contrast between the two disclosure mechanisms accurately, not re-argue
why they're shaped that way.

## Amendment (2026-10-18)

Before shipping, a re-read against #68's fourth acceptance criterion found
bullet 2's "a client checking its own flow's exposure cannot" overclaimed:
under #68's decision the only client *is* the operator, whose token can
list every identity via `status_query`, and a model served by a single
node makes a handle trivially attributable. Bullet 2 now says so, and
scopes the handle's protection to completion replies, and to a future
non-operator client — #68's revisit trigger.
