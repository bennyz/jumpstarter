"""Black-box lease lifecycle tests.

Each test runs a real Exporter against a fake controller over real gRPC and
real hook subprocesses, and asserts only on what the outside can observe: the
statuses the controller receives, the RPCs it receives, the hook log, and when
serve() returns. Nothing here reaches into exporter internals except the
session socket path that EndSession needs, so the file runs unchanged against
different lifecycle implementations.
"""

import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address
from pathlib import Path
from typing import Literal

import anyio
import grpc
import pytest
from anyio import create_memory_object_stream, create_task_group, fail_after
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jumpstarter_protocol import jumpstarter_pb2, jumpstarter_pb2_grpc, router_pb2_grpc

from jumpstarter.common import ExporterStatus, LeaseHookPhase
from jumpstarter.config.exporter import HookConfigV1Alpha1, HookInstanceConfigV1Alpha1
from jumpstarter.driver import Driver
from jumpstarter.exporter.exporter import Exporter
from jumpstarter.exporter.hooks import HookExecutor

pytestmark = pytest.mark.anyio

S = ExporterStatus


class _Device(Driver):
    @classmethod
    def client(cls):
        return "jumpstarter.client.DriverClient"


def _self_signed() -> tuple[bytes, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(tz=UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([]))
        .issuer_name(x509.Name([]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(IPv4Address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem_key = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    )
    return pem_key, cert.public_bytes(serialization.Encoding.PEM)


@dataclass(kw_only=True)
class FakeRouter(router_pb2_grpc.RouterServiceServicer):
    """Records when the exporter opens and closes the router stream for each dial."""

    opened: dict[str, float] = field(default_factory=dict)
    closed: dict[str, float] = field(default_factory=dict)

    async def Stream(self, request_iterator, context):  # ty: ignore[invalid-method-override]
        authorization = dict(context.invocation_metadata()).get("authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        self.opened[token] = time.time()
        try:
            async for _ in request_iterator:
                pass
            await anyio.sleep_forever()
        finally:
            self.closed[token] = time.time()


@dataclass(kw_only=True)
class FakeController(jumpstarter_pb2_grpc.ControllerServiceServicer):
    """Records what the exporter tells the controller; the test drives Status."""

    reports: list[tuple[float, ExporterStatus, str]] = field(default_factory=list)
    releases: list[str] = field(default_factory=list)
    registered: int = 0
    unregistered: int = 0
    release_ends_lease: bool = True
    router_endpoint: str = ""
    records: bool = False
    """Keep a lease hook record (#1208): Status carries it, UpdateLeaseHooks writes it."""
    record: jumpstarter_pb2.LeaseHooks = field(default_factory=jumpstarter_pb2.LeaseHooks)
    record_writes: list[tuple[str, str, LeaseHookPhase, int]] = field(default_factory=list)
    assigned: tuple[str, str, str] | None = None  # (lease, uid, client)
    down: grpc.StatusCode | None = None
    """While set, every call fails with this code, and the open Status stream ends with it."""

    def __post_init__(self):
        self._status_tx, self._status_rx = create_memory_object_stream[jumpstarter_pb2.StatusResponse | None](64)
        self._dial_tx, self._dial_rx = create_memory_object_stream[str](64)

    def _status(self) -> jumpstarter_pb2.StatusResponse:
        status = jumpstarter_pb2.StatusResponse(leased=False)
        if self.assigned is not None:
            lease, uid, client = self.assigned
            status = jumpstarter_pb2.StatusResponse(leased=True, lease_name=lease, client_name=client)
            if self.records:
                status.lease_uid = uid
        if self.records:
            status.lease_hooks.CopyFrom(self.record)
        return status

    # --- driving the exporter -------------------------------------------------
    def assign(self, lease: str, client: str = "client-a") -> None:
        self.assigned = (lease, f"uid-{lease}", client)
        self._status_tx.send_nowait(self._status())

    def unassign(self) -> None:
        self.assigned = None
        self._status_tx.send_nowait(self._status())

    def hook_phase(self, hook: Literal["before_lease", "after_lease"]) -> LeaseHookPhase | None:
        return LeaseHookPhase(getattr(self.record, hook).phase) if self.record.HasField(hook) else None

    def dial(self, token: str) -> None:
        """A client dials the current lease; the exporter hears it on Listen."""
        self._dial_tx.send_nowait(token)

    def go_down(self, code: grpc.StatusCode = grpc.StatusCode.UNAVAILABLE) -> None:
        self.down = code
        self._status_tx.send_nowait(None)  # ends the open Status stream

    def come_back(self) -> None:
        self.down = None

    async def _fail_if_down(self, context) -> None:
        if self.down is not None:
            await context.abort(self.down, "controller unavailable")

    # --- observing -----------------------------------------------------------
    def statuses(self) -> list[ExporterStatus]:
        return [s for _, s, _ in self.reports]

    def statuses_since(self, status: ExporterStatus, nth: int = 0) -> list[ExporterStatus]:
        """Statuses after the nth occurrence of `status` (inclusive)."""
        seen = [i for i, s in enumerate(self.statuses()) if s == status]
        assert len(seen) > nth, f"{status} reported {len(seen)} times: {self.statuses()}"
        return self.statuses()[seen[nth]:]

    async def wait_for(self, status: ExporterStatus, count: int = 1, timeout: float = 10) -> None:
        with fail_after(timeout):
            while self.statuses().count(status) < count:
                await anyio.sleep(0.01)

    # --- ControllerService ---------------------------------------------------
    async def Register(self, request, context):
        await self._fail_if_down(context)
        self.registered += 1
        return jumpstarter_pb2.RegisterResponse(uuid="00000000-0000-0000-0000-000000000000")

    async def Unregister(self, request, context):
        self.unregistered += 1
        return jumpstarter_pb2.UnregisterResponse()

    async def ReportStatus(self, request, context):
        await self._fail_if_down(context)
        self.reports.append((time.monotonic(), S.from_proto(request.status), request.message))
        return jumpstarter_pb2.ReportStatusResponse()

    async def ReleaseLease(self, request, context):
        self.releases.append(request.name)
        if self.release_ends_lease:
            self.unassign()
        return jumpstarter_pb2.ReleaseLeaseResponse()

    async def GetServiceEndpoints(self, request, context):
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "no telemetry")

    async def UpdateLeaseHooks(self, request, context):
        """The controller's record rules, as in #1208: only the assigned lease starts setup,
        an outcome must match the running attempt, a new lease replaces the record, and
        a failure with onFailure: endLease ends the assigned lease."""
        await self._fail_if_down(context)
        if not self.records:
            await context.abort(grpc.StatusCode.UNIMPLEMENTED, "no hook records")
        hook = "before_lease" if request.HasField("before_lease") else "after_lease"
        state = getattr(request, hook)
        phase = LeaseHookPhase(state.phase)
        assigned = self.assigned is not None and self.assigned[:2] == (request.lease_name, request.lease_uid)
        starting = phase in (LeaseHookPhase.RUNNING, LeaseHookPhase.SKIPPED)
        if hook == "before_lease" and starting and not assigned:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "lease is not assigned")
        if request.lease_uid != self.record.lease_uid:
            if not (hook == "before_lease" and starting and assigned):
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "no record of this lease")
            assert self.assigned is not None
            self.record = jumpstarter_pb2.LeaseHooks(
                lease_name=request.lease_name, lease_uid=request.lease_uid, client_name=self.assigned[2]
            )
        current = getattr(self.record, hook) if self.record.HasField(hook) else None
        if phase in (LeaseHookPhase.SUCCEEDED, LeaseHookPhase.FAILED) and current is not None and (
            current.phase != LeaseHookPhase.RUNNING or current.attempts != state.attempts
        ):
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "outcome of an attempt that is not running")
        getattr(self.record, hook).CopyFrom(state)
        self.record_writes.append((request.lease_name, hook, phase, state.attempts))
        if assigned and phase == LeaseHookPhase.FAILED and (
            state.on_failure == jumpstarter_pb2.LEASE_HOOK_FAILURE_ACTION_END_LEASE
        ):
            self.unassign()
        return self.record

    async def Status(self, request, context):
        await self._fail_if_down(context)
        yield self._status()  # the current state as the stream opens, as the controller does
        async for status in self._status_rx:
            if status is None:  # go_down(); one queued before this stream opened is stale
                await self._fail_if_down(context)
                continue
            if self.records:  # always the latest record, whenever the message was queued
                status.lease_hooks.CopyFrom(self.record)
            yield status

    async def Listen(self, request, context):
        async for token in self._dial_rx:
            yield jumpstarter_pb2.ListenResponse(router_endpoint=self.router_endpoint, router_token=token)


