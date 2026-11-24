"""Where the exporter lifecycle is, for logs, status and debugging.

The phase is a label the lifecycle sets as it moves along. It is never read to
decide anything, and no table here says which moves exist: the lifecycle diagram
in the hooks documentation is drawn from the moves the lifecycle tests take, and
the tests fail when the diagram and the code differ (exporter/conftest.py).
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
        now = anyio.current_time()
        if old != Phase.IDLE:
            logger.debug("Lifecycle %s ended after %.3fs", old, now - self._since)
        logger.info("Lifecycle %s -> %s", old, new)
        self._phase, self._since = new, now
