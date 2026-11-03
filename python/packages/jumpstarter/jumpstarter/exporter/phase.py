"""Where the exporter lifecycle is, for logs, status and debugging.

The phase is a label the lifecycle sets as it moves along. It is never read to
decide anything. Transitions are checked against ALLOWED, the same table the
documentation diagram is drawn from: lifecycle tests run strict and fail on an
undeclared edge, production logs it and carries on.
"""

import logging
import os
from enum import StrEnum

import anyio

logger = logging.getLogger(__name__)

STRICT_ENV = "JUMPSTARTER_STRICT_LIFECYCLE"


class Phase(StrEnum):
    IDLE = "idle"
    SETUP = "setup"
    SERVING = "serving"
    CLEANUP = "cleanup"
    HOLDING = "holding"
    STOPPING = "stopping"


ALLOWED: dict[Phase, frozenset[Phase]] = {
    Phase.IDLE: frozenset({Phase.SETUP, Phase.STOPPING}),
    # SETUP -> HOLDING: setup failed with endLease/exit. SETUP -> CLEANUP: the
    # lease ended or a stop arrived during setup. SETUP -> IDLE: the lease ended
    # before setup started, or the activity was cancelled.
    Phase.SETUP: frozenset({Phase.SERVING, Phase.CLEANUP, Phase.HOLDING, Phase.IDLE}),
    Phase.SERVING: frozenset({Phase.CLEANUP, Phase.IDLE}),
    Phase.CLEANUP: frozenset({Phase.HOLDING, Phase.IDLE}),
    Phase.HOLDING: frozenset({Phase.IDLE}),
    Phase.STOPPING: frozenset(),
}


class IllegalTransition(RuntimeError):
    pass


class PhaseTracker:
    def __init__(self, strict: bool | None = None) -> None:
        self._phase = Phase.IDLE
        self._since = 0.0
        self._strict = os.environ.get(STRICT_ENV) == "1" if strict is None else strict

    @property
    def current(self) -> Phase:
        return self._phase

    def move(self, new: Phase) -> None:
        old = self._phase
        if new == old:
            return
        if new not in ALLOWED[old]:
            if self._strict:
                raise IllegalTransition(f"{old} -> {new}")
            logger.error("Illegal lifecycle transition %s -> %s", old, new)
        now = anyio.current_time()
        if old != Phase.IDLE:
            logger.debug("Lifecycle %s ended after %.3fs", old, now - self._since)
        logger.info("Lifecycle %s -> %s", old, new)
        self._phase, self._since = new, now
