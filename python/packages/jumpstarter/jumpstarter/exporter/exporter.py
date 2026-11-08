import inspect
import logging
import math
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import partial
from typing import TYPE_CHECKING, Any, Self

import anyio
import anyio.lowlevel
import grpc
from anyio import (
    AsyncContextManagerMixin,
    CancelScope,
    Event,
    connect_unix,
    create_memory_object_stream,
    create_task_group,
    get_cancelled_exc_class,
    move_on_after,
    sleep,
    to_thread,
)
from anyio.abc import TaskGroup
from google.protobuf import empty_pb2
from jumpstarter_protocol import (
    jumpstarter_pb2,
    jumpstarter_pb2_grpc,
    telemetry_pb2_grpc,
)

from jumpstarter.common import ExporterStatus, LeaseHookPhase, Metadata, TemporarySocket
from jumpstarter.common.exceptions import CertificateDiscoveryError
from jumpstarter.common.streams import connect_router_stream
from jumpstarter.common.telemetry_token import TelemetryCredential
from jumpstarter.config.env import JMP_GRPC_INSECURE, JUMPSTARTER_GRPC_INSECURE
from jumpstarter.config.exporter import DEFAULT_STATUS_STREAM_RETRY_TIMEOUT
from jumpstarter.config.tls import TLSConfigV1Alpha1
from jumpstarter.exporter.hooks import HookExecutor, SetupOutcome, failure_ends_lease
from jumpstarter.exporter.inputs import Assignment, InputBuffer, Inputs
from jumpstarter.exporter.lease_context import LeaseContext
from jumpstarter.exporter.lease_hooks import (
    FINISHED_HOOK_PHASES,
    HOOK_NAMES,
    HookPlan,
    HookType,
    Restart,
    decide_restart,
)
from jumpstarter.exporter.metrics_stream import MetricsStreamClient
from jumpstarter.exporter.phase import Phase, PhaseTracker
from jumpstarter.exporter.session import Session
from jumpstarter.exporter.telemetry import TelemetryLogHandler
from jumpstarter.exporter.token_refresh import (
    calculate_token_refresh_sleep,
    calculate_token_rotation_retry_sleep,
    is_internal_exporter_token,
)
from jumpstarter.logging import clear_log_context, set_log_context

if TYPE_CHECKING:
    from jumpstarter.driver import Driver

logger = logging.getLogger(__name__)

# gRPC retry configuration
# Worst case ~18 min (21 attempts × 30s timeout + backoff sum ~8 min).
# The exporter has nothing useful to do while the controller is unreachable —
# a restart would only wait in _register_with_controller.
_TRANSIENT_GRPC_CODES = frozenset({grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED})
_RPC_MAX_RETRIES = 20
_RPC_BACKOFF_BASE = 1.0
_RPC_BACKOFF_CAP = 30.0
_RPC_TIMEOUT = 30
_TOKEN_REFRESH_CHECK_INTERVAL = 3600.0
_TOKEN_ROTATION_RETRY_INTERVAL = 3600.0
_FAIL_FAST_STREAM_CODES = frozenset({
    grpc.StatusCode.UNAUTHENTICATED,
    grpc.StatusCode.PERMISSION_DENIED,
    grpc.StatusCode.NOT_FOUND,
    grpc.StatusCode.INVALID_ARGUMENT,
    grpc.StatusCode.UNIMPLEMENTED,
})
_RESTARTABLE_STREAM_CODES = frozenset({
    grpc.StatusCode.UNAUTHENTICATED,
    grpc.StatusCode.PERMISSION_DENIED,
})
_TEMPORARY_STREAM_FAILURE_EXIT_CODE = 75  # EX_TEMPFAIL; service managers may restart this.


def _rpc_backoff(attempt: int) -> float:
    # The exponent is clamped so unbounded retry loops cannot overflow.
    return min(_RPC_BACKOFF_BASE * 2 ** min(attempt, 10), _RPC_BACKOFF_CAP)


def _is_retryable_stream_error(error: Exception) -> bool:
    if isinstance(error, grpc.aio.AioRpcError):
        return error.code() not in _FAIL_FAST_STREAM_CODES
    return isinstance(error, (CertificateDiscoveryError, OSError))

# The controller refused a hook record write (lease not assigned, attempt mismatch).
_LEASE_HOOK_REFUSED_CODES = frozenset({grpc.StatusCode.FAILED_PRECONDITION, grpc.StatusCode.INVALID_ARGUMENT})
# Failure actions (onFailure values) recorded for a failed lifecycle hook.
_HOOK_FAILURE_ACTIONS = {
    "warn": jumpstarter_pb2.LEASE_HOOK_FAILURE_ACTION_WARN,
    "endLease": jumpstarter_pb2.LEASE_HOOK_FAILURE_ACTION_END_LEASE,
    "exit": jumpstarter_pb2.LEASE_HOOK_FAILURE_ACTION_EXIT,
}
_HOOK_FAILURE_ACTION_NAMES = {action: name for name, action in _HOOK_FAILURE_ACTIONS.items()}


def _hook_failure_ended_lease(state: jumpstarter_pb2.LeaseHookState | None) -> bool:
    """Whether a recorded hook failed with an onFailure action that releases the lease."""
    return (
        state is not None
        and state.phase == LeaseHookPhase.FAILED
        and failure_ends_lease(_HOOK_FAILURE_ACTION_NAMES.get(state.on_failure, ""))
    )


def _note_recorded(
    lease: LeaseContext, hook: HookType, phase: LeaseHookPhase, on_failure: str, attempts: int
) -> None:
    """What a written hook transition tells this process about the lease."""
    if hook == "before_lease":
        lease.hooks.recorded = True
    if phase == LeaseHookPhase.RUNNING:
        lease.hooks.started[hook] = attempts
    if phase == LeaseHookPhase.FAILED and failure_ends_lease(on_failure):
        lease.hooks.controller_ends_lease = True


class Entry(Enum):
    """Where a lease's activity starts. Only a restart starts anywhere but NEW."""

    NEW = auto()  # set the lease up
    RESUME = auto()  # an earlier process set it up: no setup, no device reset
    FINISH_SETUP = auto()  # an earlier process's setup was cut off: rerun or fail it, per onInterrupt
    CLEAN_UP = auto()  # it owes cleanup: run it first thing


_SEVERITY_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def _severity_to_level(severity: str) -> int:
    """Map a JEP-0013 severity string to a Python logging level."""
    key = severity.lower() if severity else ""
    level = _SEVERITY_MAP.get(key)
    if level is None:
        if key:
            import structlog
            structlog.get_logger(__name__).warning(
                "Unrecognized min_severity value, defaulting to info",
                value=severity,
                accepted=list(_SEVERITY_MAP),
            )
        return logging.INFO
    return level


# Sidecar launcher socket injected by the ExporterSet QEMU provisioner.
_LAUNCHER_SOCKET_ENV = "JUMPSTARTER_LAUNCHER_SOCKET"
_DEFAULT_JMP_EXEC = "/shared/jumpstarter-exec"


