"""Where the exporter lifecycle is, for logs, status and debugging.

The phase is a label the lifecycle sets as it moves along. It is never read to
decide anything. Transitions are checked against ALLOWED, the table the
lifecycle diagram in the hooks documentation is drawn from (mermaid()). The
exporter tests check the table against the code both ways (exporter/conftest.py):
a transition the table does not list fails the test that takes it, and an edge
the lifecycle tests never take fails the run. Here an undeclared transition is
logged, and the lifecycle carries on.
"""

import logging
from enum import StrEnum

import anyio

logger = logging.getLogger(__name__)


class Phase(StrEnum):
    IDLE = "idle"
    SETUP = "setup"
    SERVING = "serving"
    CLEANUP = "cleanup"
    HOLDING = "holding"
    PARKED = "parked"
    STOPPING = "stopping"


ALLOWED: dict[Phase, frozenset[Phase]] = {
    # IDLE -> PARKED: the afterLease hook failed unexpectedly; no lease is admitted.
    Phase.IDLE: frozenset({Phase.SETUP, Phase.PARKED, Phase.STOPPING}),
    # SETUP -> HOLDING: setup failed with endLease/exit. SETUP -> CLEANUP: the
    # lease ended or a stop arrived during setup. SETUP -> IDLE: the lease ended
    # before setup started, or the activity was cancelled.
    Phase.SETUP: frozenset({Phase.SERVING, Phase.CLEANUP, Phase.HOLDING, Phase.IDLE}),
    Phase.SERVING: frozenset({Phase.CLEANUP, Phase.IDLE}),
    Phase.CLEANUP: frozenset({Phase.HOLDING, Phase.IDLE}),
    Phase.HOLDING: frozenset({Phase.IDLE}),
    Phase.PARKED: frozenset({Phase.STOPPING}),
    Phase.STOPPING: frozenset(),
}


def mermaid() -> str:
    """The lifecycle as a mermaid state diagram, drawn from ALLOWED."""
    order = list(Phase)
    lines = ["stateDiagram-v2", f"    [*] --> {Phase.IDLE}"]
    for old in order:
        lines += [f"    {old} --> {new}" for new in sorted(ALLOWED[old], key=order.index)]
    lines.append(f"    {Phase.STOPPING} --> [*]")
    return "\n".join(lines)


class PhaseTracker:
    def __init__(self) -> None:
        self._phase = Phase.IDLE
        self._since = 0.0

    @property
    def current(self) -> Phase:
        return self._phase

    def move(self, new: Phase) -> None:
        old = self._phase
        if new == old:
            return
        if new not in ALLOWED[old]:
            logger.error("Illegal lifecycle transition %s -> %s", old, new)
        now = anyio.current_time()
        if old != Phase.IDLE:
            logger.debug("Lifecycle %s ended after %.3fs", old, now - self._since)
        logger.info("Lifecycle %s -> %s", old, new)
        self._phase, self._since = new, now
