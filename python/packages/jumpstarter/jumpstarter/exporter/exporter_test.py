"""Unit tests for exporter status reporting, lease release and helpers.

Lease lifecycle behaviour (hooks, release, reassignment, stops) is tested
black-box in lifecycle_test.py.
"""

import logging
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import pytest
from anyio import Event, create_task_group, fail_after

from jumpstarter.common import ExporterStatus
from jumpstarter.exporter.exporter import (
    _RPC_BACKOFF_BASE,
    _RPC_BACKOFF_CAP,
    _RPC_MAX_RETRIES,
    _RPC_TIMEOUT,
)
from jumpstarter.exporter.inputs import InputBuffer
from jumpstarter.exporter.lease_context import LeaseContext

pytestmark = pytest.mark.anyio


def make_lease_context(lease_name="test-lease", client_name="test-client"):
    ctx = LeaseContext(
        lease_name=lease_name,
        before_lease_hook=Event(),
        client_name=client_name,
    )
    mock_session = MagicMock()
    mock_session.context_log_source.return_value = nullcontext()
    ctx.session = mock_session
    ctx.socket_path = "/tmp/test_socket"
    ctx.hook_socket_path = "/tmp/test_hook_socket"
    return ctx


def _make_base_exporter(**overrides):
    """Shared exporter factory: initializes the init=False fields that status
    reporting and the release path touch."""
    from jumpstarter.exporter.exporter import Exporter

    defaults = {
        "_exporter_status": ExporterStatus.AVAILABLE,
        "_lease_context": None,
        "_inputs": InputBuffer(),
        "_forced": False,
        "stop_grace": 25.0,
        "_standalone": False,
        "_tg": None,
        "_registered": False,
        "_unregister": False,
        "_exit_code": None,
        "hook_executor": None,
        "exit_on_lease_end": False,
        "labels": {},
        "exporter_name": "test-exporter",
        "_status_drain_active": False,
        "_pending_status_request": None,
        "_status_rpc_event": Event(),
        "_fatal_stream_error": None,
        "_report_status": AsyncMock(),
        "_request_lease_release": AsyncMock(),
        "_telemetry_handler": None,
        "_telemetry_channel": None,
        "_metrics_stream": None,
    }
    defaults.update(overrides)
    exporter = Exporter.__new__(Exporter)
    for k, v in defaults.items():
        setattr(exporter, k, v)
    return exporter


def test_exporter_name_property_reads_exporter_name():
    exporter = _make_base_exporter()
    assert exporter.labels == {}
    assert exporter.name == "test-exporter"


def make_exporter(lease_ctx, hook_executor=None):
    return _make_base_exporter(
        _lease_context=lease_ctx,
        hook_executor=hook_executor,
    )


def _make_exporter_for_report_status():
    """Create an Exporter with real methods for testing gRPC error handling.

    Unlike the other factories, this restores the real _report_status and
    _request_lease_release so tests can verify retry logic and error paths.
    """
    from jumpstarter.exporter.exporter import Exporter

    exporter = _make_base_exporter()
    exporter._report_status = Exporter._report_status.__get__(exporter, Exporter)
    exporter._request_lease_release = Exporter._request_lease_release.__get__(exporter, Exporter)
    return exporter


def _setup_mock_controller_stub(exporter, side_effect=None):
    """Helper to set up mock controller stub for testing ReportStatus.

    Returns:
        tuple: (mock_controller, stub_context_manager)
    """
    mock_controller = AsyncMock()
    if side_effect is not None:
        mock_controller.ReportStatus = AsyncMock(side_effect=side_effect)

    stub_ctx = AsyncMock()
    stub_ctx.__aenter__ = AsyncMock(return_value=mock_controller)
    stub_ctx.__aexit__ = AsyncMock(return_value=False)

    return mock_controller, stub_ctx