def hook(
    log: Path, tag: str, *, sleep: float = 0, exit_code: int = 0, on_failure="warn", timeout=60,
    sleep_first: float = 0, on_interrupt="rerun",
):
    """A hook that logs its run. sleep_first: sleep this long on its first run only."""
    stamp = log.parent / f"{tag}.start"
    first = log.parent / f"{tag}.first"
    once = f"if [ ! -e {first} ]; then touch {first}; sleep {sleep_first}; fi; " if sleep_first else ""
    script = f'date +%s.%N > {stamp}; echo "{tag} $LEASE_NAME" >> {log}; {once}sleep {sleep}; exit {exit_code}'
    return HookInstanceConfigV1Alpha1(
        script=script, timeout=timeout, on_failure=on_failure, on_interrupt=on_interrupt,
    )


def session_socket(exporter: Exporter) -> str:
    """The current lease's client socket; the one place that knows an implementation's internals."""
    lease = getattr(exporter, "_lease_context", None)
    fsm = getattr(exporter, "fsm", None)  # the typed-FSM variant (#1273)
    if lease is None and fsm is not None and fsm.runner is not None:
        lease = getattr(fsm.runner.current_state, "lease", None)
    assert lease is not None and lease.socket_path, "no lease session"
    return lease.socket_path


