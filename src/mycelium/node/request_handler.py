"""Handles inbound messages on a node's already-registered coordinator
connection: routed completion requests from the coordinator, forwarded to
the local vLLM process.

See the design doc for issue #10. registration.py owns exactly the
registration handshake; this module owns everything that comes after —
concurrent "complete" requests, each run in its own task so the node can
answer more than one at a time (vLLM does its own internal batching) and
so the event loop stays free to keep answering coordinator pings while a
completion is in flight in a thread (see vllm_process.py's
COMPLETE_TIMEOUT_SECONDS — a single completion can take up to 120s).
"""

from __future__ import annotations

import asyncio
import json

import websockets

from mycelium.node.vllm_process import VLLMProcess


async def handle_messages(websocket, process: VLLMProcess) -> None:
    """Loop reading messages until the connection closes, dispatching each
    "complete" message to its own task. Returns normally when the
    connection closes cleanly, or lets ConnectionClosed propagate on an
    abnormal close — cli.py's caller already treats both the same way
    (reconnect). Any tasks still running when the connection closes are
    cancelled rather than left to send a reply nobody can receive."""
    tasks: set[asyncio.Task] = set()
    try:
        async for raw in websocket:
            task = asyncio.create_task(_handle_complete(websocket, process, raw))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        for task in tasks:
            task.cancel()


async def _handle_complete(websocket, process: VLLMProcess, raw: str) -> None:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(message, dict) or message.get("type") != "complete":
        return

    request_id = message.get("request_id")
    prompt = message.get("prompt")
    try:
        # Broad except is deliberate here, not sloppy: whatever goes wrong
        # calling vLLM (HTTP error, timeout, malformed response) becomes a
        # complete_error the coordinator/client can see, per the design
        # doc for issue #10 — never left to hang or crash this task.
        text = await asyncio.to_thread(process.complete, prompt)
    except Exception as exc:
        reply = {"type": "complete_error", "request_id": request_id, "reason": str(exc)}
    else:
        reply = {"type": "complete_result", "request_id": request_id, "text": text}

    try:
        await websocket.send(json.dumps(reply))
    except websockets.exceptions.ConnectionClosed:
        pass  # coordinator connection is gone; nothing left to report to
