# 1. Project name: Mycelium

## Status

Accepted — 2026-08-12.

## Context

The project needed a working name before the PRD could be written down.
No domain/trademark/namespace availability sweep was performed for this
decision (unlike Bailment's ADR-0001 in the sibling project) — if the name
needs to survive contact with a public launch, that check is still
outstanding.

The brief given: something in the same conceptual space as ChainOpera
(decentralized, community-powered AI compute) without resembling it
directly.

## Decision

The project is named **Mycelium** — the underground fungal network that
routes resources between trees across a forest, with no central trunk.

This fits the shape of the system: independently owned GPU nodes,
connected through a coordinator, growing from a small trusted pool
(Phase 0) into a public network (Phase 1) without a hierarchical
structure at the node level.

### Other candidates considered

| Candidate | Reason not chosen |
| --- | --- |
| Rhizome | Same decentralized-network metaphor, more academic/philosophical register, less immediately evocative. |
| Hivemind | Already the name of an existing BigScience library for decentralized DHT coordination (see Readme §8, Prior Art) — would collide conceptually and by name. |
| Compute Commons | Literal rather than metaphorical; dropped in favor of the nature metaphor. |

## Consequences

- The sibling project in this git organization, Bailment, has an unrelated
  and explicitly opposite thesis ("ship tasks, not tokens"). Mycelium and
  Bailment should not share architecture or be merged.
- No availability check (domain, GitHub org, package registries,
  trademark) has been done. Treat the name as provisional until that
  check happens, if this goes toward a public release.