def shutdown_runtime_sidecar(
    *,
    socket_path: str | None = None,
    binary: str | None = None,
    timeout: float = 10.0,
) -> bool:
    """Ask jumpstarter-exec serve (runtime container PID 1) to exit cleanly.

    With native sidecars (KEP-753), kubelet already terminates ``target-runtime``
    after the exporter (main) container exits, so Pod completion does not depend
    on this call. Invoking ``jumpstarter-exec shutdown`` is best-effort: it
    SIGTERMs in-flight Exec children (e.g. QEMU) for a faster, cleaner teardown
    than waiting for the Pod termination grace period.

    Returns True if a shutdown was attempted successfully, False if no launcher
    socket is configured (non-sidecar / InPlaceReuse hosts) or shutdown failed.

    Callers on the async event loop must offload this via
    ``await to_thread.run_sync(shutdown_runtime_sidecar)``.
    """
    import os
    import subprocess
    from pathlib import Path

    sock = socket_path or os.environ.get(_LAUNCHER_SOCKET_ENV)
    if not sock:
        return False

    exec_bin = Path(binary) if binary else Path(_DEFAULT_JMP_EXEC)
    if not exec_bin.is_file():
        # Fall back to the binary next to the socket (same shared volume).
        candidate = Path(sock).parent / "jumpstarter-exec"
        if candidate.is_file():
            exec_bin = candidate
        else:
            logger.warning(
                "jumpstarter-exec binary not found at %s or %s; cannot shut down runtime",
                _DEFAULT_JMP_EXEC,
                candidate,
            )
            return False

    cmd = [str(exec_bin), "shutdown", "--socket", sock]
    logger.info("Shutting down runtime sidecar via %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
    except OSError as e:
        # FileNotFoundError, PermissionError, and other pre-exec failures.
        logger.warning("jumpstarter-exec not executable at %s: %s", exec_bin, e)
        return False
    except subprocess.TimeoutExpired:
        logger.warning("jumpstarter-exec shutdown timed out after %ss", timeout)
        return False

    if result.returncode != 0:
        logger.warning(
            "jumpstarter-exec shutdown exited %s: stdout=%r stderr=%r",
            result.returncode,
            result.stdout,
            result.stderr,
        )
        return False

    logger.info("Runtime sidecar shutdown acknowledged")
    return True


@dataclass(kw_only=True)
class Exporter(AsyncContextManagerMixin, Metadata):
    """Represents a Jumpstarter Exporter runtime instance.

    Inherits from Metadata, which provides:
        uuid: Unique identifier for the exporter instance (UUID4)
        labels: Key-value labels for exporter identification and selector matching
    """

    # Public Configuration Fields

    # JWT subject for this exporter. Register no longer carries
    # jumpstarter.dev/name (#1058), so the name is not stored in labels.
    exporter_name: str = "unknown"

    channel_factory: Callable[[str], Awaitable[grpc.aio.Channel]]
    """Factory function for creating gRPC channels to communicate with the controller.

    Called multiple times throughout the exporter lifecycle to establish connections.
    Receives the current token and handles authentication, credentials, and channel configuration.
    Used when creating controller stubs, unregistering, and establishing streams.
    """

    device_factory: Callable[[], "Driver"]
    """Factory function for creating Driver instances representing the hardware/devices.

    Called when creating Sessions to provide access to the underlying device.
    The Driver can contain child drivers in a composite pattern, representing
    the full device tree being exported. Typically created from ExporterConfigV1Alpha1.
    """

    tls: TLSConfigV1Alpha1 = field(default_factory=TLSConfigV1Alpha1)
    """TLS/SSL configuration for secure communication with router and controller.

    Contains certificate authority (ca) and insecure flag for certificate verification.
    Passed to connect_router_stream() when handling client connections.
    Default creates empty config with ca="" and insecure=False.
    """

    grpc_options: dict[str, str] = field(default_factory=dict)
    """Custom gRPC channel options that override or supplement default settings.

    Merged with defaults (round_robin load balancing, keepalive settings, etc.).
    Configured via YAML as grpcOptions in exporter config.
    Passed to connect_router_stream() for client connections.
    """

    motd: str | None = field(default=None)
    """Message of the day shown to clients when they enter a shell on this exporter.

    Configured via YAML as motd in exporter config and returned to clients in GetReport.
    """

    hook_executor: HookExecutor | None = field(default=None)
    """Optional executor for lifecycle hooks (before-lease and after-lease).

    When configured, runs custom scripts at key points in the lease lifecycle:
    - before-lease: Runs when transitioning to leased state (setup, validation)
    - after-lease: Runs when transitioning from leased state (cleanup, reset)
    Created when hooks.before_lease or hooks.after_lease are defined in config.
    """

    exit_on_lease_end: bool = field(default=False)
    """When True, the exporter exits after serving one lease.

    The lease is known to be the last one when it is admitted, so the exporter
    drains from that moment and never reports AVAILABLE again.
    """

    stop_grace: float = field(default=25.0)
    """Seconds a running hook may continue after a stop-now request.

    Counted from the first stop request and shared by every hook that runs
    after it. Keep it below the service manager's stop timeout. A second
    stop-now request skips what is left of it.
    """

    status_stream_retry_timeout: float = DEFAULT_STATUS_STREAM_RETRY_TIMEOUT
    """Maximum seconds without a Status item after an error or stream EOF."""

    token: str = field(default="")
    """Bearer token used to authenticate with the telemetry service.

    Set from ExporterConfigV1Alpha1.token so PushLogs calls can include
    the exporter's JWT as an Authorization header.
    """

    on_token_rotated: Callable[[str], Awaitable[None] | None] | None = field(default=None)
    """Optional callback invoked when the authentication token is rotated.

    Passed the new token string. Can be async or sync.
    """

    _token_rotation_unsupported: bool = field(init=False, default=False)

    # Internal State Fields

    _registered: bool = field(init=False, default=False)
    """Tracks whether exporter has successfully registered with the controller.

    Set to True after successful registration. Used to determine if unregistration
    is needed during cleanup.
    """

    _unregister: bool = field(init=False, default=False)
    """Internal flag indicating whether to actively unregister during shutdown.

    Set when stop(should_unregister=True) is called. When False, relies on
    heartbeat timeout for implicit unregistration.
    """

    _inputs: InputBuffer = field(init=False, default_factory=InputBuffer)
    """Latest controller assignment and stop request, read by the lifecycle."""

    _phase: PhaseTracker = field(init=False, default_factory=PhaseTracker)
    """Where the lifecycle is. A label for logs; never read to decide."""

    _forced: bool = field(init=False, default=False)
    """A second stop-now cancelled everything; skip cleanup on the way out."""

    _tg: TaskGroup | None = field(init=False, default=None)
    """Reference to the anyio TaskGroup managing concurrent tasks.

    Manages streams and connection handling tasks. Used to cancel all tasks
    when stopping. Set during serve() and cleared when done.
    """

    _exporter_status: ExporterStatus = field(init=False, default=ExporterStatus.OFFLINE)
    """Current status of the exporter.

    Updated via _update_status() and reported to controller and session.
    Possible values: OFFLINE, AVAILABLE, BEFORE_LEASE_HOOK, LEASE_READY,
    AFTER_LEASE_HOOK, BEFORE_LEASE_HOOK_FAILED, AFTER_LEASE_HOOK_FAILED.
    """

    _exit_code: int | None = field(init=False, default=None)
    """Exit code to use when the exporter shuts down.

    Hook failures with on_failure='exit' use 1 to request shutdown. A Status
    outage or an authentication/authorization error uses 75 to request a
    service-manager restart.
    """

    _controller_stream_failed: bool = field(init=False, default=False)
    """Skip controller RPCs during cleanup after a fatal stream failure."""

    _standalone: bool = field(init=False, default=False)
    """When True, exporter runs without a controller (TCP listener only).

    _report_status and __aexit__ skip controller calls when _standalone is True.
    """

    _lease_context: LeaseContext | None = field(init=False, default=None)
    """Encapsulates all resources associated with the current lease.

    Contains the session, socket path, and synchronization event needed
    throughout the lease lifecycle. This replaces the previous individual
    _current_session, _session_socket_path, and _before_lease_hook fields.

    Set by _serve_lease for exactly the lifetime of one lease's activity, and
    read by status reporting, the release path and the Status stream sink.
    """

    _telemetry_handler: "TelemetryLogHandler | None" = field(init=False, default=None)
    """Optional telemetry log handler that pushes log entries to jumpstarter-telemetry.

    Created after successful registration when GetServiceEndpoints returns a
    telemetry endpoint. None when telemetry is not configured or not available.
    """

    _telemetry_channel: grpc.aio.Channel | None = field(init=False, default=None)
    """gRPC channel to the telemetry service. Closed on exporter shutdown."""

    _metrics_stream: MetricsStreamClient | None = field(init=False, default=None)
    """Optional MetricsStream client. Started next to the log flush loop when telemetry is configured."""

    _telemetry_credential: TelemetryCredential | None = field(init=False, default=None)
    """Token sent on telemetry calls. Renewed next to the log flush loop."""

    _status_drain_active: bool = field(init=False, default=False)
    """True only while serve()'s task group is running and the drain task is active.

    When True, _report_status enqueues status updates for background processing.
    When False (registration, unregistration), _report_status awaits directly.
    """

    _pending_status_request: jumpstarter_pb2.ReportStatusRequest | None = field(init=False, default=None)
    """Latest status update pending delivery by the background drain task.

    "Latest wins" semantics: if multiple _report_status calls happen while the
    drain task is busy retrying, only the most recent one is sent.
    """

    _status_rpc_event: Event = field(init=False, default_factory=Event)
    """Signals the drain task that a new status update is pending."""

    @property
    def name(self) -> str:
        return self.exporter_name

    @property
    def _records_lease_hooks(self) -> bool:
        """Whether this exporter records its leases' hooks with the controller.

        Not for exitOnLeaseEnd exporters, whose runtime does not outlive the
        exporter process, nor standalone ones, which have no controller.
        """
        return self.hook_executor is not None and not self.exit_on_lease_end and not self._standalone

    @property
    def _tracks_lease_hooks(self) -> bool:
        """Whether new leases have their hooks recorded: this exporter records them and the controller keeps them."""
        return self._records_lease_hooks and self._inputs.snapshot().hook_record is not None

    def _on_interrupt(self, hook: HookType) -> str | None:
        """The configured onInterrupt of a hook; None if no such hook is configured."""
        config = getattr(self.hook_executor.config, hook) if self.hook_executor else None
        return config.on_interrupt if config is not None else None

    async def _record_lease_hook(
        self,
        lease: LeaseContext,
        hook: HookType,
        phase: LeaseHookPhase,
        message: str = "",
        on_failure: str = "",
        attempts: int | None = None,
    ) -> bool:
        """Record a hook transition of the lease with the controller (Exporter.status.leaseHooks).

        The record outlives this process: the next one resumes the lease from it.
        Hooks start only once their start is recorded, and every transition is
        recorded before the lease moves on, retrying while the controller is
        unreachable. The exceptions are a setup whose lease ended before it
        started, which must not start, and a stop: the write gets the stop grace,
        like a hook, and the next process reads what it did not record. Returns
        False if the transition was not recorded; a hook whose start returns
        False must not run. A controller without the call keeps no record, and
        the lease's hooks run without one.
        """
        if not lease.hooks.tracked:
            return True
        if attempts is None:
            attempts = lease.hooks.attempt(hook)
        request = jumpstarter_pb2.UpdateLeaseHooksRequest(
            lease_name=lease.lease_name,
            lease_uid=lease.lease_uid,
            **{
                hook: jumpstarter_pb2.LeaseHookState(
                    phase=jumpstarter_pb2.LeaseHookPhase.ValueType(phase),
                    on_failure=_HOOK_FAILURE_ACTIONS.get(
                        on_failure, jumpstarter_pb2.LEASE_HOOK_FAILURE_ACTION_UNSPECIFIED
                    ),
                    message=message,
                    attempts=attempts,
                )
            },
        )
        setup_start = hook == "before_lease" and phase in (LeaseHookPhase.RUNNING, LeaseHookPhase.SKIPPED)
        description = f"record {HOOK_NAMES[hook]} hook {phase}"
        async with self._stoppable(f"Recording {HOOK_NAMES[hook]} hook {phase}"):
            while True:
                if setup_start and lease.lease_ended.is_set():
                    logger.info(
                        "Not recording %s: lease %s ended before its setup started", description, lease.lease_name
                    )
                    return False
                ok, code = await self._retry_rpc(
                    lambda ctrl: ctrl.UpdateLeaseHooks(request, timeout=_RPC_TIMEOUT),
                    description,
                    non_retryable_codes=_LEASE_HOOK_REFUSED_CODES | {grpc.StatusCode.UNIMPLEMENTED},
                )
                if ok:
                    _note_recorded(lease, hook, phase, on_failure, attempts)
                    return True
                if code == grpc.StatusCode.UNIMPLEMENTED:
                    logger.warning(
                        "Controller does not record lease hooks; the hooks of lease %s run without a record",
                        lease.lease_name,
                    )
                    lease.hooks.tracked = False
                    return True
                if code in _LEASE_HOOK_REFUSED_CODES:
                    logger.warning("Controller refused to %s for lease %s", description, lease.lease_name)
                    return False
                # Retries ran out with the controller still unreachable: the record must
                # not be skipped, so keep going until it is written or a stop's grace ends.
                logger.warning("Still unable to %s for lease %s; retrying", description, lease.lease_name)
                await sleep(_RPC_BACKOFF_CAP)
        return False  # a stop's grace ran out before the controller came back

    def stop(self, wait_for_lease_exit=False, should_unregister=False, exit_code: int | None = None):
        """Ask the exporter to stop.

        Args:
            wait_for_lease_exit (bool): Drain: finish the current lease, take no new one, then stop.
            should_unregister (bool): Unregister from the controller on the way out.
            exit_code (int | None): If set, the exporter will exit with this code.

        A stop-now ends waiting at once and gives a running hook stop_grace
        seconds. A second stop-now cancels everything immediately.
        """
        if exit_code is not None:
            self._exit_code = exit_code
        stop = self._inputs.request_stop(now=not wait_for_lease_exit, unregister=should_unregister)
        if stop.force:
            logger.warning("Repeated stop request: stopping immediately, skipping remaining hook grace")
            self._forced = True
            self._unregister = stop.unregister
            if self._tg is not None:
                self._tg.cancel_scope.cancel()
        elif stop.now:
            logger.info("Stopping exporter (%s)", "will unregister" if stop.unregister else "will not unregister")
        else:
            logger.info("Exporter marked for stop upon lease exit")

    @property
    def exit_code(self) -> int | None:
        """Get the exit code for the exporter.

        Returns:
            The requested exit code, or None if no explicit code was set.
        """
        return self._exit_code

    @asynccontextmanager
    async def _controller_stub(self) -> AsyncGenerator[jumpstarter_pb2_grpc.ControllerServiceStub, None]:
        """Create a controller service stub as a context manager.

        Yields:
            ControllerServiceStub connected to the controller

        The underlying channel is automatically closed when the context exits.
        """
        channel = await self.channel_factory(self.token)
        try:
            yield jumpstarter_pb2_grpc.ControllerServiceStub(channel)
        finally:
            await channel.close()

    async def _retry_stream(
        self,
        stream_name: str,
        stream_factory: Callable[[jumpstarter_pb2_grpc.ControllerServiceStub], AsyncGenerator],
        send_tx,
        backoff: float = 0.5,
        outage_budget: float | None = None,
        on_reconnect: Callable[[], None] = lambda: None,
    ):
        """Reconnect a controller stream until data resumes.

        The outage budget applies only while no lease is served: client sessions
        go through the router and survive a controller outage, and a restarted
        exporter could do nothing without the controller anyway. Losing the
        controller (the budget running out, or a fatal error) stops the exporter
        like a stop-now does, so a running hook gets its stop grace.
        """
        delay = backoff
        outage_started = None
        kept_lease = None
        while True:
            scope = CancelScope(deadline=self._outage_deadline(outage_started, outage_budget))
            try:
                with scope:
                    async with self._controller_stub() as controller:
                        async for item in stream_factory(controller):
                            if outage_started is not None:
                                logger.info(
                                    "%s stream reconnected after %.1fs",
                                    stream_name,
                                    anyio.current_time() - outage_started,
                                )
                                outage_started = None
                                kept_lease = None
                                scope.deadline = math.inf
                                on_reconnect()
                            delay = backoff
                            await send_tx.send(item)
            except Exception as e:
                if not _is_retryable_stream_error(e):
                    if isinstance(e, grpc.aio.AioRpcError):
                        # A new process reloads rotated credentials; PermissionDenied
                        # can also mean the controller temporarily lost API access.
                        exit_code = _TEMPORARY_STREAM_FAILURE_EXIT_CODE if e.code() in _RESTARTABLE_STREAM_CODES else 1
                        self._lose_controller(exit_code, f"{stream_name} stream failed: {e.code().name}: {e.details()}")
                        return
                    raise
                reason = str(e)
            else:
                reason = "stream ended" if not scope.cancelled_caught else "outage deadline reached"

            now = anyio.current_time()
            if outage_started is None:
                outage_started = now
                logger.warning("%s stream interrupted (%s); retrying", stream_name, reason)
            else:
                logger.debug("%s stream still unavailable: %s", stream_name, reason)

            wait, kept_lease = self._outage_wait(stream_name, delay, now - outage_started, outage_budget, kept_lease)
            if wait is None:
                return
            # Keep the lease session alive until Status confirms release. EOF
            # needs the same backoff as an error to avoid a busy reconnect loop.
            await sleep(wait)
            delay = min(delay * 2, 5.0)

    def _outage_deadline(self, outage_started: float | None, budget: float | None) -> float:
        """When a reconnect attempt gives up: the outage budget counts only while no lease is served."""
        if outage_started is None or budget is None or self._lease_context is not None:
            return math.inf
        return outage_started + budget

    def _outage_wait(
        self, stream_name: str, delay: float, elapsed: float, budget: float | None, logged_for: LeaseContext | None
    ) -> tuple[float | None, LeaseContext | None]:
        """How long to wait before reconnecting; None once the controller is given up on.

        Past the budget, a served lease keeps running (logged once per lease, the
        lease returned for that); with no lease, the controller is lost.
        """
        if budget is None:
            return delay, logged_for
        if elapsed < budget:
            return min(delay, budget - elapsed), logged_for
        lease = self._lease_context
        if lease is None:
            self._lose_controller(
                _TEMPORARY_STREAM_FAILURE_EXIT_CODE,
                f"{stream_name} stream unavailable for {elapsed:.1f}s (budget {budget:.1f}s)",
            )
            return None, None
        if lease is not logged_for:
            logger.warning(
                "%s stream unavailable for %.1fs; keeping lease %s running until the controller is back",
                stream_name, elapsed, lease.lease_name,
            )
        return delay, lease

    def _lose_controller(self, exit_code: int, reason: str) -> None:
        """Give up on the controller: stop now, as a stop request does.

        Waiting phases end at once and a running hook gets the stop grace. A
        lease whose hooks are recorded is left to the next process.
        """
        logger.error("%s; stopping", reason)
        self._controller_stream_failed = True
        self._exit_code = exit_code
        if not self._inputs.snapshot().stop_now:  # a repeated stop-now would skip the grace
            self._inputs.request_stop(now=True, unregister=False)

    def _resend_status(self) -> None:
        """Send the latest status again once the Status stream is back.

        A report can be dropped during an outage, and the controller clears an
        Offline status when the stream reconnects.
        """
        if self._status_drain_active and self._pending_status_request is not None:
            self._status_rpc_event.set()

    def _listen_stream_factory(
        self, lease_name: str
    ) -> Callable[[jumpstarter_pb2_grpc.ControllerServiceStub], AsyncGenerator[jumpstarter_pb2.ListenResponse, None]]:
        """Create a stream factory for listening to connection requests."""

        def factory(
            ctrl: jumpstarter_pb2_grpc.ControllerServiceStub,
        ) -> AsyncGenerator[jumpstarter_pb2.ListenResponse, None]:
            return ctrl.Listen(jumpstarter_pb2.ListenRequest(lease_name=lease_name))

        return factory

    def _status_stream_factory(
        self,
    ) -> Callable[[jumpstarter_pb2_grpc.ControllerServiceStub], AsyncGenerator[jumpstarter_pb2.StatusResponse, None]]:
        """Create a stream factory for status updates."""

        def factory(
            ctrl: jumpstarter_pb2_grpc.ControllerServiceStub,
        ) -> AsyncGenerator[jumpstarter_pb2.StatusResponse, None]:
            return ctrl.Status(jumpstarter_pb2.StatusRequest())

        return factory

    async def _register_with_controller(self, local_channel: grpc.aio.Channel):
        """Register the exporter with the controller.

        Waits for an unreachable controller, as the streams do: a process that
        gave up would only be restarted into the same wait. A stop ends the
        wait, with nothing registered and so nothing to unregister.

        Args:
            local_channel: The local Unix socket channel to get device reports from
        """
        # Get device reports from the local session
        exporter_stub = jumpstarter_pb2_grpc.ExporterServiceStub(local_channel)
        response: jumpstarter_pb2.GetReportResponse = await exporter_stub.GetReport(empty_pb2.Empty())
        request = jumpstarter_pb2.RegisterRequest(
            labels=self.labels,
            reports=response.reports,
            # Lets the controller hold this exporter while its record owes cleanup.
            records_lease_hooks=self._records_lease_hooks,
        )

        # Register with the REMOTE controller (not the local session)
        logger.info("Registering exporter with controller")
        attempt = 0
        while True:
            try:
                async with self._controller_stub() as controller:
                    await controller.Register(request)
                break
            except grpc.aio.AioRpcError as e:
                if e.code() not in _TRANSIENT_GRPC_CODES:
                    raise
                error = f"{e.code().name}: {e.details()}"
            except (CertificateDiscoveryError, OSError) as e:
                error = str(e)
            backoff = _rpc_backoff(attempt)
            attempt += 1
            logger.warning("Controller unreachable (%s); registering again in %.1fs", error, backoff)
            with move_on_after(backoff):
                await self._inputs.wait_until(lambda i: i.stop is not None)
            if self._inputs.snapshot().stop is not None:
                logger.info("Stopping before the exporter registered")
                return
        # Mark exporter as registered internally
        self._registered = True
        # Only report AVAILABLE status during initial registration (no lease context)
        # During per-lease registration, status is managed by serve() to avoid
        # overwriting LEASE_READY with AVAILABLE
        # An exporter that records hooks reports AVAILABLE once it has checked the
        # record for work a previous process left (_take_over_from_previous_process).
        if self._lease_context is None and not self._records_lease_hooks:
            await self._report_status(ExporterStatus.AVAILABLE, "Exporter registered and available")

        # Discover optional telemetry service endpoint.
        await self._setup_telemetry()

    async def _setup_telemetry(self) -> None:
        """Discover and connect to the optional telemetry service.

        Calls GetServiceEndpoints on the controller. When a telemetry endpoint is
        returned, a gRPC channel is created, TelemetryLogHandler is attached
        to the root Python logger, and a MetricsStreamClient is prepared for
        reverse-scrape. Safe to call multiple times; subsequent calls
        are no-ops if a handler is already configured.
        """
        if self._telemetry_handler is not None:
            return

        try:
            resp = await self._get_service_endpoints()
        except grpc.aio.AioRpcError as e:
            # Older controllers that don't support this RPC return UNIMPLEMENTED.
            # Any other error is also non-fatal — telemetry is best-effort.
            logger.debug("GetServiceEndpoints unavailable: %s", e.code())
            return

        if not resp.telemetry_endpoints:
            logger.debug("No telemetry endpoint configured, skipping telemetry setup")
            return

        ep = resp.telemetry_endpoints[0]
        logger.info("Connecting to telemetry service at %s (min_severity=%s)", ep.endpoint, ep.min_severity)

        grpc_insecure = (
            self.tls.insecure
            or os.getenv(JMP_GRPC_INSECURE) == "1"
            or os.getenv(JUMPSTARTER_GRPC_INSECURE) == "1"
        )

        if ep.certificate:
            # Use CA certificate provided by the controller for the telemetry endpoint.
            self._telemetry_channel = grpc.aio.secure_channel(
                ep.endpoint,
                grpc.ssl_channel_credentials(root_certificates=ep.certificate.encode()),
            )
        elif grpc_insecure:
            # Development/testing mode: plaintext gRPC, no TLS at all.
            self._telemetry_channel = grpc.aio.insecure_channel(ep.endpoint)
        else:
            # Production: TLS with system CA pool.
            self._telemetry_channel = grpc.aio.secure_channel(
                ep.endpoint, grpc.ssl_channel_credentials()
            )
        stub = telemetry_pb2_grpc.TelemetryServiceStub(self._telemetry_channel)
        # Both consumers read the token per request, so renewal reaches them.
        credential = TelemetryCredential(ep)
        self._telemetry_credential = credential
        handler = TelemetryLogHandler(stub, namespace=getattr(self, "namespace", "") or "", token=credential)
        handler.setLevel(_severity_to_level(ep.min_severity))
        logging.getLogger().addHandler(handler)
        self._telemetry_handler = handler
        self._metrics_stream = MetricsStreamClient(
            stub,
            # JWT subject. Not the jumpstarter.dev/name label (#1058).
            identity=self.exporter_name,
            token=credential,
        )
        logger.info("Telemetry log handler attached")

    async def _get_service_endpoints(self) -> jumpstarter_pb2.GetServiceEndpointsResponse:
        async with self._controller_stub() as controller:
            return await controller.GetServiceEndpoints(
                jumpstarter_pb2.GetServiceEndpointsRequest(),
                timeout=_RPC_TIMEOUT,
            )

    async def rotate_token(self) -> str:
        """Request a newly signed authentication token from the controller.

        Updates self.token and invokes the on_token_rotated callback if configured.

        Returns:
            The newly rotated token string.
        """
        async with self._controller_stub() as controller:
            response: jumpstarter_pb2.RotateTokenResponse = await controller.RotateToken(
                jumpstarter_pb2.RotateTokenRequest(),
                timeout=_RPC_TIMEOUT,
            )

        new_token = response.token
        self.token = new_token

        if self.on_token_rotated is not None:
            try:
                res = self.on_token_rotated(new_token)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                logger.exception("Error in on_token_rotated callback")

        return new_token

    async def _token_refresh_loop(self) -> None:
        """Monitor token expiration and request a new token when it is about to expire."""
        while True:
            # The server also verifies the internal signature, issuer and exporter UID.
            if not is_internal_exporter_token(self.token):
                logger.debug("Token is not an internal exporter credential; automatic refresh disabled")
                return
            sleep_duration = calculate_token_refresh_sleep(self.token)
            if sleep_duration is None:
                logger.debug("Token does not have an expiry claim; automatic refresh disabled")
                return

            if sleep_duration > 0:
                logger.debug("Token refresh scheduled in %.1f seconds", sleep_duration)
                await sleep(min(sleep_duration, _TOKEN_REFRESH_CHECK_INTERVAL))
                continue

            if not await self._rotate_token_with_retry():
                return

    async def _rotate_token_with_retry(self) -> bool:
        attempt = 0
        while True:
            # The controller rejects an expired token before it reaches rotation.
            if calculate_token_rotation_retry_sleep(self.token, _TOKEN_ROTATION_RETRY_INTERVAL) <= 0:
                logger.error("Exporter token expired; re-provision the exporter credential")
                return False
            try:
                logger.info("Token is about to expire; requesting new token from controller")
                await self.rotate_token()
                self._token_rotation_unsupported = False
                logger.info("Successfully refreshed exporter authentication token")
                return True
            except grpc.aio.AioRpcError as e:
                code = e.code()
                if code == grpc.StatusCode.UNAUTHENTICATED:
                    # The controller rejected the credential itself (expired by its clock,
                    # or from an issuer it does not trust), so renewal cannot succeed.
                    logger.error("Failed to rotate token (%s): %s", code, e.details())
                    return False
                if code in (grpc.StatusCode.UNIMPLEMENTED, grpc.StatusCode.PERMISSION_DENIED):
                    delay = calculate_token_rotation_retry_sleep(self.token, _TOKEN_ROTATION_RETRY_INTERVAL)
                    if delay <= 0:
                        continue  # reported as expired at the top of the loop
                    if code == grpc.StatusCode.UNIMPLEMENTED:
                        log = logger.debug if self._token_rotation_unsupported else logger.warning
                        log("Controller does not support token rotation (UNIMPLEMENTED); retrying in %.1fs", delay)
                        self._token_rotation_unsupported = True
                    else:
                        logger.warning("Token rotation denied (%s); retrying in %.1fs", e.details(), delay)
                    await sleep(delay)
                    return True
                failure = f"{code}: {e.details()}"
            except Exception as e:  # noqa: BLE001 - keep retrying after unexpected refresh failures
                failure = str(e)
            backoff = _rpc_backoff(attempt)
            attempt += 1
            logger.warning("Failed to rotate token (%s); retrying in %.1fs (attempt %d)", failure, backoff, attempt)
            await sleep(backoff)

    async def _retry_rpc(
        self,
        rpc_call: Callable,
        description: str,
        *,
        non_retryable_codes: frozenset[grpc.StatusCode] = frozenset(),
        lease_context: LeaseContext | None = None,
    ) -> tuple[bool, grpc.StatusCode | None]:
        """Retry a unary gRPC call with exponential backoff.

        Args:
            rpc_call: Async callable that takes a controller stub and performs the RPC
            description: Human-readable description for log messages
            non_retryable_codes: gRPC status codes that should cause immediate return
                without retry (e.g. UNIMPLEMENTED for unsupported RPCs)
            lease_context: For release RPCs, stop if this lease is replaced or ended.

        Returns:
            (True, None) on success.
            (False, status_code) on non-retryable error or retry exhaustion.
            (False, None) on non-gRPC exception or stale lease.
        """
        for attempt in range(_RPC_MAX_RETRIES + 1):
            try:
                async with self._controller_stub() as controller:
                    # Channel setup also awaits; check immediately before every send.
                    if lease_context is not None and (
                        self._lease_context is not lease_context or lease_context.lease_ended.is_set()
                    ):
                        logger.debug("Skipping %s for stale lease %s", description, lease_context.lease_name)
                        return False, None
                    await rpc_call(controller)
                return True, None
            except grpc.aio.AioRpcError as e:
                if e.code() in non_retryable_codes:
                    return False, e.code()
                if e.code() in _TRANSIENT_GRPC_CODES and attempt < _RPC_MAX_RETRIES:
                    backoff = _rpc_backoff(attempt)
                    logger.warning(
                        "Transient error %s (attempt %d/%d), retrying in %.1fs: %s",
                        description,
                        attempt + 1,
                        _RPC_MAX_RETRIES + 1,
                        backoff,
                        e,
                    )
                    await anyio.sleep(backoff)
                    continue
                logger.exception("Failed to %s", description)
                return False, e.code()
            except Exception as e:  # noqa: BLE001
                logger.error("Failed to %s: %s", description, e)
                return False, None
        return False, None

    async def _send_report_status_rpc(self, request: jumpstarter_pb2.ReportStatusRequest) -> bool:
        """Send ReportStatus RPC to the controller with retry on transient errors.

        Returns:
            True if the RPC succeeded, False if unsupported or retries exhausted
        """
        ok, code = await self._retry_rpc(
            lambda ctrl: ctrl.ReportStatus(request, timeout=_RPC_TIMEOUT),
            "report status",
            non_retryable_codes=frozenset({grpc.StatusCode.UNIMPLEMENTED}),
        )
        if not ok and code == grpc.StatusCode.UNIMPLEMENTED:
            # Legacy support: ReportStatus added Nov 2025 (commit b76f6c87).
            # All production controllers support it; safe to remove in future versions.
            logger.warning("ReportStatus not supported by controller, status updates will be skipped")
        return ok

    async def _drain_status_reports(self):
        """Background task that sends status updates to the controller.

        Runs during serve() to make _report_status non-blocking. Uses "latest wins"
        semantics: if multiple status updates arrive while retrying, only the most
        recent one is sent. This prevents blocking the client connection path when
        the controller is temporarily unreachable.
        """
        while True:
            await self._status_rpc_event.wait()
            self._status_rpc_event = Event()  # Reset for next update
            request = self._pending_status_request
            if request and await self._send_report_status_rpc(request):
                logger.info(
                    "Updated status to %s: %s",
                    ExporterStatus.from_proto(request.status),
                    request.message,
                )

    async def _report_status(self, status: ExporterStatus, message: str = ""):
        """Report the exporter status with the controller and session."""
        self._exporter_status = status

        # Update status in lease context (handles session update internally)
        # This ensures status is stored even before session is created
        if self._lease_context:
            self._lease_context.update_status(status, message)

        if self._standalone:
            logger.debug("Updated status to %s: %s (standalone, no controller)", status, message)
            return

        if status == ExporterStatus.AVAILABLE and self._inputs.snapshot().stop is not None:
            # The client session still sees AVAILABLE (its cleanup is done), but
            # the controller must not: this exporter takes no further lease.
            logger.info("Not reporting AVAILABLE to the controller: exporter is stopping")
            return

        request = jumpstarter_pb2.ReportStatusRequest(
            status=status.to_proto(),
            message=message,
        )

        if self._status_drain_active:
            # During serve(): enqueue for background drain (non-blocking)
            self._pending_status_request = request
            self._status_rpc_event.set()
        else:
            # Outside serve() (registration, unregistration): send directly
            if await self._send_report_status_rpc(request):
                logger.info("Updated status to %s: %s", status, message)

    async def _request_lease_release(self, lease_scope: LeaseContext):
        """Request the controller to release lease_scope's lease.

        Called when a lifecycle hook fails with on_failure='endLease'.
        Ordinary hook completion, client disconnection, and exporter cleanup
        must not request release; the client or controller ends those leases.

        Each hook gets this callback bound to its own lease, so a delayed call
        cannot release, or report AVAILABLE over, a replacement lease.

        Uses the named ReleaseLease RPC, supported for exporters since controller
        v0.9.0, so retries cannot target a replacement lease. A controller that
        recorded the failure ends the lease itself; the lease then ends through
        the Status stream like any other.
        """
        if lease_scope.hooks.controller_ends_lease:
            logger.info("The controller ends lease %s after its endLease hook failure", lease_scope.lease_name)
            return
        if self._controller_stream_failed:
            logger.info("Skipping lease-release RPC after controller stream failure")
            return
        # The lease already ended (controller sent leased=false, or a previous
        # call released it), or a replacement took the slot: nothing to release.
        if self._lease_context is not lease_scope or lease_scope.lease_ended.is_set():
            logger.debug("Lease %s no longer active, skipping release request", lease_scope.lease_name)
            return

        if self._standalone:
            self._end_lease_locally(lease_scope)
            return

        ok, _ = await self._retry_rpc(
            lambda ctrl: ctrl.ReleaseLease(
                jumpstarter_pb2.ReleaseLeaseRequest(name=lease_scope.lease_name), timeout=_RPC_TIMEOUT
            ),
            "release lease",
            lease_context=lease_scope,
        )

        # The lease may have changed while the RPC was pending.
        if self._lease_context is not lease_scope:
            return
        if ok:
            logger.info("Released lease %s via ReleaseLease RPC", lease_scope.lease_name)
        elif not lease_scope.lease_ended.is_set():
            logger.warning("Failed to release lease %s, proceeding to AVAILABLE", lease_scope.lease_name)
        await self._report_status(ExporterStatus.AVAILABLE, "Exporter available after lease release")

        # The controller may not send another leased=False after our release
        # request, so end it locally as well.
        self._end_lease_locally(lease_scope)

    def _end_lease_locally(self, lease: LeaseContext) -> None:
        if not lease.lease_ended.is_set():
            lease.lease_ended.set()
            self._inputs.notify()

    async def _unregister_with_controller(self):
        """Safely unregister from controller with timeout and error handling."""
        if not (self._registered and self._unregister):
            return

        logger.info("Unregistering exporter with controller")
        try:
            with move_on_after(10):  # 10 second timeout
                channel = await self.channel_factory(self.token)
                try:
                    controller = jumpstarter_pb2_grpc.ControllerServiceStub(channel)
                    await self._report_status(ExporterStatus.OFFLINE, "Exporter shutting down")
                    await controller.Unregister(
                        jumpstarter_pb2.UnregisterRequest(
                            reason="Exporter shutdown",
                        )
                    )
                    logger.info("Controller unregistration completed successfully")
                finally:
                    with CancelScope(shield=True):
                        await channel.close()
        except Exception as e:  # noqa: BLE001
            logger.error("Error during controller unregistration: %s", e)

    @asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        try:
            yield self
        finally:
            try:
                await self._unregister_with_controller()
            except Exception:
                logger.exception("Error during exporter cleanup")
                # Don't re-raise to avoid masking the original exception

    async def _handle_client_conn(
        self, path: str, endpoint: str, token: str, tls_config: TLSConfigV1Alpha1, grpc_options: dict[str, Any] | None
    ) -> None:
        """Handle a single client connection by proxying between session and router.

        This method establishes a connection from the local session Unix socket to the
        router endpoint, creating a bidirectional proxy that allows the client to
        communicate with the device through the router infrastructure.

        Args:
            path: Unix socket path where the session is serving
            endpoint: Router endpoint URL to connect to
            token: Authentication token for the router connection
            tls_config: TLS configuration for secure router communication
            grpc_options: Optional gRPC channel options for the router connection

        Note:
            This is a private method spawned as a concurrent task by handle_lease_conn()
            for each incoming connection request. It runs until the client disconnects
            or an error occurs.
        """
        try:
            logger.debug("Connecting to session socket at %s", path)
            async with await connect_unix(path) as stream:
                logger.debug("Connected to session, bridging to router at %s", endpoint)
                async with connect_router_stream(endpoint, token, stream, tls_config, grpc_options):
                    logger.debug("Router stream established, forwarding traffic")
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to handle client connection: %s", e)

    @asynccontextmanager
    async def session(self):
        """Create and manage an exporter Session context for initial registration.

        Yields:
            tuple[Session, str]: A tuple of (session, socket_path) for use in lease handling.
        """
        with Session(
            uuid=self.uuid,
            labels=self.labels,
            exporter_name=self.exporter_name,
            root_device=self.device_factory(),
            motd=self.motd,
            # Registration only reads the device report. Resetting here could undo
            # the setup of a lease this process resumes.
            reset_device=False,
        ) as session:
            # Create a Unix socket
            async with session.serve_unix_async() as path:
                # Create a gRPC channel to the controller via the socket
                async with grpc.aio.secure_channel(
                    f"unix://{path}", grpc.local_channel_credentials(grpc.LocalConnectionType.UDS)
                ) as channel:
                    # Register the exporter with the controller
                    await self._register_with_controller(channel)
                # Yield both session and path for creating LeaseScope
                yield session, path

    @asynccontextmanager
    async def session_for_lease(self, reset_device: bool = True):
        """Create and manage an exporter Session context with separate hook socket.

        This creates two Unix sockets:
        - Main socket: For client gRPC connections (LogStream, driver calls, etc.)
        - Hook socket: For hook subprocess j commands (isolated to prevent SSL corruption)

        The separation prevents SSL frame corruption that occurs when multiple gRPC
        connections share the same socket simultaneously.

        Note: Registration with the controller is handled once during serve() via
        self.session(). Per-lease sessions do not re-register to avoid spurious
        status updates that can tear down the session prematurely.

        Yields:
            tuple[Session, str, str]: A tuple of (session, main_socket_path, hook_socket_path)
        """
        logger.info("Creating new session for lease")
        with Session(
            uuid=self.uuid,
            labels=self.labels,
            exporter_name=self.exporter_name,
            root_device=self.device_factory(),
            motd=self.motd,
            reset_device=reset_device,
        ) as session:
            # Create dual Unix sockets - one for clients, one for hooks
            async with session.serve_unix_with_hook_socket_async() as (main_path, hook_path):
                logger.info("Session serving on main=%s, hook=%s", main_path, hook_path)
                yield session, main_path, hook_path
        logger.info("Session closed")

    # ── Lifecycle ────────────────────────────────────────────────────────────
    #
    # One loop admits leases. Each lease is one call to _serve_lease, whose
    # session and client connections are `async with` blocks: leaving them
    # closes the lease's resources, on return, error or cancellation, before
    # the next lease can be admitted. Every wait goes through
    # InputBuffer.wait_until, so every decision reads live inputs.

    async def serve(self):
        """Serve the exporter, handling leases until stopped."""
        # Set exporter identity before anything else so every log line (including
        # registration and telemetry setup) carries the correct exporter name.
        set_log_context(exporter=self.exporter_name)
        async with self.session():
            pass
        try:
            async with create_task_group() as tg:
                self._tg = tg
                self._status_rpc_event = Event()
                self._pending_status_request = None
                self._status_drain_active = True
                tg.start_soon(self._drain_status_reports)
                self._start_telemetry_tasks(tg)
                tg.start_soon(partial(
                    self._retry_stream,
                    "Status",
                    self._status_stream_factory(),
                    _StatusSink(self),
                    backoff=0.5,
                    outage_budget=self.status_stream_retry_timeout,
                    on_reconnect=self._resend_status,
                ))
                tg.start_soon(self._token_refresh_loop)
                await self._lifecycle()
                tg.cancel_scope.cancel()
        finally:
            if self.exit_on_lease_end:
                # Ensure the runtime container exits whenever this exporter is
                # configured for ExitAndReplace (covers hook on_failure=exit and
                # other stop paths).
                await to_thread.run_sync(shutdown_runtime_sidecar)
            self._tg = None
            self._status_drain_active = False
            clear_log_context()

        # Flush any remaining telemetry entries before the process exits.
        if self._telemetry_handler is not None:
            logging.getLogger().removeHandler(self._telemetry_handler)
            await self._telemetry_handler.close_async()
            self._telemetry_handler = None
        if self._telemetry_channel is not None:
            await self._telemetry_channel.close()
            self._telemetry_channel = None
        self._metrics_stream = None
        self._telemetry_credential = None

    def _start_telemetry_tasks(self, tg: TaskGroup) -> None:
        """Start PushLogs flush, MetricsStream and token renewal next to the control-plane tasks."""
        if self._telemetry_handler is not None:
            tg.start_soon(self._telemetry_handler.flush_loop)
        if self._metrics_stream is not None:
            tg.start_soon(self._metrics_stream.run)
        if self._telemetry_credential is not None:
            tg.start_soon(self._telemetry_credential.renew, self._get_service_endpoints)

    async def _lifecycle(self) -> None:
        inputs = await self._inputs.wait_until(lambda i: i.status_seen or i.stop is not None)
        admitted, parked = None, None
        if inputs.stop is None:
            admitted, parked = await self._take_over_from_previous_process(inputs)
        while parked is None:
            inputs = await self._inputs.wait_until(
                lambda i, admitted=admitted: i.stop is not None
                or (i.assignment is not None and i.generation != admitted)
            )
            if inputs.stop is not None:
                break
            assert inputs.assignment is not None
            admitted = inputs.generation
            a = inputs.assignment
            lease = LeaseContext(
                lease_name=a.lease_name,
                lease_uid=a.lease_uid,
                before_lease_hook=Event(),
                client_name=a.client_name,
                # Hook records identify a lease by its UID, which every controller that keeps them sends.
                hooks=HookPlan(tracked=self._tracks_lease_hooks and bool(a.lease_uid)),
            )
            parked = await self._serve_lease(lease, Entry.NEW, self._lease_log_context(a))
        if parked is not None:
            await self._park(parked)
        self._phase.move(Phase.STOPPING)
        stop = self._inputs.snapshot().stop
        self._unregister = stop.unregister if stop is not None else False

    async def _take_over_from_previous_process(self, inputs: Inputs) -> tuple[int | None, str | None]:
        """Pick up what a previous exporter process left of its last lease, before anything else.

        The controller's hook record names that lease, and decide_restart says
        what it still needs. A lease that needs hooks goes through _serve_lease
        like any other, with an Entry that skips the steps that already ran.
        Returns the assignment generation this handled, so the loop does not
        admit that lease again (None if the assigned lease, if any, is new),
        and why to park, as _serve_lease does.
        """
        record = inputs.hook_record
        a = inputs.assignment
        assigned = a.key if a is not None else None
        restart = decide_restart(record, assigned) if self._tracks_lease_hooks and record is not None else None
        if restart in (None, Restart.NEW, Restart.NOTHING):
            if restart is Restart.NEW and record is not None and record.HasField("before_lease"):
                logger.info(
                    "Not finishing hooks of lease %s (uid %s): lease %s (uid %s) is assigned now",
                    record.lease_name, record.lease_uid, *(assigned or ("", "")),
                )
            if self._records_lease_hooks and a is None:
                # Reported once the record is checked: it tells the controller no hook work is owed.
                await self._report_status(ExporterStatus.AVAILABLE, "Exporter registered and available")
            return None, None
        assert record is not None
        before = record.before_lease
        after = record.after_lease if record.HasField("after_lease") else None
        lease = LeaseContext(
            lease_name=record.lease_name,
            lease_uid=record.lease_uid,
            before_lease_hook=Event(),
            hooks=HookPlan(tracked=True, recorded=True),
            client_name=record.client_name,  # kept by the record, as the lease may have ended
        )
        handled = inputs.generation if assigned is not None else None
        if restart is Restart.SETTLE:
            await self._settle_lease_without_hooks(lease, before, after, assigned is not None)
            return handled, None
        if restart is Restart.FINISH_SETUP:
            logger.info("Resuming lease %s after exporter restart (beforeLease hook did not finish)", lease.lease_name)
            lease.hooks.finish_cut_off("before_lease", before, self._on_interrupt("before_lease"), lease.lease_name)
            entry = Entry.FINISH_SETUP
        elif restart is Restart.RESUME:
            logger.info(
                "Resuming lease %s after exporter restart (beforeLease hook %s before it)",
                lease.lease_name, LeaseHookPhase(before.phase).name.lower(),
            )
            lease.hooks.resume(before)
            entry = Entry.RESUME
        else:  # Restart.CLEAN_UP
            if before.phase not in FINISHED_HOOK_PHASES:
                # The lease ended before the cut-off setup could run again.
                await self._record_lease_hook(
                    lease, "before_lease", LeaseHookPhase.FAILED,
                    "beforeLease hook did not finish: the lease ended while the exporter was restarting",
                    attempts=max(before.attempts, 1),
                )
            if after is not None:
                lease.hooks.finish_cut_off("after_lease", after, self._on_interrupt("after_lease"), lease.lease_name)
            lease.hooks.resumed = (ExporterStatus.AFTER_LEASE_HOOK, "Running afterLease hooks")
            if assigned is None:
                logger.info("Lease %s ended while the exporter was down; running its afterLease hook", lease.lease_name)
                lease.lease_ended.set()
            else:
                logger.info("Lease %s owes its afterLease hook; running it", lease.lease_name)
            entry = Entry.CLEAN_UP
        parked = await self._serve_lease(lease, entry, {"lease_id": lease.lease_name, "exporter": self.exporter_name})
        return handled, parked

    async def _settle_lease_without_hooks(
        self,
        lease: LeaseContext,
        before: jumpstarter_pb2.LeaseHookState,
        after: jumpstarter_pb2.LeaseHookState | None,
        assigned: bool,
    ) -> None:
        """Settle a recorded lease that needs no more hooks.

        A setup that failed with endLease or exit skips cleanup, as without a
        restart. A lease still assigned is never set up again, and is released
        only if a hook failed with onFailure: endLease, as the previous process
        would have done; otherwise its client or its expiry ends it.
        """
        if after is None:
            await self._record_lease_hook(lease, "after_lease", LeaseHookPhase.SKIPPED, "beforeLease hook failed")
        if not assigned:
            # Reported once the record is checked: it tells the controller no hook work is owed.
            await self._report_status(ExporterStatus.AVAILABLE, "Exporter registered and available")
            return
        self._lease_context = lease  # routes the release and status below to this lease
        try:
            if _hook_failure_ended_lease(before) or _hook_failure_ended_lease(after):
                logger.info("Lease %s had a hook fail with onFailure: endLease; releasing it", lease.lease_name)
                await self._request_lease_release(lease)  # reports AVAILABLE once released
            else:
                logger.info("Lease %s is done with its hooks; leaving it to its client", lease.lease_name)
                await self._report_status(ExporterStatus.AVAILABLE, "Available for new lease")
        finally:
            self._lease_context = None

    def _lease_log_context(self, assignment: Assignment) -> dict[str, str]:
        """Log context for a newly-assigned lease: its name, this exporter, and the lease's spec.context."""
        return {"lease_id": assignment.lease_name, "exporter": self.exporter_name, **dict(assignment.context)}

    async def _serve_lease(self, lease: LeaseContext, entry: "Entry", log_context: dict[str, str]) -> str | None:
        """Serve one lease, from setup to the controller releasing it.

        Returns why to park: the afterLease hook failed unexpectedly, so the
        device state is unknown. None once the device is ready for the next lease.
        """
        self._lease_context = lease
        set_log_context(**log_context)
        if lease.client_name:
            set_log_context(client=lease.client_name)
        logger.info("Starting %s lease: %s", entry.name.lower().replace("_", " "), lease.lease_name)
        if self.exit_on_lease_end:
            # This is the last lease: drain now, so nothing reports AVAILABLE again.
            self._inputs.request_stop(now=False, unregister=True)
        try:
            self._phase.move(Phase.SETUP)
            # A lease an earlier process set up keeps the device as that process left it.
            async with self.session_for_lease(reset_device=lease.hooks.resumed is None) as (
                session, main_path, hook_path,
            ):
                lease.session = session
                lease.socket_path = main_path
                lease.hook_socket_path = hook_path
                lease.notify = self._inputs.notify
                session.lease_context = lease
                session.update_status(lease.current_status, lease.status_message)
                parked = await self._lease_activity(lease, entry, connections=True)
        finally:
            lease.before_lease_hook.set()
            self._lease_context = None
            clear_log_context()
            set_log_context(exporter=self.exporter_name)
            self._phase.move(Phase.IDLE)
        if parked is not None:
            return parked
        # Ready for the next lease. This also clears a failure status a lease
        # ended with (an endLease failure the controller ended it for); a stopping
        # exporter does not report it (_report_status).
        if self._exporter_status != ExporterStatus.AVAILABLE:
            await self._report_status(ExporterStatus.AVAILABLE, "Available for new lease")
        return None

    async def _park(self, reason: str) -> None:
        """Admit no lease until restarted: the afterLease hook failed unexpectedly.

        The device state is unknown, so the next lease could start on a board in
        any state. The failure status stays, which keeps the controller from
        assigning this exporter. A restart (after checking the device) clears it.
        """
        self._phase.move(Phase.PARKED)
        logger.error(
            "Not admitting leases: the afterLease hook failed unexpectedly (%s). "
            "Check the device, then restart the exporter.", reason,
        )
        await self._report_status(
            ExporterStatus.AFTER_LEASE_HOOK_FAILED,
            f"afterLease hook failed: {reason}; not admitting leases until the exporter restarts",
        )
        await self._inputs.wait_until(lambda i: i.stop is not None)

    async def _lease_activity(self, lease: LeaseContext, entry: "Entry", *, connections: bool) -> str | None:
        """Set up, serve and clean up one lease, then hold it until it is released.

        Returns why to park: the afterLease hook failed unexpectedly. Cancelled
        (a fatal stream error, or a forced stop), it still cleans up what setup
        left owed, within the stop grace.
        """
        if lease.lease_ended.is_set() and entry is Entry.NEW:
            logger.info("Lease %s already ended, skipping", lease.lease_name)
            await self._report_status(ExporterStatus.AVAILABLE, "Available for new lease")
            return None

        setup = SetupOutcome.CLEAN_UP
        try:
            # A lease that ended while no process served it has no clients to connect.
            async with self._lease_connections(lease, enabled=connections and not lease.lease_ended.is_set()):
                if lease.hooks.resumed is not None:
                    # An earlier process ran this lease's setup: say what it said, run nothing.
                    await self._report_status(*lease.hooks.resumed)
                    lease.before_lease_hook.set()
                else:
                    setup = await self._run_setup(lease)
                if setup is SetupOutcome.NO_CLEANUP:
                    await self._hold_without_cleanup(lease)
                    return None
                if entry is Entry.CLEAN_UP and not lease.lease_ended.is_set():
                    # Owed cleanup of a lease still assigned: finish it as after EndSession.
                    return await self._clean_up_and_hold(lease)
                if not lease.lease_ended.is_set():
                    self._phase.move(Phase.SERVING)
                inputs = await self._inputs.wait_until(
                    lambda i: lease.lease_ended.is_set() or i.stop_now or lease.end_session_requested.is_set()
                )
                if lease.end_session_requested.is_set() and not lease.lease_ended.is_set() and not inputs.stop_now:
                    # The client asked to end its session: clean up while it is still
                    # connected, so it receives the afterLease output.
                    logger.info("EndSession requested, running afterLease hook")
                    return await self._clean_up_and_hold(lease)
                if inputs.stop_now and lease.hooks.tracked and not lease.lease_ended.is_set():
                    # A restart is a process event, not a lease event: the lease and its
                    # device stay as they are, and the next process resumes it from the record.
                    logger.info("Stopping: leaving lease %s to the next exporter process", lease.lease_name)
                    return None

            # Released, revoked or stopping: client connections are closed before
            # cleanup touches the device.
            return await self._clean_up_and_hold(lease)
        except get_cancelled_exc_class():
            if setup is SetupOutcome.CLEAN_UP:
                await self._cleanup_on_cancel(lease)
            raise

    async def _hold_without_cleanup(self, lease: LeaseContext) -> None:
        """Hold a lease whose setup leaves no cleanup, keeping its failure status until release.

        The record's cleanup is closed first, or the controller holds this exporter for it.
        """
        if lease.hooks.recorded:
            await self._record_lease_hook(lease, "after_lease", LeaseHookPhase.SKIPPED, "beforeLease hook failed")
        await self._hold(lease)

    async def _clean_up_and_hold(self, lease: LeaseContext) -> str | None:
        unknown_state = await self._run_cleanup(lease)
        await self._hold(lease)
        return unknown_state

    async def _hold(self, lease: LeaseContext) -> None:
        """Keep the lease's session until the controller releases the lease, or a stop."""
        self._phase.move(Phase.HOLDING)
        await self._inputs.wait_until(lambda i: lease.lease_ended.is_set() or i.stop_now)

    async def _run_setup(self, lease: LeaseContext) -> SetupOutcome:
        if not self.hook_executor:
            await self._report_status(ExporterStatus.LEASE_READY, "Ready for commands")
            lease.before_lease_hook.set()
            return SetupOutcome.CLEAN_UP
        async with self._stoppable("beforeLease hook"):
            return await self.hook_executor.run_before_lease_hook(
                lease, self._report_status, self.stop, self._request_lease_release,
                record_hook=partial(self._record_lease_hook, lease),
            )
        return SetupOutcome.CLEAN_UP  # cut off by the stop grace: cleanup still runs

    async def _run_cleanup(self, lease: LeaseContext) -> str | None:
        """Run the afterLease hook once. Returns why the device state is unknown, if it is."""
        if lease.after_lease_hook_started.is_set():
            return None
        lease.after_lease_hook_started.set()
        self._phase.move(Phase.CLEANUP)
        if self.hook_executor and (lease.has_client() or self._standalone):
            logger.info("Running afterLease hook")
            async with self._stoppable("afterLease hook"):
                return await self.hook_executor.run_after_lease_hook(
                    lease, self._report_status, self.stop, self._request_lease_release,
                    record_hook=partial(self._record_lease_hook, lease),
                )
            return None  # cut off by the stop grace
        if lease.hooks.tracked:
            await self._record_lease_hook(lease, "after_lease", LeaseHookPhase.SKIPPED, "no client used the lease")
        await self._report_status(ExporterStatus.AVAILABLE, "Available for new lease")
        return None

    async def _cleanup_on_cancel(self, lease: LeaseContext) -> None:
        """The activity was cancelled (fatal stream error, or a forced stop).

        A forced stop skips cleanup. So does a lease whose hooks are recorded:
        the next process owes it, from the record. Otherwise the afterLease hook
        still runs, bounded by stop_grace, as it did when the exporter shut down
        mid-lease.
        """
        if self._forced or lease.hooks.tracked or lease.after_lease_hook_started.is_set():
            return
        with CancelScope(shield=True, deadline=anyio.current_time() + self.stop_grace):
            await self._run_cleanup(lease)

    @asynccontextmanager
    async def _stoppable(self, what: str) -> AsyncGenerator[None, None]:
        """Run a hook, or a record write, so that a stop-now cuts it off after stop_grace."""
        with CancelScope() as scope:
            async with create_task_group() as tg:

                async def arm() -> None:
                    inputs = await self._inputs.wait_until(lambda i: i.stop_now)
                    assert inputs.stop_now_at is not None
                    scope.deadline = inputs.stop_now_at + self.stop_grace
                    left = scope.deadline - anyio.current_time()
                    logger.info("Stop requested: %s may run %.1fs more", what, max(left, 0.0))

                tg.start_soon(arm)
                try:
                    yield
                finally:
                    tg.cancel_scope.cancel()
        if scope.cancelled_caught:
            logger.warning("%s cut off after the %.1fs stop grace", what, self.stop_grace)

    @asynccontextmanager
    async def _lease_connections(self, lease: LeaseContext, *, enabled: bool) -> AsyncGenerator[None, None]:
        """Bridge client dials for this lease while the block is open."""
        if not enabled:
            yield
            return
        listen_tx, listen_rx = create_memory_object_stream[jumpstarter_pb2.ListenResponse](max_buffer_size=10)
        async with listen_tx, listen_rx, create_task_group() as tg:
            tg.start_soon(self._retry_stream, "Listen", self._listen_stream_factory(lease.lease_name), listen_tx)

            async def bridge() -> None:
                # Dials buffered during setup are bridged only once setup is done,
                # and never for a lease that has already ended.
                await lease.before_lease_hook.wait()
                async for request in listen_rx:
                    if lease.lease_ended.is_set():
                        break
                    logger.info(
                        "Handling new connection request on lease %s (router=%s)",
                        lease.lease_name,
                        request.router_endpoint,
                    )
                    tg.start_soon(
                        self._handle_client_conn,
                        lease.socket_path,
                        request.router_endpoint,
                        request.router_token,
                        self.tls,
                        self.grpc_options,
                    )

            tg.start_soon(bridge)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()

    async def serve_standalone_tcp(
        self,
        host: str,
        port: int,
        *,
        tls_credentials: grpc.ServerCredentials | None = None,
        interceptors: list | None = None,
    ) -> None:
        """Serve the exporter on a TCP address without a controller (standalone mode).

        One session is served on host:port (and a temporary Unix socket for hook
        j commands) as a single lease that is never released: setup, serving
        and cleanup run exactly as for a controller lease, until stop().
        """
        self._standalone = True
        lease = LeaseContext(lease_name="standalone", before_lease_hook=Event(), notify=self._inputs.notify)
        self._lease_context = lease
        set_log_context(exporter=self.exporter_name, lease_id="standalone")

        try:
            with TemporarySocket() as hook_path:
                hook_path_str = str(hook_path)
                with Session(
                    uuid=self.uuid,
                    labels=self.labels,
                    exporter_name=self.exporter_name,
                    root_device=self.device_factory(),
                    motd=self.motd,
                ) as session:
                    session.lease_context = lease
                    lease.session = session
                    lease.socket_path = hook_path_str
                    lease.hook_socket_path = hook_path_str

                    async with session.serve_tcp_and_unix_async(
                        host, port, hook_path_str,
                        tls_credentials=tls_credentials,
                        interceptors=interceptors,
                    ), create_task_group() as tg:
                        self._tg = tg
                        self._phase.move(Phase.SETUP)
                        # Standalone serves one lease until stopped: there is nothing to park.
                        await self._lease_activity(lease, Entry.NEW, connections=False)
                        tg.cancel_scope.cancel()
        finally:
            self._phase.move(Phase.IDLE)
            self._lease_context = None
            self._tg = None


class _StatusSink:
    """Where the Status stream writes: the input buffer, plus lease-local flags."""

    def __init__(self, exporter: Exporter) -> None:
        self._exporter = exporter

    async def send(self, status: jumpstarter_pb2.StatusResponse) -> None:
        inputs: Inputs = self._exporter._inputs.apply_status(status)
        lease = self._exporter._lease_context
        if lease is None:
            if not status.leased:
                logger.info("Currently not leased")
            return
        current = inputs.assignment
        same = current is not None and current.lease_name == lease.lease_name and (
            not lease.lease_uid or not current.lease_uid or current.lease_uid == lease.lease_uid
        )
        if current is not None and same:
            if current.client_name != lease.client_name:
                lease.update_client(current.client_name)
            logger.info("Currently leased by %s under %s", current.client_name, current.lease_name)
        elif not lease.lease_ended.is_set():
            if current is None:
                logger.info("Lease %s ended", lease.lease_name)
            else:
                logger.warning(
                    "Controller reassigned exporter from lease %s to %s; ending current lease",
                    lease.lease_name,
                    current.lease_name,
                )
            self._exporter._end_lease_locally(lease)