class TestReportStatusGrpcErrorHandling:
    async def test_unimplemented_grpc_error_logs_warning(self, caplog):
        """When ReportStatus returns UNIMPLEMENTED, a warning is logged
        instead of an error."""
        exporter = _make_exporter_for_report_status()

        error = grpc.aio.AioRpcError(
            code=grpc.StatusCode.UNIMPLEMENTED,
            initial_metadata=grpc.aio.Metadata(),
            trailing_metadata=grpc.aio.Metadata(),
            details="Method not implemented",
        )
        mock_controller, stub_ctx = _setup_mock_controller_stub(exporter, side_effect=error)

        with (
            patch.object(exporter, "_controller_stub", return_value=stub_ctx),
            caplog.at_level(logging.WARNING, logger="jumpstarter.exporter.exporter"),
        ):
            await exporter._report_status(ExporterStatus.AVAILABLE, "test")

        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("ReportStatus not supported" in r.message for r in warning_msgs), (
            f"Expected warning about ReportStatus not supported, got: {[r.message for r in caplog.records]}"
        )
        # Ensure no ERROR-level log was emitted
        error_msgs = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_msgs) == 0, (
            f"No error should be logged for UNIMPLEMENTED, got: {[r.message for r in error_msgs]}"
        )
        # Ensure no retry - UNIMPLEMENTED should fail fast
        assert mock_controller.ReportStatus.call_count == 1

    async def test_other_grpc_error_logs_error(self, caplog):
        """When ReportStatus returns a gRPC error other than UNIMPLEMENTED,
        it retries and eventually logs at ERROR level."""
        exporter = _make_exporter_for_report_status()

        error = grpc.aio.AioRpcError(
            code=grpc.StatusCode.UNAVAILABLE,
            initial_metadata=grpc.aio.Metadata(),
            trailing_metadata=grpc.aio.Metadata(),
            details="Service unavailable",
        )
        mock_controller, stub_ctx = _setup_mock_controller_stub(exporter, side_effect=error)

        with (
            patch.object(exporter, "_controller_stub", return_value=stub_ctx),
            patch("anyio.sleep") as mock_sleep,
            caplog.at_level(logging.DEBUG, logger="jumpstarter.exporter.exporter"),
        ):
            await exporter._report_status(ExporterStatus.AVAILABLE, "test")

        # Should log retry warnings (_RPC_MAX_RETRIES)
        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        retry_warnings = [r for r in warning_msgs if "Transient error" in r.message]
        assert len(retry_warnings) == _RPC_MAX_RETRIES, (
            f"Expected {_RPC_MAX_RETRIES} retry warnings, got: {len(retry_warnings)}"
        )

        # Verify exponential backoff with cap at _RPC_BACKOFF_CAP
        backoff_values = [call.args[0] for call in mock_sleep.call_args_list]
        expected_backoffs = [min(_RPC_BACKOFF_BASE * (2**i), _RPC_BACKOFF_CAP) for i in range(_RPC_MAX_RETRIES)]
        assert backoff_values == expected_backoffs, f"Expected {expected_backoffs}, got: {backoff_values}"

        # _RPC_MAX_RETRIES + 1 total attempts
        assert mock_controller.ReportStatus.call_count == _RPC_MAX_RETRIES + 1

        # Eventually logs ERROR after exhausting retries
        error_msgs = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any("Failed to report status" in r.message for r in error_msgs), (
            f"Expected error about failed status update, got: {[r.message for r in caplog.records]}"
        )
        # Ensure no WARNING about "not supported" was logged
        assert not any("ReportStatus not supported" in r.message for r in warning_msgs), (
            "UNAVAILABLE error should not produce 'not supported' warning"
        )

    @pytest.mark.parametrize("error_code", [
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
    ])
    async def test_report_status_retries_on_transient_failure(self, error_code):
        """Transient gRPC errors (UNAVAILABLE, DEADLINE_EXCEEDED) are retried.
        If the error resolves before exhausting retries, the status is delivered."""
        exporter = _make_exporter_for_report_status()

        transient_error = grpc.aio.AioRpcError(
            code=error_code,
            initial_metadata=grpc.aio.Metadata(),
            trailing_metadata=grpc.aio.Metadata(),
            details=f"{error_code.name} error",
        )

        delivered_statuses = []
        call_count = 0

        async def fail_twice_then_succeed(request, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise transient_error
            # Third attempt succeeds
            delivered_statuses.append(ExporterStatus.from_proto(request.status))

        _mock_controller, stub_ctx = _setup_mock_controller_stub(exporter, side_effect=fail_twice_then_succeed)

        with patch.object(exporter, "_controller_stub", return_value=stub_ctx), \
                patch("anyio.sleep") as mock_sleep:
            await exporter._report_status(ExporterStatus.AVAILABLE, "test")

        # Status was delivered on the third attempt
        assert ExporterStatus.AVAILABLE in delivered_statuses
        assert call_count == 3

        # Verify exponential backoff for the 2 retries before success
        backoff_values = [call.args[0] for call in mock_sleep.call_args_list]
        assert backoff_values == [1.0, 2.0], f"Expected [1.0, 2.0], got: {backoff_values}"

    async def test_report_status_does_not_retry_non_transient_grpc_error(self):
        """Non-transient gRPC errors (PERMISSION_DENIED, INVALID_ARGUMENT, etc.)
        are not retried — fail fast."""
        exporter = _make_exporter_for_report_status()

        permission_error = grpc.aio.AioRpcError(
            code=grpc.StatusCode.PERMISSION_DENIED,
            initial_metadata=grpc.aio.Metadata(),
            trailing_metadata=grpc.aio.Metadata(),
            details="Permission denied",
        )
        mock_controller, stub_ctx = _setup_mock_controller_stub(exporter, side_effect=permission_error)

        with patch.object(exporter, "_controller_stub", return_value=stub_ctx):
            await exporter._report_status(ExporterStatus.AVAILABLE, "test")

        # Called exactly once (no retry)
        assert mock_controller.ReportStatus.call_count == 1

    async def test_report_status_does_not_retry_non_grpc_exception(self):
        """Non-gRPC exceptions (ConnectionError, etc.) are not retried."""
        exporter = _make_exporter_for_report_status()

        connection_error = ConnectionError("Network unreachable")
        mock_controller, stub_ctx = _setup_mock_controller_stub(exporter, side_effect=connection_error)

        with patch.object(exporter, "_controller_stub", return_value=stub_ctx):
            await exporter._report_status(ExporterStatus.AVAILABLE, "test")

        # Called exactly once (no retry)
        assert mock_controller.ReportStatus.call_count == 1

    async def test_rpc_timeout_parameter_passed(self):
        """Verify that timeout=_RPC_TIMEOUT is passed to gRPC calls."""
        exporter = _make_exporter_for_report_status()

        captured_calls = []

        async def capture_call(*args, **kwargs):
            captured_calls.append(kwargs)

        mock_controller = AsyncMock()
        mock_controller.ReportStatus = AsyncMock(side_effect=capture_call)

        stub_ctx = AsyncMock()
        stub_ctx.__aenter__ = AsyncMock(return_value=mock_controller)
        stub_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch.object(exporter, "_controller_stub", return_value=stub_ctx):
            await exporter._report_status(ExporterStatus.AVAILABLE, "test")

        # Verify timeout parameter was passed
        assert len(captured_calls) == 1
        assert "timeout" in captured_calls[0]
        assert captured_calls[0]["timeout"] == _RPC_TIMEOUT

    async def test_report_status_non_blocking_during_serve(self):
        """Verify _report_status returns immediately when drain is active (serve running)."""
        exporter = _make_exporter_for_report_status()

        # Simulate serve() drain active
        exporter._status_drain_active = True
        exporter._status_rpc_event = Event()
        exporter._pending_status_request = None

        # Call _report_status — should return immediately without awaiting RPC
        await exporter._report_status(ExporterStatus.AVAILABLE, "test")

        # Verify it enqueued the request
        assert exporter._pending_status_request is not None
        assert exporter._pending_status_request.message == "test"
        assert exporter._status_rpc_event.is_set()

        # Now verify latest-wins: call again with different status
        exporter._status_rpc_event = Event()  # Reset
        await exporter._report_status(ExporterStatus.LEASE_READY, "ready")

        # The pending request should be replaced
        assert exporter._pending_status_request.message == "ready"
        assert ExporterStatus.from_proto(exporter._pending_status_request.status) == ExporterStatus.LEASE_READY

    async def test_request_lease_release_succeeds_on_first_attempt(self):
        """When ReleaseLease succeeds immediately, send AVAILABLE and exit."""
        exporter = _make_exporter_for_report_status()

        lease_ctx = LeaseContext(
            lease_name="test-lease",
            before_lease_hook=Event(),
            client_name="test-client",
        )
        exporter._lease_context = lease_ctx

        release_calls = []
        report_calls = []

        async def capture_release(request, **kwargs):
            release_calls.append(request)

        async def capture_report(request, **kwargs):
            report_calls.append(request)

        mock_controller = AsyncMock()
        mock_controller.ReleaseLease = AsyncMock(side_effect=capture_release)
        mock_controller.ReportStatus = AsyncMock(side_effect=capture_report)

        stub_ctx = AsyncMock()
        stub_ctx.__aenter__ = AsyncMock(return_value=mock_controller)
        stub_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch.object(exporter, "_controller_stub", return_value=stub_ctx):
            await exporter._request_lease_release(lease_ctx)

        # ReleaseLease called once with correct lease name
        assert len(release_calls) == 1
        assert release_calls[0].name == "test-lease"

        # ReportStatus called once (AVAILABLE only, no release_lease)
        assert len(report_calls) == 1
        assert report_calls[0].release_lease is False
        assert ExporterStatus.from_proto(report_calls[0].status) == ExporterStatus.AVAILABLE

        # Lease ended event set
        assert lease_ctx.lease_ended.is_set()

    async def test_request_lease_release_rejects_stale_lease(self):
        """A delayed release callback for a replaced lease must not release
        the replacement lease.

        Simulates the beforeLease endLease path: the hook fails for
        'old-lease', the release is delayed, and a replacement lease is
        granted before the callback fires.
        """
        exporter = _make_exporter_for_report_status()

        stale_ctx = make_lease_context("old-lease")
        replacement_ctx = LeaseContext(
            lease_name="new-lease",
            before_lease_hook=Event(),
            client_name="test-client",
        )
        exporter._lease_context = replacement_ctx

        release_calls = []

        async def capture_release(request, **kwargs):
            release_calls.append(request)

        mock_controller = AsyncMock()
        mock_controller.ReleaseLease = AsyncMock(side_effect=capture_release)
        mock_controller.ReportStatus = AsyncMock()

        stub_ctx = AsyncMock()
        stub_ctx.__aenter__ = AsyncMock(return_value=mock_controller)
        stub_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch.object(exporter, "_controller_stub", return_value=stub_ctx):
            await exporter._request_lease_release(stale_ctx)

        # No ReleaseLease RPC for the stale lease; replacement lease untouched
        assert release_calls == []
        assert not replacement_ctx.lease_ended.is_set()

    @pytest.mark.parametrize(("pending_rpc", "error_code"), [
        ("release", None),
        ("release", grpc.StatusCode.PERMISSION_DENIED),
        ("release", grpc.StatusCode.UNIMPLEMENTED),
        ("release", grpc.StatusCode.INVALID_ARGUMENT),
        ("available", None),
    ])
    async def test_request_lease_release_preserves_replacement_during_rpc(self, pending_rpc, error_code):
        """Replacing a lease during an RPC must not apply stale release side effects."""
        from grpc.aio import AioRpcError, Metadata

        exporter = _make_exporter_for_report_status()
        original = make_lease_context("old-lease")
        exporter._lease_context = original
        exporter._exporter_status = ExporterStatus.BEFORE_LEASE_HOOK_FAILED
        replacement = make_lease_context("new-lease")
        replacement.update_status(ExporterStatus.LEASE_READY, "Ready for commands")
        rpc_started = Event()
        rpc_complete = Event()
        report_calls = []

        async def release(request, **kwargs):
            assert request.name == "old-lease"
            if pending_rpc == "release":
                rpc_started.set()
                await rpc_complete.wait()
            if error_code is not None:
                raise AioRpcError(error_code, Metadata(), Metadata(), "Release failed")

        async def report(request, **kwargs):
            report_calls.append(request)
            if pending_rpc != "release":
                rpc_started.set()
                await rpc_complete.wait()

        controller, stub_ctx = _setup_mock_controller_stub(exporter, side_effect=report)
        controller.ReleaseLease = AsyncMock(side_effect=release)
        with patch.object(exporter, "_controller_stub", return_value=stub_ctx), fail_after(5):
            async with create_task_group() as tg:
                tg.start_soon(exporter._request_lease_release, original)
                await rpc_started.wait()
                exporter._lease_context = replacement
                exporter._exporter_status = ExporterStatus.LEASE_READY
                rpc_complete.set()

        assert exporter._lease_context is replacement
        assert not replacement.lease_ended.is_set()
        assert replacement.current_status == ExporterStatus.LEASE_READY
        assert exporter._exporter_status == ExporterStatus.LEASE_READY
        # A report already in flight may finish, but no new report may follow it.
        assert len(report_calls) == (0 if pending_rpc == "release" else 1)
        if report_calls:
            assert report_calls[0].release_lease is False

    @pytest.mark.parametrize("lease_change", ["replacement", "ended", "unchanged"])
    async def test_release_retries_only_for_active_original_lease(self, lease_change):
        from grpc.aio import AioRpcError, Metadata

        exporter = _make_exporter_for_report_status()
        original = make_lease_context("old-lease")
        replacement = make_lease_context("new-lease")
        exporter._lease_context = original
        transient_error = AioRpcError(grpc.StatusCode.UNAVAILABLE, Metadata(), Metadata(), "Try again")
        controller, stub_ctx = _setup_mock_controller_stub(exporter)
        controller.ReleaseLease.side_effect = [transient_error, None]

        async def during_backoff(delay):
            if lease_change == "replacement":
                exporter._lease_context = replacement
            elif lease_change == "ended":
                original.lease_ended.set()

        with (
            patch.object(exporter, "_controller_stub", return_value=stub_ctx),
            patch("anyio.sleep", side_effect=during_backoff) as sleep,
        ):
            await exporter._request_lease_release(original)

        sleep.assert_awaited_once()
        releases = controller.ReleaseLease.await_args_list
        assert len(releases) == (2 if lease_change == "unchanged" else 1)
        assert all(call.args[0].name == original.lease_name for call in releases)
        assert not replacement.lease_ended.is_set()
        if lease_change == "replacement":
            controller.ReportStatus.assert_not_awaited()
        if lease_change == "unchanged":
            assert original.lease_ended.is_set()

    @pytest.mark.parametrize("replace_on_attempt", [1, 2])
    async def test_release_rechecks_lease_after_channel_setup(self, replace_on_attempt):
        from grpc.aio import AioRpcError, Metadata

        exporter = _make_exporter_for_report_status()
        original = make_lease_context("old-lease")
        exporter._lease_context = original
        replacement = make_lease_context("new-lease")
        controller, stub_ctx = _setup_mock_controller_stub(exporter)
        controller.ReleaseLease.side_effect = AioRpcError(
            grpc.StatusCode.UNAVAILABLE, Metadata(), Metadata(), "Try again",
        )

        async def enter_stub():
            if stub_ctx.__aenter__.await_count == replace_on_attempt:
                exporter._lease_context = replacement
            return controller

        stub_ctx.__aenter__.side_effect = enter_stub
        with patch.object(exporter, "_controller_stub", return_value=stub_ctx), patch("anyio.sleep"):
            await exporter._request_lease_release(original)

        assert controller.ReleaseLease.await_count == replace_on_attempt - 1
        controller.ReportStatus.assert_not_awaited()
        assert not replacement.lease_ended.is_set()

    async def test_request_lease_release_retries_on_transient_failure(self):
        """When ReleaseLease fails with UNAVAILABLE on first attempt, retry and succeed."""
        exporter = _make_exporter_for_report_status()

        lease_ctx = LeaseContext(
            lease_name="test-lease",
            before_lease_hook=Event(),
            client_name="test-client",
        )
        exporter._lease_context = lease_ctx

        unavailable_error = grpc.aio.AioRpcError(
            code=grpc.StatusCode.UNAVAILABLE,
            initial_metadata=grpc.aio.Metadata(),
            trailing_metadata=grpc.aio.Metadata(),
            details="Service unavailable",
        )

        release_calls = []
        report_calls = []
        release_call_count = 0

        async def fail_first_then_succeed(request, **kwargs):
            nonlocal release_call_count
            release_call_count += 1
            if release_call_count == 1:
                raise unavailable_error
            release_calls.append(request)

        async def capture_report(request, **kwargs):
            report_calls.append(request)

        mock_controller = AsyncMock()
        mock_controller.ReleaseLease = AsyncMock(side_effect=fail_first_then_succeed)
        mock_controller.ReportStatus = AsyncMock(side_effect=capture_report)

        stub_ctx = AsyncMock()
        stub_ctx.__aenter__ = AsyncMock(return_value=mock_controller)
        stub_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch.object(exporter, "_controller_stub", return_value=stub_ctx), patch("anyio.sleep"):
            await exporter._request_lease_release(lease_ctx)

        # ReleaseLease called twice (failed once, succeeded on retry)
        assert release_call_count == 2
        assert len(release_calls) == 1
        assert release_calls[0].name == "test-lease"

        # ReportStatus called once (AVAILABLE)
        assert len(report_calls) == 1
        assert ExporterStatus.from_proto(report_calls[0].status) == ExporterStatus.AVAILABLE

    async def test_request_lease_release_exhausts_retries(self):
        """When ReleaseLease and AVAILABLE status both fail, lease_ended is still set
        so handle_lease can exit (prevents being stuck forever)."""
        exporter = _make_exporter_for_report_status()

        lease_ctx = LeaseContext(
            lease_name="test-lease",
            before_lease_hook=Event(),
            client_name="test-client",
        )
        exporter._lease_context = lease_ctx

        unavailable_error = grpc.aio.AioRpcError(
            code=grpc.StatusCode.UNAVAILABLE,
            initial_metadata=grpc.aio.Metadata(),
            trailing_metadata=grpc.aio.Metadata(),
            details="Service unavailable",
        )

        release_call_count = 0
        report_call_count = 0

        async def always_fail_release(request, **kwargs):
            nonlocal release_call_count
            release_call_count += 1
            raise unavailable_error

        async def always_fail_report(request, **kwargs):
            nonlocal report_call_count
            report_call_count += 1
            raise unavailable_error

        mock_controller = AsyncMock()
        mock_controller.ReleaseLease = AsyncMock(side_effect=always_fail_release)
        mock_controller.ReportStatus = AsyncMock(side_effect=always_fail_report)

        stub_ctx = AsyncMock()
        stub_ctx.__aenter__ = AsyncMock(return_value=mock_controller)
        stub_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch.object(exporter, "_controller_stub", return_value=stub_ctx), patch("anyio.sleep"):
            await exporter._request_lease_release(lease_ctx)

        # ReleaseLease: _RPC_MAX_RETRIES + 1 attempts (all fail)
        assert release_call_count == _RPC_MAX_RETRIES + 1

        # ReportStatus: _RPC_MAX_RETRIES + 1 attempts via _send_report_status_rpc (all fail)
        assert report_call_count == _RPC_MAX_RETRIES + 1

        # Critical: lease_ended must be set even when everything fails,
        # otherwise handle_lease never exits
        assert lease_ctx.lease_ended.is_set()

    @pytest.mark.parametrize("error_code", [
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.UNIMPLEMENTED,
    ])
    async def test_rejected_release_never_sends_nameless_release(self, error_code, caplog):
        from grpc.aio import AioRpcError, Metadata

        exporter = _make_exporter_for_report_status()
        lease_ctx = make_lease_context()
        exporter._lease_context = lease_ctx
        controller, stub_ctx = _setup_mock_controller_stub(exporter)
        controller.ReleaseLease.side_effect = AioRpcError(error_code, Metadata(), Metadata(), "Release rejected")

        with (
            patch.object(exporter, "_controller_stub", return_value=stub_ctx),
            caplog.at_level(logging.WARNING, logger="jumpstarter.exporter.exporter"),
        ):
            await exporter._request_lease_release(lease_ctx)

        controller.ReleaseLease.assert_awaited_once()
        assert controller.ReleaseLease.await_args.args[0].name == "test-lease"
        controller.ReportStatus.assert_awaited_once()
        assert controller.ReportStatus.await_args.args[0].release_lease is False
        assert "Failed to release lease" in caplog.text


class TestShutdownRuntimeSidecar:
    def test_noop_without_socket_env(self, monkeypatch):
        from jumpstarter.exporter.exporter import shutdown_runtime_sidecar

        monkeypatch.delenv("JUMPSTARTER_LAUNCHER_SOCKET", raising=False)
        assert shutdown_runtime_sidecar() is False

    def test_runs_shutdown_command(self, monkeypatch, tmp_path):
        from jumpstarter.exporter.exporter import shutdown_runtime_sidecar

        sock = tmp_path / "launcher.sock"
        sock.write_text("")  # path only; connect is mocked via subprocess
        binary = tmp_path / "jumpstarter-exec"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)

        monkeypatch.setenv("JUMPSTARTER_LAUNCHER_SOCKET", str(sock))

        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert shutdown_runtime_sidecar(binary=str(binary)) is True
            run.assert_called_once()
            args = run.call_args.args[0]
            assert args[0] == str(binary)
            assert args[1] == "shutdown"
            assert "--socket" in args
            assert str(sock) in args

    def test_falls_back_to_binary_beside_socket(self, monkeypatch, tmp_path):
        """When the default path is missing, use jumpstarter-exec next to the socket."""
        from pathlib import Path

        from jumpstarter.exporter.exporter import shutdown_runtime_sidecar

        sock = tmp_path / "launcher.sock"
        sock.write_text("")
        binary = tmp_path / "jumpstarter-exec"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)

        monkeypatch.setenv("JUMPSTARTER_LAUNCHER_SOCKET", str(sock))

        real_is_file = Path.is_file

        def is_file_no_default(self):
            # Treat the packaged default path as absent so we exercise fallback.
            if str(self) == "/shared/jumpstarter-exec":
                return False
            return real_is_file(self)

        with (
            patch.object(Path, "is_file", is_file_no_default),
            patch("subprocess.run") as run,
        ):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert shutdown_runtime_sidecar() is True
            assert run.call_args.args[0][0] == str(binary)

        monkeypatch.delenv("JUMPSTARTER_LAUNCHER_SOCKET", raising=False)
        with (
            patch.object(Path, "is_file", is_file_no_default),
            patch("subprocess.run") as run,
        ):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert shutdown_runtime_sidecar(socket_path=str(sock)) is True
            assert run.call_args.args[0][0] == str(binary)

    def test_missing_binary_returns_false(self, monkeypatch, tmp_path):
        from jumpstarter.exporter.exporter import shutdown_runtime_sidecar

        sock = tmp_path / "launcher.sock"
        sock.write_text("")
        monkeypatch.setenv("JUMPSTARTER_LAUNCHER_SOCKET", str(sock))

        missing = tmp_path / "does-not-exist"
        assert shutdown_runtime_sidecar(binary=str(missing)) is False

    def test_file_not_found_returns_false(self, monkeypatch, tmp_path):
        from jumpstarter.exporter.exporter import shutdown_runtime_sidecar

        sock = tmp_path / "launcher.sock"
        sock.write_text("")
        binary = tmp_path / "jumpstarter-exec"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        monkeypatch.setenv("JUMPSTARTER_LAUNCHER_SOCKET", str(sock))

        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert shutdown_runtime_sidecar(binary=str(binary)) is False

    def test_permission_error_returns_false(self, monkeypatch, tmp_path):
        from jumpstarter.exporter.exporter import shutdown_runtime_sidecar

        sock = tmp_path / "launcher.sock"
        sock.write_text("")
        binary = tmp_path / "jumpstarter-exec"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        monkeypatch.setenv("JUMPSTARTER_LAUNCHER_SOCKET", str(sock))

        with patch("subprocess.run", side_effect=PermissionError("denied")):
            assert shutdown_runtime_sidecar(binary=str(binary)) is False

    def test_timeout_returns_false(self, monkeypatch, tmp_path):
        import subprocess

        from jumpstarter.exporter.exporter import shutdown_runtime_sidecar

        sock = tmp_path / "launcher.sock"
        sock.write_text("")
        binary = tmp_path / "jumpstarter-exec"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        monkeypatch.setenv("JUMPSTARTER_LAUNCHER_SOCKET", str(sock))

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["jumpstarter-exec"], timeout=1),
        ):
            assert shutdown_runtime_sidecar(binary=str(binary), timeout=1.0) is False

    def test_nonzero_exit_returns_false(self, monkeypatch, tmp_path):
        from jumpstarter.exporter.exporter import shutdown_runtime_sidecar

        sock = tmp_path / "launcher.sock"
        sock.write_text("")
        binary = tmp_path / "jumpstarter-exec"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        monkeypatch.setenv("JUMPSTARTER_LAUNCHER_SOCKET", str(sock))

        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
            assert shutdown_runtime_sidecar(binary=str(binary)) is False


class TestContextPropagation:
    """spec.context of the assigned lease is bound to the lease's log context."""

    def test_lease_context_is_bound_with_the_lease(self):
        from jumpstarter.exporter.exporter import Exporter
        from jumpstarter.exporter.inputs import Assignment

        exporter = Exporter.__new__(Exporter)
        exporter.exporter_name = "lab-exporter-01"
        with_context = Assignment(
            lease_name="lease-ctx", context=(("build_id", "nightly-42"), ("image_digest", "sha256:abc")),
        )
        assert exporter._lease_log_context(with_context) == {
            "lease_id": "lease-ctx", "exporter": "lab-exporter-01",
            "build_id": "nightly-42", "image_digest": "sha256:abc",
        }
        assert exporter._lease_log_context(Assignment(lease_name="lease-no-ctx")) == {
            "lease_id": "lease-no-ctx", "exporter": "lab-exporter-01",
        }
