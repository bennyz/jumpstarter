"""Level-triggered inputs to the exporter lifecycle.

Producers (the controller Status stream, stop requests, EndSession, a lease
release) only write the latest value here. The lifecycle is the single reader:
it waits for a predicate over the current inputs and always decides from live
values, so a late or repeated message can never be replayed into a decision.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

import anyio
from anyio import Event
from jumpstarter_protocol import jumpstarter_pb2


@dataclass(frozen=True)
class Assignment:
    """The lease the controller currently assigns to this exporter."""

    lease_name: str
    client_name: str = ""
    context: tuple[tuple[str, str], ...] = ()
    lease_uid: str = ""
    """Tells the lease apart from an earlier one with the same name."""

    @property
    def key(self) -> tuple[str, str]:
        return (self.lease_name, self.lease_uid)


@dataclass(frozen=True)
class StopRequest:
    now_at: float | None
    """anyio clock time of the first stop-now: stop as soon as possible, a running
    hook getting the stop grace from then. None: drain, stop once no lease is served."""
    unregister: bool
    force: bool = False
    """A second stop-now: skip every remaining grace period."""

    @property
    def now(self) -> bool:
        return self.now_at is not None


@dataclass(frozen=True)
class Inputs:
    assignment: Assignment | None = None
    generation: int = 0
    """Bumped whenever the assigned lease changes, including to and from none,
    so a lease that is released and assigned again is a new admission."""
    stop: StopRequest | None = None
    status_seen: bool = False
    """At least one Status message arrived: the assignment and hook record are known."""
    hook_record: jumpstarter_pb2.LeaseHooks | None = None
    """The controller's hook record of this exporter's latest lease; None if it keeps none."""

    @property
    def stop_now(self) -> bool:
        return self.stop is not None and self.stop.now


class InputBuffer:
    def __init__(self) -> None:
        self._inputs = Inputs()
        self._changed: Event | None = None

    def snapshot(self) -> Inputs:
        return self._inputs

    def snapshot_and_watch(self) -> tuple[Inputs, Event]:
        """The current inputs and an event set on the next change, with no await in between."""
        if self._changed is None:
            self._changed = Event()
        return self._inputs, self._changed

    async def wait_until(self, done: Callable[[Inputs], bool]) -> Inputs:
        while True:
            inputs, changed = self.snapshot_and_watch()
            if done(inputs):
                return inputs
            await changed.wait()

    def notify(self) -> None:
        """Wake waiters whose predicates read state outside the buffer (lease events)."""
        self._publish(self._inputs)

    def apply_status(self, status: jumpstarter_pb2.StatusResponse) -> Inputs:
        assignment = None
        if status.leased and status.lease_name:
            assignment = Assignment(
                lease_name=status.lease_name,
                client_name=status.client_name,
                context=tuple(sorted(status.context.items())),
                lease_uid=status.lease_uid,
            )
        current = self._inputs
        before = current.assignment.key if current.assignment else None
        after = assignment.key if assignment else None
        generation = current.generation + (1 if before != after else 0)
        record = status.lease_hooks if status.HasField("lease_hooks") else None
        self._publish(replace(
            current, assignment=assignment, generation=generation, status_seen=True, hook_record=record,
        ))
        return self._inputs

    def request_stop(self, *, now: bool, unregister: bool) -> StopRequest:
        current = self._inputs
        previous = current.stop
        now_at = previous.now_at if previous is not None else None
        if now and now_at is None:
            now_at = anyio.current_time()
        stop = StopRequest(
            now_at=now_at,
            unregister=unregister or (previous is not None and previous.unregister),
            force=(now and previous is not None and previous.now) or (previous is not None and previous.force),
        )
        self._publish(replace(current, stop=stop))
        return stop

    def _publish(self, inputs: Inputs) -> None:
        self._inputs = inputs
        changed, self._changed = self._changed, None
        if changed is not None:
            changed.set()
