"""Entry point: launch the TraceImage Qt application.

Run from the project root with:  python -m traceimage.main
(After `pip install -e .` it can also be exposed as a console script.)
"""

import os
import sys


def _ensure_src_on_path():
    """Allow `python src/traceimage/main.py` as well as `-m traceimage.main`."""
    here = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.dirname(here)  # .../src
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)


def main():
    _ensure_src_on_path()

    from PySide6.QtWidgets import QApplication
    from traceimage.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("TraceImage")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
