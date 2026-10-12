import os

# Force the offscreen Qt platform for the whole suite, before any test module
# imports PyQt6 and builds a QApplication.
#
# This has to be an assignment, not a setdefault: a real desktop session exports
# QT_QPA_PLATFORM (Wayland or X11), and setdefault would leave it in place, so
# every run would open actual windows. pytest imports this file before the test
# modules beside it, which is early enough to win the race. Setting
# SC_STUDIO_REAL_DISPLAY=1 runs against the real window server anyway, for the
# rare case of wanting to watch a test draw.
if not os.environ.get("SC_STUDIO_REAL_DISPLAY"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"