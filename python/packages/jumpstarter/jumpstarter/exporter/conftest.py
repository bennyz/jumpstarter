"""The lifecycle's phase label and its documented diagram, checked against the code.

The lifecycle diagram in the hooks documentation is not written by hand: it is
drawn from the phase transitions the lifecycle tests take. In every exporter
test, a transition the diagram does not show fails the test that takes it, and a
hook that runs outside its phase (beforeLease outside setup, afterLease outside
cleanup) fails it too: the label must say what the lifecycle is doing. When the
lifecycle suites run in full, the run also fails if the diagram shows a
transition no test took: it may only promise what the code does.

When the lifecycle changes on purpose, redraw the diagram from the tests:

    JMP_UPDATE_LIFECYCLE_DIAGRAM=1 pytest jumpstarter/exporter/lifecycle_test.py \
        jumpstarter/exporter/lifecycle_restart_test.py
"""

import os
import re
from contextvars import ContextVar
from functools import cache
from pathlib import Path

import pytest

from jumpstarter.common import LogSource
from jumpstarter.exporter.hooks import HookExecutor
from jumpstarter.exporter.phase import Phase, PhaseTracker

Transition = tuple[Phase, Phase]

HOOKS_DOC = Path(__file__).resolve().parents[5] / "docs" / "source" / "introduction" / "hooks.md"
_UPDATE = "JMP_UPDATE_LIFECYCLE_DIAGRAM"
_DIAGRAM = re.compile(
    r"(<!-- Generated from the exporter lifecycle tests[^\n]*\n```\{mermaid\}\n)(.*?)(\n```)", re.DOTALL
)
_LIFECYCLE_SUITES = frozenset({
    "jumpstarter.exporter.lifecycle_test",
    "jumpstarter.exporter.lifecycle_restart_test",
})
_taken: set[Transition] = set()
_suites_ran_in_full = pytest.StashKey[bool]()


_HOOK_PHASES = {LogSource.BEFORE_LEASE_HOOK: Phase.SETUP, LogSource.AFTER_LEASE_HOOK: Phase.CLEANUP}
_current_tracker: ContextVar[PhaseTracker | None] = ContextVar("_current_tracker", default=None)


def draw(transitions: set[Transition]) -> str:
    """The lifecycle as a mermaid state diagram."""
    order = list(Phase)
    lines = ["stateDiagram-v2", f"    [*] --> {Phase.IDLE}"]
    lines += [
        f"    {old} --> {new}"
        for old, new in sorted(transitions, key=lambda edge: (order.index(edge[0]), order.index(edge[1])))
    ]
    lines.append(f"    {Phase.STOPPING} --> [*]")
    return "\n".join(lines)


@cache
def documented() -> frozenset[Transition]:
    """The transitions the diagram in the hooks documentation shows."""
    diagram = _DIAGRAM.search(HOOKS_DOC.read_text())
    assert diagram, f"no generated lifecycle diagram in {HOOKS_DOC}"
    return frozenset((Phase(old), Phase(new)) for old, new in re.findall(r"(\w+) --> (\w+)", diagram.group(2)))


@pytest.fixture(autouse=True)
def _checked_lifecycle(monkeypatch):
    move = PhaseTracker.move

    def checked_move(self: PhaseTracker, new: Phase) -> None:
        old = self.current
        if new != old:
            assert _UPDATE in os.environ or (old, new) in documented(), (
                f"lifecycle transition {old} -> {new} is not in the documented diagram; "
                f"if it is intended, redraw the diagram (see {__name__})"
            )
            _taken.add((old, new))
        _current_tracker.set(self)
        move(self, new)

    execute = HookExecutor._execute_hook

    misplaced: list[str] = []

    async def checked_execute(self, hook_config, lease_scope, log_source):
        # Hook unit tests run hooks without a lifecycle: nothing to check against.
        # Collected rather than raised here: the hook runner would catch it as a hook failure.
        tracker = _current_tracker.get()
        if tracker is not None and tracker.current is not _HOOK_PHASES[log_source]:
            misplaced.append(f"{log_source} hook ran while the phase said {tracker.current}")
        return await execute(self, hook_config, lease_scope, log_source)

    token = _current_tracker.set(None)
    monkeypatch.setattr(PhaseTracker, "move", checked_move)
    monkeypatch.setattr(HookExecutor, "_execute_hook", checked_execute)
    try:
        yield
    finally:
        _current_tracker.reset(token)
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
    if _taken == documented():
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if _UPDATE in os.environ:
        HOOKS_DOC.write_text(_DIAGRAM.sub(lambda m: m.group(1) + draw(_taken) + m.group(3), HOOKS_DOC.read_text()))
        if reporter is not None:
            reporter.write_line(f"Redrew the lifecycle diagram in {HOOKS_DOC}", yellow=True)
        return
    if reporter is not None:
        edges = ", ".join(f"{old} -> {new}" for old, new in sorted(documented() - _taken))
        reporter.write_line(
            f"The documented lifecycle diagram shows transitions no lifecycle test takes: {edges}", red=True
        )
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
