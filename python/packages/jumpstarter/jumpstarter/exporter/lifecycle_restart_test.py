"""Black-box tests: an exporter restart is a process event, not a lease event.

Each test runs two exporter processes, one after the other, against one fake
controller that keeps a lease hook record (#1208). The first process is
stopped mid-lease; the second must pick the lease up from the record.
"""

import anyio
import pytest
from anyio import fail_after

from jumpstarter.common import LeaseHookPhase
from jumpstarter.exporter.lifecycle_test import S, hook, running

pytestmark = pytest.mark.anyio

Phase = LeaseHookPhase


async def _stop(h) -> None:
    h.exporter.stop(should_unregister=True)
    await h.wait_returned(10)


async def test_stop_hands_the_lease_to_the_next_process(tmp_path):
    log = tmp_path / "hooks.log"
    hooks = {"before": hook(log, "before"), "after": hook(log, "after")}
    async with running(tmp_path, records=True, **hooks) as first:
        ctl = first.controller
        ctl.assign("lease-a")
        await ctl.wait_for(S.LEASE_READY)
        await _stop(first)
        # The lease and its device stay as they are: no cleanup, no release.
        assert first.hook_runs() == ["before lease-a"]
        assert ctl.releases == []
        assert ctl.hook_phase("before_lease") == Phase.SUCCEEDED
        assert ctl.hook_phase("after_lease") is None

    async with running(tmp_path, controller=ctl, **hooks) as second:
        await ctl.wait_for(S.LEASE_READY, count=2)  # resumed without running setup again
        assert second.hook_runs() == ["before lease-a"]
        ctl.unassign()
        with fail_after(10):
            while ctl.hook_phase("after_lease") != Phase.SUCCEEDED:
                await anyio.sleep(0.01)
        assert second.hook_runs() == ["before lease-a", "after lease-a"]


async def test_lease_that_ended_while_down_is_cleaned_up_first(tmp_path):
    log = tmp_path / "hooks.log"
    hooks = {"before": hook(log, "before"), "after": hook(log, "after")}
    async with running(tmp_path, records=True, **hooks) as first:
        ctl = first.controller
        ctl.assign("lease-a")
        await ctl.wait_for(S.LEASE_READY)
        await _stop(first)
    ctl.unassign()  # the client releases while no exporter process runs

    async with running(tmp_path, controller=ctl, **hooks) as second:
        with fail_after(10):
            while ctl.hook_phase("after_lease") != Phase.SUCCEEDED:
                await anyio.sleep(0.01)
        await ctl.wait_for(S.AVAILABLE, count=2)
        assert second.hook_runs() == ["before lease-a", "after lease-a"]
        assert ctl.releases == []


@pytest.mark.parametrize(
    "on_interrupt, on_failure",
    [("rerun", "warn"), ("fail", "warn"), ("fail", "endLease")],
)
async def test_setup_cut_off_by_a_stop_is_finished_per_on_interrupt(tmp_path, on_interrupt, on_failure):
    """rerun: the hook runs again as attempt 2. fail: it fails without running,
    and its onFailure applies as to any failure."""
    log = tmp_path / "hooks.log"
    hooks = {
        "before": hook(log, "before", sleep_first=30, on_interrupt=on_interrupt, on_failure=on_failure),
        "after": hook(log, "after"),
    }
    async with running(tmp_path, records=True, stop_grace=0.5, **hooks) as first:
        ctl = first.controller
        ctl.assign("lease-a")
        await ctl.wait_for(S.BEFORE_LEASE_HOOK)
        await _stop(first)
        assert ctl.hook_phase("before_lease") == Phase.RUNNING  # cut off: the record says so

    async with running(tmp_path, controller=ctl, **hooks) as second:
        if on_interrupt == "rerun":
            await ctl.wait_for(S.LEASE_READY)
            assert second.hook_runs() == ["before lease-a", "before lease-a"]
            assert ctl.record.before_lease.attempts == 2
            return
        if on_failure == "warn":
            await ctl.wait_for(S.LEASE_READY)
            assert "did not finish" in ctl.reports[-1][2]
        else:
            await ctl.wait_for(S.BEFORE_LEASE_HOOK_FAILED)
            with fail_after(10):
                while ctl.releases != ["lease-a"]:
                    await anyio.sleep(0.01)
        assert second.hook_runs() == ["before lease-a"]  # not run again
        assert ctl.hook_phase("before_lease") == Phase.FAILED


async def test_cleanup_cut_off_by_a_stop_runs_again(tmp_path):
    log = tmp_path / "hooks.log"
    hooks = {"before": hook(log, "before"), "after": hook(log, "after", sleep_first=30)}
    async with running(tmp_path, records=True, stop_grace=0.5, **hooks) as first:
        ctl = first.controller
        ctl.assign("lease-a")
        await ctl.wait_for(S.LEASE_READY)
        ctl.unassign()
        await ctl.wait_for(S.AFTER_LEASE_HOOK)
        await _stop(first)
        assert ctl.hook_phase("after_lease") == Phase.RUNNING

    async with running(tmp_path, controller=ctl, **hooks) as second:
        with fail_after(10):
            while ctl.hook_phase("after_lease") != Phase.SUCCEEDED:
                await anyio.sleep(0.01)
        assert second.hook_runs() == ["before lease-a", "after lease-a", "after lease-a"]
        assert ctl.record.after_lease.attempts == 2


async def test_record_of_a_replaced_lease_is_left_alone(tmp_path):
    """The lease was replaced while no process ran: its hooks must not touch a
    device handed to the replacement, which is set up as new."""
    log = tmp_path / "hooks.log"
    hooks = {"before": hook(log, "before"), "after": hook(log, "after")}
    async with running(tmp_path, records=True, **hooks) as first:
        ctl = first.controller
        ctl.assign("lease-a")
        await ctl.wait_for(S.LEASE_READY)
        await _stop(first)
    ctl.assign("lease-b")

    async with running(tmp_path, controller=ctl, **hooks) as second:
        await ctl.wait_for(S.LEASE_READY, count=2)
        assert second.hook_runs() == ["before lease-a", "before lease-b"]
        assert ctl.record.lease_name == "lease-b"


async def test_drain_still_ends_the_lease_normally(tmp_path):
    """A drain (SIGHUP) waits for the lease to end: cleanup runs in this process."""
    log = tmp_path / "hooks.log"
    async with running(tmp_path, records=True, before=hook(log, "before"), after=hook(log, "after")) as h:
        h.controller.assign("lease-a")
        await h.controller.wait_for(S.LEASE_READY)
        h.exporter.stop(wait_for_lease_exit=True, should_unregister=True)
        h.controller.unassign()
        await h.wait_returned(10)
        assert h.hook_runs() == ["before lease-a", "after lease-a"]
        assert h.controller.hook_phase("after_lease") == Phase.SUCCEEDED
