import inspect
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class TaskSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    progress = Signal(object)
    finished = Signal()


class TaskRunnable(QRunnable):
    def __init__(self, fn: Callable, *args, **kwargs) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = TaskSignals()

    def _supports_progress_callback(self) -> bool:
        try:
            parameters = inspect.signature(self._fn).parameters
        except (TypeError, ValueError):
            return False
        return "progress_callback" in parameters

    def run(self) -> None:
        kwargs = dict(self._kwargs)
        if self._supports_progress_callback():
            kwargs["progress_callback"] = self.signals.progress.emit
        try:
            result = self._fn(*self._args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self.signals.error.emit(str(exc))
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()


class TaskRunner(QObject):
    def __init__(self) -> None:
        super().__init__()
        self._pool = QThreadPool.globalInstance()

    def submit(
        self,
        fn: Callable,
        *args,
        on_result: Callable[[object], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_progress: Callable[[object], None] | None = None,
        on_finished: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        task = TaskRunnable(fn, *args, **kwargs)
        if on_result is not None:
            task.signals.result.connect(on_result)
        if on_error is not None:
            task.signals.error.connect(on_error)
        if on_progress is not None:
            task.signals.progress.connect(on_progress)
        if on_finished is not None:
            task.signals.finished.connect(on_finished)
        self._pool.start(task)