@dataclass
class Harness:
    controller: FakeController
    router: FakeRouter
    exporter: Exporter
    log: Path
    done: anyio.Event
    returned_at: float | None = None

    def started_at(self, tag: str) -> float:
        return float((self.log.parent / f"{tag}.start").read_text())

    async def wait_opened(self, token: str, timeout: float = 10) -> None:
        with fail_after(timeout):
            while token not in self.router.opened:
                await anyio.sleep(0.01)

    def hook_runs(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    async def wait_returned(self, timeout: float) -> float:
        """Wait for serve() to return; returns how long it took."""
        start = time.monotonic()
        with fail_after(timeout):
            await self.done.wait()
        return time.monotonic() - start


@asynccontextmanager
async def running(
    tmp_path, *, before=None, after=None, exit_on_lease_end=False, stop_grace=None, records=False, controller=None,
    status_stream_retry_timeout=None, wait_ready=True,
):
    """One exporter process against a fake controller. Pass a previous run's
    controller to start the next process of the same exporter against it.
    wait_ready=False yields as soon as the exporter starts, before it registers."""
    reused = controller is not None
    controller = controller or FakeController(records=records)
    registered_before = controller.registered
    router = FakeRouter()
    server = grpc.aio.server()
    port = server.add_insecure_port("127.0.0.1:0")
    jumpstarter_pb2_grpc.add_ControllerServiceServicer_to_server(controller, server)
    key, cert = _self_signed()
    router_server = grpc.aio.server()
    router_port = router_server.add_secure_port(
        "127.0.0.1:0", grpc.ssl_server_credentials(private_key_certificate_chain_pairs=[(key, cert)])
    )
    router_pb2_grpc.add_RouterServiceServicer_to_server(router, router_server)
    controller.router_endpoint = f"127.0.0.1:{router_port}"
    await server.start()
    await router_server.start()
    # The router's certificate is self-signed: trust whatever it presents.
    saved_insecure = os.environ.get("JUMPSTARTER_GRPC_INSECURE")
    os.environ["JUMPSTARTER_GRPC_INSECURE"] = "1"

    async def channel_factory(token: str = "") -> grpc.aio.Channel:  # older exporters pass no token
        return grpc.aio.insecure_channel(f"127.0.0.1:{port}")

    executor = None
    if before or after:
        executor = HookExecutor(config=HookConfigV1Alpha1(before_lease=before, after_lease=after))
    exporter = Exporter(
        channel_factory=channel_factory,
        device_factory=_Device,
        hook_executor=executor,
        exit_on_lease_end=exit_on_lease_end,
        exporter_name="lifecycle-test",
    )
    if stop_grace is not None:
        exporter.stop_grace = stop_grace
    if status_stream_retry_timeout is not None:
        exporter.status_stream_retry_timeout = status_stream_retry_timeout
    h = Harness(controller, router, exporter, tmp_path / "hooks.log", anyio.Event())

    async def serve():
        try:
            async with exporter:  # as jmp run does: unregisters on exit
                await exporter.serve()
        finally:
            h.returned_at = time.monotonic()
            h.done.set()

    try:
        async with create_task_group() as tg:
            tg.start_soon(serve)
            if wait_ready and reused:  # a later process of the same exporter: it may resume a lease
                with fail_after(10):
                    while controller.registered <= registered_before:
                        await anyio.sleep(0.01)
            elif wait_ready:
                await controller.wait_for(S.AVAILABLE)  # registered and idle
            yield h
            tg.cancel_scope.cancel()
    finally:
        if saved_insecure is None:
            os.environ.pop("JUMPSTARTER_GRPC_INSECURE", None)
        else:
            os.environ["JUMPSTARTER_GRPC_INSECURE"] = saved_insecure
        await router_server.stop(grace=None)
        await server.stop(grace=None)


# ── the ordinary path ────────────────────────────────────────────────────────


async def test_lease_runs_setup_then_cleanup_and_returns_to_available(tmp_path):
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before"), after=hook(log, "after")) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        h.controller.unassign()
        await h.controller.wait_for(S.AVAILABLE, count=2)

        assert h.hook_runs() == ["before lease-a", "after lease-a"]
        assert h.controller.statuses_since(S.BEFORE_LEASE_HOOK) == [
            S.BEFORE_LEASE_HOOK, S.LEASE_READY, S.AFTER_LEASE_HOOK, S.AVAILABLE,
        ]
        assert h.controller.releases == []


async def test_consecutive_leases_run_hooks_in_order(tmp_path):
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before"), after=hook(log, "after", sleep=0.3)) as h:
        for n, lease in enumerate(["lease-a", "lease-b"], start=1):
            h.controller.assign(lease)
            await h.controller.wait_for(S.LEASE_READY, count=n)
            h.controller.unassign()
            await h.controller.wait_for(S.AVAILABLE, count=n + 1)
        assert h.hook_runs() == ["before lease-a", "after lease-a", "before lease-b", "after lease-b"]


async def test_lease_released_during_setup_is_never_ready_and_is_cleaned_up(tmp_path):
    """A release can only be acted on once it arrives, so a lease released
    right after assignment may still start setup. What is guaranteed: it never
    becomes LEASE_READY, and the device is cleaned up. (A lease released before
    the exporter admits it is never set up; see the teardown tests below.)"""
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before", sleep=1.0), after=hook(log, "after")) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.BEFORE_LEASE_HOOK)
        h.controller.unassign()
        await h.controller.wait_for(S.AVAILABLE, count=2)
        assert S.LEASE_READY not in h.controller.statuses()
        assert h.hook_runs() == ["before lease-a", "after lease-a"]


async def test_lease_without_a_client_skips_cleanup_hook(tmp_path):
    """An unused lease (it expired before any client connected) has nothing to clean up."""
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before"), after=hook(log, "after")) as h:
        h.controller.assign("lease-a", client="")
        await h.controller.wait_for(S.LEASE_READY)
        h.controller.unassign()
        await h.controller.wait_for(S.AVAILABLE, count=2)
        assert h.hook_runs() == ["before lease-a"]


async def test_end_session_runs_cleanup_once_while_still_leased(tmp_path):
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before"), after=hook(log, "after")) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        path = session_socket(h.exporter)
        async with grpc.aio.secure_channel(
            f"unix://{path}", grpc.local_channel_credentials(grpc.LocalConnectionType.UDS)
        ) as channel:
            await jumpstarter_pb2_grpc.ExporterServiceStub(channel).EndSession(jumpstarter_pb2.EndSessionRequest())
        with fail_after(10):
            while "after lease-a" not in h.hook_runs():
                await anyio.sleep(0.01)
        h.controller.unassign()
        await h.controller.wait_for(S.AVAILABLE, count=2)
        await anyio.sleep(0.5)
        assert h.hook_runs() == ["before lease-a", "after lease-a"]


# ── client connections ───────────────────────────────────────────────────────


async def test_client_connection_closes_on_release_before_cleanup_starts(tmp_path):
    """Cleanup may reset the device; a released client must not still be on it."""
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before"), after=hook(log, "after", sleep=0.5)) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        h.controller.dial("conn-1")
        await h.wait_opened("conn-1")
        h.controller.unassign()
        await h.controller.wait_for(S.AVAILABLE, count=2)
        assert "conn-1" in h.router.closed
        assert h.router.closed["conn-1"] <= h.started_at("after")


async def test_dial_buffered_during_setup_of_a_released_lease_is_never_bridged(tmp_path):
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before", sleep=1.0), after=hook(log, "after")) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.BEFORE_LEASE_HOOK)
        h.controller.dial("early")  # held until setup finishes
        await anyio.sleep(0.2)
        h.controller.unassign()
        await h.controller.wait_for(S.AVAILABLE, count=2)
        await anyio.sleep(0.5)
        assert "early" not in h.router.opened


async def test_end_session_keeps_the_client_connected_through_cleanup(tmp_path):
    """After EndSession the client stays connected to receive the afterLease output."""
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before"), after=hook(log, "after", sleep=0.5)) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        h.controller.dial("conn-1")
        await h.wait_opened("conn-1")
        path = session_socket(h.exporter)
        async with grpc.aio.secure_channel(
            f"unix://{path}", grpc.local_channel_credentials(grpc.LocalConnectionType.UDS)
        ) as channel:
            await jumpstarter_pb2_grpc.ExporterServiceStub(channel).EndSession(jumpstarter_pb2.EndSessionRequest())
        await h.controller.wait_for(S.AVAILABLE, count=2)
        assert "conn-1" not in h.router.closed
        h.controller.unassign()
        with fail_after(10):
            while "conn-1" not in h.router.closed:
                await anyio.sleep(0.01)


# ── hook failure policies ────────────────────────────────────────────────────


async def test_before_lease_end_lease_failure_releases_and_skips_cleanup(tmp_path):
    log = tmp_path / "hooks.log"
    before = hook(log, "before", exit_code=1, on_failure="endLease")
    async with running(tmp_path, before=before, after=hook(log, "after")) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.BEFORE_LEASE_HOOK_FAILED)
        await h.controller.wait_for(S.AVAILABLE, count=2)
        assert h.controller.releases == ["lease-a"]
        assert h.hook_runs() == ["before lease-a"]


