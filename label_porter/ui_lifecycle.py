"""Ownership helpers for disposable UI callbacks and worker results."""
import queue
import threading
import tkinter as tk


class UiEventQueue:
    """Unbounded mailbox whose close atomically discards queued and late results.

    Workers may finish after their view is destroyed. They must not keep a result
    in an abandoned mailbox, or call Tk to find out whether the view still exists.
    """

    def __init__(self):
        self._queue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._closed = False

    def put(self, item):
        with self._lock:
            if not self._closed:
                self._queue.put(item)

    def get_nowait(self):
        return self._queue.get_nowait()

    def qsize(self):
        return self._queue.qsize()

    def empty(self):
        return self._queue.empty()

    def close(self):
        with self._lock:
            self._closed = True
            try:
                while True:
                    self._queue.get_nowait()
            except queue.Empty:
                pass


def remove_traces(traces):
    """Remove only the Tcl callbacks owned by this view, once."""
    while traces:
        variable, token = traces.pop()
        try:
            variable.trace_remove("write", token)
        except tk.TclError:
            # Parent/interpreter destruction can have already removed the trace.
            pass


def release_variables(owner):
    """Drop owned Tcl variables on the UI thread, before widget cycles are GC'd.

    Destroyed widgets can form Python cycles through their parent references.
    Leaving variables on those cycles lets a later worker allocation finalize
    them on the wrong thread. Widgets retain variable names, not these objects.
    """
    for name, value in list(vars(owner).items()):
        if isinstance(value, tk.Variable):
            setattr(owner, name, None)
