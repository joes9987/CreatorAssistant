"""
Elapsed timer that actively displays time (updates every second).
Replaces progress bars with a live clock.
"""

import sys
import threading
import time


def _format_elapsed(secs: float) -> str:
    m, s = divmod(int(secs), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class ElapsedTimer:
    """Context manager that shows elapsed time updating every second.
    Use timer.log(msg) to buffer messages; they're printed when the timer ends.
    """

    def __init__(self, desc: str = "Processing"):
        self.desc = desc
        self._start = None
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._messages = []

    def log(self, msg: str):
        """Buffer a message to print when the timer ends."""
        with self._lock:
            self._messages.append(msg)

    def _run(self):
        while self._running:
            with self._lock:
                if not self._running:
                    break
                elapsed = time.time() - self._start
                msg = f"\r  {self.desc}... {_format_elapsed(elapsed)} elapsed    "
                if sys.stdout:
                    sys.stdout.write(msg)
                    sys.stdout.flush()
            time.sleep(1)

    def __enter__(self):
        self._start = time.time()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._running = False
        with self._lock:
            pass
        if self._thread:
            self._thread.join(timeout=2)
        if sys.stdout:
            sys.stdout.write("\r" + " " * 60 + "\r")
            sys.stdout.flush()
        for msg in self._messages:
            print(msg)
        elapsed = time.time() - self._start
        print(f"  {self.desc}... Done in {_format_elapsed(elapsed)}")
        return False


def iter_with_timer(iterable, desc: str):
    """Wrap an iterable and show elapsed time while iterating (single line, updates in place)."""
    start = time.time()
    count = 0
    for item in iterable:
        count += 1
        elapsed = time.time() - start
        if sys.stdout:
            sys.stdout.write(f"\r  {desc}... {_format_elapsed(elapsed)} elapsed ({count} samples)    ")
            sys.stdout.flush()
        yield item
    elapsed = time.time() - start
    if sys.stdout:
        sys.stdout.write(f"\r  {desc}... Done in {_format_elapsed(elapsed)}    \n")
        sys.stdout.flush()