async def test_before_lease_exit_failure_goes_offline_and_exits_after_release(tmp_path):
    log = tmp_path / "hooks.log"
    before = hook(log, "before", exit_code=1, on_failure="exit")
    async with running(tmp_path, before=before, after=hook(log, "after")) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.OFFLINE)
        h.controller.unassign()
        await h.wait_returned(10)
        # FAILED and OFFLINE are sent back to back; status reports coalesce, so
        # only the absence of AVAILABLE and the final OFFLINE are guaranteed.
        assert S.AVAILABLE not in h.controller.statuses_since(S.BEFORE_LEASE_HOOK)
        assert S.OFFLINE in h.controller.statuses_since(S.BEFORE_LEASE_HOOK)
        assert h.exporter.exit_code == 1
        assert h.hook_runs() == ["before lease-a"]


# ── an exporter that will not take another lease never says AVAILABLE ──────


async def test_exit_on_lease_end_never_reports_available_after_the_lease(tmp_path):
    """ExporterSet ExitAndReplace pods: AVAILABLE during teardown lets the
    controller bind a queued lease to a pod that is about to exit."""
    log = tmp_path / "hooks.log"
    async with running(tmp_path, after=hook(log, "after", sleep=0.5), exit_on_lease_end=True) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        h.controller.unassign()
        await h.wait_returned(10)
        assert h.hook_runs() == ["after lease-a"]
        assert S.AVAILABLE not in h.controller.statuses_since(S.LEASE_READY)


