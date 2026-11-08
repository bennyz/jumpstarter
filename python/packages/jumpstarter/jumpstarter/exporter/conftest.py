"""The lifecycle's phase label, checked against the code.

In every exporter test, a transition ALLOWED does not list raises, failing the
test that takes it, and a hook that runs outside its phase (beforeLease outside
setup, afterLease outside cleanup) fails it too: the label must say what the
lifecycle is doing. When the lifecycle suites run in full, the run also fails if
an edge ALLOWED lists was never taken: the table, and the documentation diagram
drawn from it, may only promise what the code does.
"""

import pytest

from jumpstarter.common import LogSource
from jumpstarter.exporter.hooks import HookExecutor
from jumpstarter.exporter.phase import ALLOWED, Phase, PhaseTracker

_LIFECYCLE_SUITES = frozenset({
    "jumpstarter.exporter.lifecycle_test",
    "jumpstarter.exporter.lifecycle_restart_test",
})
_taken: set[tuple[Phase, Phase]] = set()
_suites_ran_in_full = pytest.StashKey[bool]()


_HOOK_PHASES = {LogSource.BEFORE_LEASE_HOOK: Phase.SETUP, LogSource.AFTER_LEASE_HOOK: Phase.CLEANUP}


@pytest.fixture(autouse=True)
def _checked_lifecycle(monkeypatch):
    move = PhaseTracker.move
    moving: list[PhaseTracker] = []  # the exporter lifecycle that moved last in this test

    def checked_move(self: PhaseTracker, new: Phase) -> None:
        old = self.current
        if new != old:
            assert new in ALLOWED[old], f"undeclared lifecycle transition {old} -> {new}"
            _taken.add((old, new))
        moving[:] = [self]
        move(self, new)

    execute = HookExecutor._execute_hook

    misplaced: list[str] = []

    async def checked_execute(self, hook_config, lease_scope, log_source):
        # Hook unit tests run hooks without a lifecycle: nothing to check against.
        # Collected rather than raised here: the hook runner would catch it as a hook failure.
        if moving and moving[0].current is not _HOOK_PHASES[log_source]:
            misplaced.append(f"{log_source} hook ran while the phase said {moving[0].current}")
        return await execute(self, hook_config, lease_scope, log_source)

    monkeypatch.setattr(PhaseTracker, "move", checked_move)
    monkeypatch.setattr(HookExecutor, "_execute_hook", checked_execute)
    yield
    assert not misplaced, misplaced


def pytest_collection_finish(session: pytest.Session) -> None:
    collected: dict[object, set[str]] = {}
    for item in session.items:
        module = getattr(item, "module", None)
        if module is not None and module.__name__ in _LIFECYCLE_SUITES:
            collected.setdefault(module, set()).add(getattr(item, "originalname", item.name))
    session.config.stash[_suites_ran_in_full] = len(collected) == len(_LIFECYCLE_SUITES) and all(
        names == {name for name in vars(module) if name.startswith("test_")}
        for module, names in collected.items()
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if exitstatus != pytest.ExitCode.OK or not session.config.stash.get(_suites_ran_in_full, False):
        return
    declared = {(old, new) for old, targets in ALLOWED.items() for new in targets}
    missing = sorted(declared - _taken)
    if missing:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            edges = ", ".join(f"{old} -> {new}" for old, new in missing)
            reporter.write_line(f"Lifecycle transitions ALLOWED lists but no lifecycle test takes: {edges}", red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
