# Mycelium — Operations Guide

Related: [Developer setup](SETUP.md) · [Phase 0 — Foundation](phases/phase-0-foundation.md) · [ADR-0002 — node transport model](adr/0002-node-transport-model.md)

This walks through actually **running** Mycelium end to end: standing up
a coordinator, connecting a node to it, and sending a completion through
as a client. It assumes you've already followed
[SETUP.md](SETUP.md) to get `mycelium-coordinator`, `mycelium-node`,
`mycelium-coordinator-status`, and `mycelium-client` installed.

If you just want to confirm a GPU node's vLLM stack works at all,
without any coordinator involved, skip to
["Just testing vLLM on a node, no coordinator"](#just-testing-vllm-on-a-node-no-coordinator).

## How the pieces fit together

```
Client  →  Coordinator  →  Node agent  →  vLLM  →  response
                ↑
        (node dials out and
         holds this connection open)
```

- **Coordinator** — the one stable, publicly-reachable address. Holds an
  in-memory registry of currently-connected nodes and picks a healthy one
  for each client request.
- **Node agent** — runs on a GPU machine. Starts and manages a local
  `vllm serve` process, then **dials out** to the coordinator and holds
  that connection open (Phase 0's nodes sit behind VPNs/HPC firewalls
  with no inbound reachability — see
  [ADR-0002](adr/0002-node-transport-model.md) — so the coordinator never
  connects *to* a node).
- **Client** — a one-shot request: connect, send one prompt, get one
  completion back, exit.

Every connection is TLS, authenticated by one shared secret token (same
token used by every node and every client) plus a self-signed
certificate the coordinator generates once and that every node/client
must have a local copy of. There's no CA — a copy of the coordinator's
own cert file *is* the trust anchor (see
["The trust model in one paragraph"](#the-trust-model-in-one-paragraph)
below).

## Step 1 — Create a shared token

Anyone connecting — every node, every client — authenticates with the
same secret token, compared with `hmac.compare_digest` (not sent as a
CLI flag or environment variable; always a file). Generate one and put
it somewhere only you can read:

```bash
mkdir -p ~/.mycelium
openssl rand -hex 32 > ~/.mycelium/token
chmod 600 ~/.mycelium/token
```

Copy this same file (or its contents) to every node and every client
machine — `scp ~/.mycelium/token <node-host>:~/.mycelium/token`, etc.
Anyone who has it can register a node or submit completions, so treat it
like a password.

## Step 2 — Start the coordinator

Run this on whichever machine is your stable, reachable address (a cloud
VM, a machine with a public IP, or anything your nodes and clients can
all reach):

```bash
mycelium-coordinator --token-file ~/.mycelium/token --cert-san-ip <coordinator-ip>
```

- `<coordinator-ip>` — the IP address nodes/clients will actually connect
  to (e.g. a public IP, or `127.0.0.1` if everything runs on one machine
  for local testing). It's embedded in the auto-generated certificate's
  Subject Alternative Name, and is **only required the first time** —
  once `~/.mycelium/coordinator-cert.pem` / `coordinator-key.pem` exist,
  later runs reuse them and you can drop the flag.
- Default listen address: `0.0.0.0:8765`. Override with `--host`/`--port`.
- Default cert/key paths: `~/.mycelium/coordinator-cert.pem` /
  `coordinator-key.pem`. Override with `--cert-file`/`--key-file`.

On success you'll see:

```
mycelium-coordinator 0.1.0 listening on 0.0.0.0:8765
```

It runs in the foreground until killed (`Ctrl-C` or `SIGTERM`) — run it
under `tmux`/`systemd`/`nohup` for anything long-lived.

**Copy the generated cert to every node and client**, they need it to
verify who they're talking to:

```bash
scp ~/.mycelium/coordinator-cert.pem <node-host>:~/.mycelium/coordinator-cert.pem
```

(Not the key file — that stays only on the coordinator.)

## Step 3 — Start a node

On a GPU machine, with the `node` extra installed (see
[SETUP.md](SETUP.md)'s Node/GPU setup section) and both the token file
and the coordinator's cert copied over:

```bash
mycelium-node \
  --coordinator-url wss://<coordinator-ip>:8765 \
  --coordinator-cert ~/.mycelium/coordinator-cert.pem \
  --token-file ~/.mycelium/token
```

The node agent shells out to a bare `vllm` command (not a path inside
its own venv) — make sure the venv's `bin/` directory is on `PATH`
before running this, e.g. `source .venv/bin/activate` first, or
`PATH="$PWD/.venv/bin:$PATH" mycelium-node ...`. Running the entry point
by its full path (`.venv/bin/mycelium-node ...`) without also exporting
`PATH` fails immediately with `FileNotFoundError: ... 'vllm'` — confirmed
live on a real node.

What happens:

1. Starts `vllm serve` locally (`CUDA_VISIBLE_DEVICES=0` by default —
   override with `--gpu`; `Qwen/Qwen2.5-7B-Instruct` by default —
   override with `--model`; listens on `127.0.0.1:8811` by default —
   override with `--vllm-port`) and waits for it to report healthy
   (up to 5 minutes on first run, while weights download/load).
2. Dials out to `--coordinator-url` over TLS, verifying the coordinator
   against the pinned `--coordinator-cert`.
3. Sends a registration message (token + model + node ID) and waits for
   the coordinator to ack it.
4. Holds the connection open, handling completion requests the
   coordinator routes to it, until the connection drops — then
   reconnects automatically with exponential backoff (1s, doubling,
   capped at 30s, ±20% jitter) and re-registers.

On success:

```
starting vLLM (Qwen/Qwen2.5-7B-Instruct on GPU 0)...
vLLM ready
mycelium-node 0.1.0 connecting to wss://<coordinator-ip>:8765
connected to coordinator (wss://<coordinator-ip>:8765)
registered with coordinator as 'your-hostname'
```

Node ID defaults to the machine's hostname — set one explicitly with
`--node-id` if you're running multiple nodes on the same box or want a
more memorable name. Registering a second time under the same node ID
(e.g. after a restart) silently replaces the previous connection under
that ID rather than erroring.

**Running more than one node on the same physical machine** (one GPU
each): give each a distinct `--vllm-port` too, not just a distinct
`--node-id`/`--gpu`. `--vllm-port` defaults to 8811 for every node — two
co-located nodes sharing it means the second node's own `vllm serve`
fails to bind (`Address already in use`), but its readiness check polls
the fixed `127.0.0.1:<port>/health` rather than confirming the response
came from its own process, so it sees the *first* node's healthy
response, prints `vLLM ready`, and registers anyway — silently pointing
at the wrong node's engine. Confirmed live; not caught or warned about
anywhere today.

`SIGTERM`/`SIGHUP`/`Ctrl-C` all stop `vllm serve` cleanly (process-group
kill, no orphaned GPU processes) before the node agent exits.
**`kill -9` does not** — a killed process can't run its own cleanup
code, so `vllm serve` (and its `EngineCore` subprocess) is orphaned,
still holding the GPU. Confirmed live, twice. If you have to hard-kill a
node, check `nvidia-smi` afterward and clean up manually:
`ps -o pid,ppid,pgid -p <vllm-serve-pid>` to confirm it's its own
process-group leader, then `kill -9 -<that-pgid>` (the leading `-`
targets the whole group).

## Step 4 — Check what's registered

From any machine with the coordinator's cert and the token:

```bash
mycelium-coordinator-status \
  --coordinator-url wss://<coordinator-ip>:8765 \
  --coordinator-cert ~/.mycelium/coordinator-cert.pem \
  --token-file ~/.mycelium/token
```

```
your-hostname: Qwen/Qwen2.5-7B-Instruct
```

(or `No nodes registered.` if none are currently connected).

## Step 5 — Send a completion as a client

```bash
mycelium-client \
  --coordinator-url wss://<coordinator-ip>:8765 \
  --coordinator-cert ~/.mycelium/coordinator-cert.pem \
  --token-file ~/.mycelium/token \
  --model Qwen/Qwen2.5-7B-Instruct \
  --prompt "What is the capital of France? Answer in one word."
```

Prints the completion text and exits 0, or prints `error: <reason>` and
exits 1. Reasons you'll actually see:

| Reason | Meaning |
|---|---|
| `no healthy node for model '<model>'` | No node currently registered is hosting that model. Fails immediately — no retry, no queue. |
| `node '<node-id>' did not respond within 130.0s` | The node accepted the request but never replied (not retried — it might still be running the prompt). |
| *(the node's own error text, unprefixed — e.g. an HTTP error from vLLM)* | The node itself explicitly reported the completion failed; its exception text is passed through as-is. |
| `coordinator did not respond within 140.0s` | The coordinator itself didn't reply — check it's still running. |
| `coordinator closed the connection without responding (check --token-file)` | Almost always a bad or missing token. |

If the node that was about to handle your request turns out to be
disconnected, the coordinator silently retries a different healthy node
before giving up — you'll never see that as a client-visible error as
long as another healthy node for the same model exists.

## Troubleshooting

**`ValueError: Free memory on device cuda:<N> (X/Y GiB) on startup is
less than desired GPU memory utilization (0.92, Z GiB)`** — vLLM (via
`vllm serve`, which the node agent starts) defaults to reserving 92% of
the GPU's total memory, assuming exclusive access. On a shared HPC/lab
GPU with other jobs already resident, that reservation can exceed what's
actually free even though the GPU isn't fully idle — encountered live on
a real, in-use A6000 during this doc's own verification. `--gpu` picks
*which* GPU to use, but there's currently no `mycelium-node` flag to
lower vLLM's memory-utilization target — check `nvidia-smi` on the node
first and pick (or wait for) a GPU with enough headroom for this
model's ~15 GB weights plus KV cache.

**`FileNotFoundError: [Errno 2] No such file or directory: 'vllm'`** —
see the `PATH` note in Step 3 above.

## Running more than one node

Just repeat Step 3 on each GPU machine (each with its own `--node-id` if
they'd otherwise share a hostname), all pointed at the same coordinator.
The coordinator round-robins across every node registered for a given
model. Killing whichever node a request lands on triggers an automatic,
immediate failover to another healthy node hosting the same model — no
client-visible failure as long as one remains. Live-verified end to end
(two real nodes, a real coordinator on a separate host, a real client on
a fourth machine) — see issue #11's design doc for the full transcript.

Running more than one node on the *same* physical machine (one GPU
each)? See the `--vllm-port` caveat in Step 3 above — it's a real
footgun that silently cross-talks between co-located nodes if skipped.

## The trust model in one paragraph

There's no certificate authority in Phase 0. The coordinator generates
one self-signed cert/key pair on first run; every node and client is
handed a copy of the **public cert only** and loads it directly as its
sole trust anchor (`ssl.SSLContext.load_verify_locations`, with hostname
checking off, since Phase 0 nodes/clients connect by IP, not DNS name).
In effect: possessing that exact cert file *is* what it means to trust a
given coordinator. Treat the cert file with the same care as the token —
copy it out of band (`scp`, not email/Slack) — and never publish it
anywhere the whole internet could pick it up expecting it to still mean
something private, since anyone with a copy can pin to and successfully
validate that same coordinator.

## Just testing vLLM on a node, no coordinator

To confirm a GPU node's vLLM stack works in isolation, without
registering anywhere:

```bash
mycelium-node --prompt "What is the capital of France? Answer in one word."
```

Starts `vllm serve` locally, waits for it to be ready, runs that one
prompt through it, prints the result, and exits — no token, no
coordinator, no registration involved. Same `PATH` requirement as Step 3
above (the venv's `bin/` must be on `PATH`) and the same shared-GPU
caveat in [Troubleshooting](#troubleshooting) apply here too.