async def test_drain_requested_during_cleanup_suppresses_available(tmp_path):
    log = tmp_path / "hooks.log"
    async with running(tmp_path, after=hook(log, "after", sleep=1.0)) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        h.controller.unassign()
        await h.controller.wait_for(S.AFTER_LEASE_HOOK)
        h.exporter.stop(wait_for_lease_exit=True, should_unregister=True)
        await h.wait_returned(10)
        assert S.AVAILABLE not in h.controller.statuses_since(S.AFTER_LEASE_HOOK)


# ── stops take effect within a bound ─────────────────────────────────────────


async def test_stop_now_during_a_long_cleanup_hook_is_bounded(tmp_path):
    log = tmp_path / "hooks.log"
    async with running(tmp_path, after=hook(log, "after", sleep=30), stop_grace=1.0) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        h.controller.unassign()
        await h.controller.wait_for(S.AFTER_LEASE_HOOK)
        h.exporter.stop(should_unregister=True)
        took = await h.wait_returned(8)
        assert took < 5


async def test_stop_now_during_a_long_setup_hook_is_bounded(tmp_path):
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before", sleep=30), stop_grace=1.0) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.BEFORE_LEASE_HOOK)
        h.exporter.stop(should_unregister=True)
        took = await h.wait_returned(8)
        assert took < 5


