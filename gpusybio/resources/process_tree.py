"""Process-tree attribution (spec 07 §2.2).

GPU work is often done by *children* of the PID a start script records —
an ``ollama serve`` supervisor spawning workers, a shell wrapper forking
the real server, a server forking inference threads into helper processes.
Per-process GPU attribution therefore spans the whole tree, not just the
recorded root PID.
"""

from __future__ import annotations

import os


def pid_alive(pid: int) -> bool:
    """True if a process with this PID exists (``os.kill(pid, 0)``).

    ``PermissionError`` means the process exists but belongs to another
    user — that still counts as alive. ``pid <= 0`` is never a valid
    process (``os.kill(0, sig)`` targets the caller's own process group).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _spawned_children(pid: int) -> set[int]:
    """Every direct child the process spawned from *any* thread.

    ``/proc/<pid>/task/<tid>/children`` lists only the processes that the
    specific thread ``<tid>`` actually spawned. Multithreaded runtimes
    (Go, JVM, V8, Rust) spawn long-lived workers from non-main OS threads:
    ``ollama serve`` (a Go binary whose scheduler runs ~20 threads) launches
    its ``llama-server`` worker from a secondary thread, so the main
    thread's children file is empty even though the worker is a direct
    child (its PPID is the main PID). The union over all live threads
    covers every direct child.

    An empty result (thread gone, permissions, non-Linux) degrades the
    caller's branch to just its root.
    """
    out: set[int] = set()
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except (FileNotFoundError, PermissionError, OSError):
        return out
    for tid in tids:
        try:
            with open(f"/proc/{pid}/task/{tid}/children") as f:
                out.update(int(x) for x in f.read().split())
        except (FileNotFoundError, PermissionError, ValueError, OSError):
            continue  # thread exited mid-walk, or unparseable entry
    return out


def process_tree(pid: int | None) -> set[int]:
    """The PID and all its descendant processes (children, grandchildren, …).

    Walks ``/proc/<pid>/task/<tid>/children`` for *every thread* of each
    process recursively (Linux 3.2+). A branch that cannot be read
    (process gone, permissions, non-Linux) degrades to just that branch's
    root, so the result always contains the requested root PID when it is
    not None.
    """
    if pid is None:
        return set()
    tree: set[int] = {pid}
    stack = [pid]
    while stack:
        cur = stack.pop()
        for child in _spawned_children(cur):
            if child not in tree:
                tree.add(child)
                stack.append(child)
    return tree
