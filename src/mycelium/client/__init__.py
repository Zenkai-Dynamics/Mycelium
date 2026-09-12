"""The client-side surface an agent author writes against.

Re-exported here so `mycelium.client.flow` and `mycelium.client.transport`
stay free to change shape — the package's stated design is a small set of
primitives, not a framework, and these eight names are all of it. See the
design doc for issue #59.

A hop failure surfaces as HopError, with the fault and the recorded Hop
attached. A discovery failure (Flow.list_models) surfaces as
TransportError instead — there is no Hop to attach and no fault to
report — so an agent author knows which one to catch. See the design
doc for issue #56.
"""

from mycelium.client.flow import (
    Exposure,
    Flow,
    Hop,
    HopError,
    assistant,
    system,
    user,
)
from mycelium.client.transport import TransportError

__all__ = [
    "Exposure",
    "Flow",
    "Hop",
    "HopError",
    "TransportError",
    "assistant",
    "system",
    "user",
]