async def test_second_stop_forces_an_immediate_exit(tmp_path):
    log = tmp_path / "hooks.log"
    async with running(tmp_path, after=hook(log, "after", sleep=30), stop_grace=30.0) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        h.controller.unassign()
        await h.controller.wait_for(S.AFTER_LEASE_HOOK)
        h.exporter.stop(should_unregister=True)
        await anyio.sleep(0.2)
        h.exporter.stop(should_unregister=True)
        took = await h.wait_returned(8)
        assert took < 3


async def test_second_stop_during_setup_forces_an_immediate_exit(tmp_path):
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before", sleep=30), stop_grace=30.0) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.BEFORE_LEASE_HOOK)
        h.exporter.stop(should_unregister=True)
        await anyio.sleep(0.2)
        h.exporter.stop(should_unregister=True)
        took = await h.wait_returned(8)
        assert took < 3


async def test_stop_now_while_idle_unregisters(tmp_path):
    async with running(tmp_path) as h:
        h.exporter.stop(should_unregister=True)
        await h.wait_returned(5)
        assert h.controller.unregistered == 1
        assert h.controller.statuses()[-1] == S.OFFLINE


# ── reassignment while a lease tears down ────────────────────────────────────


async def test_lease_assigned_and_released_during_teardown_is_never_served(tmp_path):
    """The controller assigns B while A's cleanup runs, then B is released
    (a cancelled CI pipeline). The exporter must not start B afterwards."""
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before"), after=hook(log, "after", sleep=1.0)) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        h.controller.unassign()
        await h.controller.wait_for(S.AFTER_LEASE_HOOK)
        h.controller.assign("lease-b")
        h.controller.unassign()
        await h.controller.wait_for(S.AVAILABLE, count=2)
        await anyio.sleep(1.0)
        assert "before lease-b" not in h.hook_runs()
        assert h.controller.statuses()[-1] == S.AVAILABLE


