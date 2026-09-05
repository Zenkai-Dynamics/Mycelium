# Mycelium

Running LLM inference across GPUs volunteered by geographically separated
people. A client sends a prompt to one stable address; a coordinator routes
it to a volunteer's machine that hosts the model; the completion comes back.

This glossary exists because several of these words are overloaded — "context"
and "client" especially mean different things in ordinary LLM usage than they
mean here, and Phase 2 (multi-model agentic flows) makes the ambiguity
load-bearing rather than merely untidy.

## Language

### The three running programs

**Coordinator**:
The single publicly-reachable process that holds the registry of connected
nodes and picks one for each incoming request. The system's only trust anchor.
Stateless with respect to conversations — it relays, it does not remember.

**Node**:
One volunteer's participating machine, running the node agent and exactly one
local model server. Identified on the wire by its own self-generated public
key, never by hostname or address.
_Avoid_: host, worker, peer, server

**Client**:
A program that submits requests to the coordinator. Covers both the shipped
`mycelium-client` CLI and any program written against the client library.
_Avoid_: user, consumer

### Models and routing

**Model**:
The exact model string a node serves and a request asks for (e.g.
`Qwen/Qwen2.5-7B-Instruct`). Matched exactly — never by family, size, or
capability.
_Avoid_: LLM, engine, backend

**Routing**:
The coordinator's choice of which node serves one request, among the nodes
serving the requested model. Softly biased by reputation, never a hard
exclusion.

**Reputation**:
Per-node counters of completions, timeouts, crashes and disconnects, used to
softly weight routing. Reflects a node's protocol-level reliability only —
never the correctness or quality of what it produced.

**Identity**:
The GitHub account bound to a node's public key on first registration. The
unit that per-identity caps and operator bans apply to. Distinct from the
node itself: one identity may run several nodes.

### Phase 2 — multi-model flows

**Context**:
The accumulated conversation a client carries across a flow, sent as a
`messages` array. Deliberately NOT the model's context window — say "context
window" in full whenever that is what is meant.
_Avoid_: state, history, memory, conversation

**Context window**:
The token limit of a particular model. A property of the model, unrelated to
who holds the context.

**Hop**:
One request/response exchange between a client and one node, through the
coordinator. A flow is a sequence of hops.

**Flow**:
One logical task spanning several hops, typically across different models.
The unit a reference agent implements and the reason Phase 2 exists.
_Avoid_: chain, pipeline, workflow, agentic flow

**Agent**:
A program that drives a flow — chooses the model for each hop and decides what
context to carry forward. Agents are written by users against the client
library; Mycelium ships exactly one, as a demonstration, not as a framework.

**Orchestration**:
Deciding the order of hops and what context each carries. In Mycelium this
happens client-side (see ADR-0003), never on the coordinator or a node.

**Flow record**:
The client's own local history of a flow — every hop, what was sent, what came
back, and which node served it. Local to the client and never transmitted;
distinct from the context, which is what a given hop actually sends.

**Exposure**:
How much of a flow one node, or one identity, actually received. The quantity
Phase 2's explicit-context rule (ADR-0004) exists to minimize, and which the
client can report per flow. Not the same as disclosure — a serving node always
reads its own hop.

**Handle**:
An opaque identifier for a node or a bound identity, returned with each
completion so a client can tell which hops one volunteer served. Derived
under a secret the coordinator generates at startup, so a client can group
by it but never resolve it to a public key, fingerprint or GitHub login.
Not stable across a coordinator restart, and not the same thing as a
fingerprint — which identifies a node *to the operator*.
_Avoid_: id, fingerprint, token

**Fault**:
Whose mistake a failure was: the client's (a malformed request, context
exceeding the model's window) or the node's (a crash, a timeout, a model it
isn't serving). Deliberately not the same as *who failed* — a node reports a
client fault for a request it served correctly and the model refused. Never a
judgement about the quality of a completion, which nothing in Mycelium
measures.
_Avoid_: error, blame, cause
