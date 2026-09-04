# 4. The client library never carries context forward implicitly

## Status

Accepted — 2026-09-04. Introduced by Phase 2 (multi-LLM agentic flow).

## Context

Phase 1 disclosed that a volunteer node necessarily reads the plaintext it
serves, and concluded that hiding it is not technically feasible. Phase 2
introduces a *different* problem, which is feasible to address: **aggregation**.
As a flow progresses, each successive node can see not just its own hop's
input but the original task and every prior model's output — so the exposure
per volunteer grows with the length of the flow, even though no single hop
discloses more than Phase 1 already did.

Conventional agent libraries accumulate conversation state and resend it on
every call. That is convenient, and in a single-vendor setting it is
harmless. Here it means every node in a flow sees everything that came before
it.

Hardware mitigations were investigated and rejected as unavailable rather than
undesirable: GPU confidential computing (TEE) exists only on Hopper H100 and
newer — the Ampere A6000s this project actually runs on cannot do it at all —
and additionally requires SEV-SNP/TDX CPUs, a specific virtualization stack,
and an attestation service. A mechanism that excludes every non-Hopper
volunteer does not fit a network built on donated consumer hardware. FHE is
many orders of magnitude too slow for inference.

## Decision

**The client library records the full flow locally, but sends only what each
call explicitly names. There is no implicit accumulator.** An agent that wants
a previous hop's output in a later hop must pass it deliberately.

```python
draft  = await flow.call("small-model", messages=[user(task)])
review = await flow.call("big-model",
                         messages=[user(critique), assistant(draft.text)])
# the second hop sees exactly those two messages — not `task`, not anything else
```

The `Flow` also reports, per node and per identity, what that node actually
received during the flow — so the property can be checked rather than trusted.

## Consequences

- The privacy property is **structural, not a setting**: the library cannot
  leak accumulated context, because it has no path that sends unnamed content.
  A default that had to be opted into would in practice be left off.
- Agent authors must be deliberate about what each hop needs. Where an agent
  assumed accumulation, it fails loudly and early rather than over-sharing
  silently — the preferable failure direction.
- This does **not** address Phase 1's disclosure: the node serving a hop still
  reads that hop in plaintext. Nothing here should be described as making
  inference private. It narrows *how much* each volunteer sees, not *whether*
  they see it.
- Adding implicit accumulation later would be both a breaking API change and a
  privacy regression, which is the reason this is recorded rather than left as
  a convention.