async def test_lease_assigned_during_teardown_is_served_after_it(tmp_path):
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before"), after=hook(log, "after", sleep=1.0)) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        h.controller.unassign()
        await h.controller.wait_for(S.AFTER_LEASE_HOOK)
        h.controller.assign("lease-b")
        await h.controller.wait_for(S.LEASE_READY, count=2)
        assert h.hook_runs() == ["before lease-a", "after lease-a", "before lease-b"]


async def test_latest_assignment_wins_over_an_intermediate_one(tmp_path):
    """B is assigned during A's teardown and replaced by C before A finishes:
    only C is served."""
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before"), after=hook(log, "after", sleep=1.0)) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        h.controller.unassign()
        await h.controller.wait_for(S.AFTER_LEASE_HOOK)
        h.controller.assign("lease-b")
        h.controller.unassign()
        h.controller.assign("lease-c")
        await h.controller.wait_for(S.LEASE_READY, count=2)
        await anyio.sleep(0.5)
        runs = h.hook_runs()
        assert "before lease-b" not in runs
        assert runs[-1] == "before lease-c"


# ── the device state is unknown: park ────────────────────────────────────────


def _fail_cleanup_unexpectedly(monkeypatch) -> None:
    """An error outside the hook's onFailure policy, as an exporter bug would raise.
    Nothing a hook script does produces one, so this is the one internal seam here."""
    execute = HookExecutor._execute_hook

    async def failing(self, hook_config, lease_scope, log_source):
        if hook_config is self.config.after_lease:
            raise RuntimeError("lost track of the device")
        return await execute(self, hook_config, lease_scope, log_source)

    monkeypatch.setattr(HookExecutor, "_execute_hook", failing)


async def test_unexpected_cleanup_failure_parks_the_exporter(tmp_path, monkeypatch):
    _fail_cleanup_unexpectedly(monkeypatch)
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before"), after=hook(log, "after")) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        h.controller.unassign()
        await h.controller.wait_for(S.AFTER_LEASE_HOOK_FAILED)
        h.controller.assign("lease-b")
        await anyio.sleep(1.0)
        assert h.hook_runs() == ["before lease-a"]
        assert S.AVAILABLE not in h.controller.statuses_since(S.AFTER_LEASE_HOOK_FAILED)
        h.exporter.stop(should_unregister=True)
        await h.wait_returned(5)


async def test_restart_clears_a_park(tmp_path, monkeypatch):
    _fail_cleanup_unexpectedly(monkeypatch)
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before"), after=hook(log, "after"), records=True) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        h.controller.unassign()
        await h.controller.wait_for(S.AFTER_LEASE_HOOK_FAILED)
        h.exporter.stop()
        await h.wait_returned(5)
    controller = h.controller
    available = controller.statuses().count(S.AVAILABLE)
    async with running(tmp_path, before=hook(log, "before"), after=hook(log, "after"), controller=controller) as h:
        await controller.wait_for(S.AVAILABLE, count=available + 1)
        controller.assign("lease-b")
        await controller.wait_for(S.LEASE_READY, count=2)
        assert h.hook_runs() == ["before lease-a", "before lease-b"]


