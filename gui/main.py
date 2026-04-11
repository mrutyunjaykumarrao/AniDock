import sys
import signal

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow


def run() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()

    # Keep Python signal processing active so Ctrl+C closes GUI cleanly from terminal.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    pulse = QTimer()
    pulse.timeout.connect(lambda: None)
    pulse.start(200)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
