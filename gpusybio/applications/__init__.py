"""Application management (spec 04): processes, residency, lifecycle."""

from gpusybio.applications.lifecycle import (
    ProcessState,
    ResidencyState,
    AppState,
    combined_ui_state,
    is_valid_combination,
)
from gpusybio.applications.scripts import ScriptResult, graceful_stop, run_script

# NOTE: gpusybio.applications.manager is imported by module path
# (from gpusybio.applications.manager import ApplicationManager) to avoid a
# circular import with gpusybio.state.

__all__ = [
    "AppState",
    "ProcessState",
    "ResidencyState",
    "ScriptResult",
    "combined_ui_state",
    "graceful_stop",
    "is_valid_combination",
    "run_script",
]