# ── the controller ends a lease after a recorded endLease failure ────────────


async def test_controller_ends_the_lease_after_a_recorded_end_lease_failure(tmp_path):
    log = tmp_path / "hooks.log"
    before = hook(log, "before", exit_code=1, on_failure="endLease")
    async with running(tmp_path, before=before, after=hook(log, "after"), records=True) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.AVAILABLE, count=2)
        assert h.controller.releases == []  # the controller ended it, not a ReleaseLease call
        assert h.controller.assigned is None
        # The record owes no cleanup, or the controller would hold the exporter for it.
        assert h.controller.hook_phase("after_lease") == LeaseHookPhase.SKIPPED
        assert h.controller.statuses_since(S.BEFORE_LEASE_HOOK) == [
            S.BEFORE_LEASE_HOOK, S.BEFORE_LEASE_HOOK_FAILED, S.AVAILABLE,
        ]
        assert h.hook_runs() == ["before lease-a"]


# ── controller outages ───────────────────────────────────────────────────────


async def test_lease_keeps_running_through_a_controller_outage(tmp_path):
    """Client sessions go through the router, so they survive the controller
    going away: the lease keeps running past the outage budget."""
    log = tmp_path / "hooks.log"
    async with running(tmp_path, after=hook(log, "after"), status_stream_retry_timeout=0.5) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        h.controller.dial("conn-1")
        await h.wait_opened("conn-1")
        h.controller.go_down()
        await anyio.sleep(2.0)
        assert not h.done.is_set()
        assert "conn-1" not in h.router.closed
        h.controller.come_back()
        h.controller.unassign()
        await h.controller.wait_for(S.AVAILABLE, count=2, timeout=15)
        assert h.hook_runs() == ["after lease-a"]
        assert h.exporter.exit_code is None


async def test_idle_exporter_gives_up_after_the_outage_budget(tmp_path):
    async with running(tmp_path, status_stream_retry_timeout=0.5) as h:
        h.controller.go_down()
        await h.wait_returned(10)
        assert h.exporter.exit_code == 75
        assert h.controller.unregistered == 0


async def test_status_is_sent_again_once_the_controller_is_back(tmp_path):
    """A report can be lost during an outage, and the controller clears an
    Offline status when the exporter reconnects: say the current one again."""
    async with running(tmp_path) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        h.controller.go_down()
        await anyio.sleep(1.0)
        h.controller.come_back()
        await h.controller.wait_for(S.LEASE_READY, count=2, timeout=15)


async def test_losing_the_controller_during_a_hook_gives_it_the_stop_grace(tmp_path):
    """Rejected credentials stop the exporter as SIGTERM does, not by cancelling
    whatever is running: the hook gets stop_grace, as it would for a stop."""
    log = tmp_path / "hooks.log"
    async with running(tmp_path, before=hook(log, "before", sleep=30), stop_grace=1.0) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.BEFORE_LEASE_HOOK)
        h.controller.go_down(grpc.StatusCode.UNAUTHENTICATED)
        took = await h.wait_returned(8)
        assert 0.8 <= took < 5
        assert h.exporter.exit_code == 75


async def test_registration_waits_for_the_controller(tmp_path):
    controller = FakeController()
    controller.go_down()
    async with running(tmp_path, controller=controller, wait_ready=False) as h:
        await anyio.sleep(1.5)  # at least one failed attempt
        assert not h.done.is_set()
        controller.come_back()
        await controller.wait_for(S.AVAILABLE, timeout=10)
        assert controller.registered == 1


async def test_stop_while_waiting_to_register_exits_promptly(tmp_path):
    controller = FakeController()
    controller.go_down()
    async with running(tmp_path, controller=controller, wait_ready=False) as h:
        await anyio.sleep(0.5)
        h.exporter.stop(should_unregister=True)
        took = await h.wait_returned(5)
        assert took < 2
        assert controller.registered == 0
        assert controller.unregistered == 0
