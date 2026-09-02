from __future__ import annotations

import threading
from collections.abc import Callable


class WorkerThread:
    def __init__(
        self,
        name: str,
        target: Callable[[], None],
        shutdown_event: threading.Event,
    ) -> None:
        self._name = name
        self._target = target
        self._shutdown_event = shutdown_event
        self._failure: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float) -> None:
        if self._thread.ident is None:
            return
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise RuntimeError(f"Worker {self._name!r} did not stop within {timeout}s")

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise RuntimeError(f"Worker {self._name!r} failed") from self._failure

    def _run(self) -> None:
        try:
            self._target()
        except BaseException as exc:
            self._failure = exc
            self._shutdown_event.set()
