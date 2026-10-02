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
    user — that still counts as alive.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_tree(pid: int | None) -> set[int]:
    """The PID and all its descendant processes (children, grandchildren, …).

    Walks ``/proc/<pid>/task/<pid>/children`` recursively (Linux 3.2+).
    A branch that cannot be read (process gone, permissions, non-Linux)
    degrades to just that branch's root, so the result always contains the
    requested root PID when it is not None.
    """
    if pid is None:
        return set()
    tree: set[int] = {pid}
    stack = [pid]
    while stack:
        cur = stack.pop()
        try:
            with open(f"/proc/{cur}/task/{cur}/children") as f:
                children = [int(x) for x in f.read().split()]
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, OSError):
            continue
        for child in children:
            if child not in tree:
                tree.add(child)
                stack.append(child)
    return tree
