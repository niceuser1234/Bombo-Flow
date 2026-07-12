"""TCC permission checks and System Settings deep links.

Input Monitoring is checked via IOHIDCheckAccess (IOKit, macOS 10.15+),
called through ctypes since pyobjc ships no IOKit binding. Accessibility
checks live in insert.py (AXIsProcessTrusted). Microphone needs no check:
macOS prompts automatically on first recording because the app bundle
declares NSMicrophoneUsageDescription.
"""

import ctypes
import logging
import subprocess

log = logging.getLogger(__name__)

# IOHIDRequestType / IOHIDAccessType constants (IOKit/hid/IOHIDLib.h)
_KIOHID_REQUEST_LISTEN_EVENT = 1
_ACCESS_GRANTED = 0

PANE_INPUT_MONITORING = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
)
PANE_ACCESSIBILITY = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)
PANE_MICROPHONE = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
)


def input_monitoring_granted() -> bool:
    try:
        iokit = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/IOKit.framework/IOKit"
        )
        iokit.IOHIDCheckAccess.restype = ctypes.c_int
        iokit.IOHIDCheckAccess.argtypes = [ctypes.c_int]
        status = iokit.IOHIDCheckAccess(_KIOHID_REQUEST_LISTEN_EVENT)
        return status == _ACCESS_GRANTED
    except Exception:
        log.exception("IOHIDCheckAccess failed — assuming granted")
        return True


def open_privacy_pane(pane_url: str) -> None:
    subprocess.run(["open", pane_url], capture_output=True)
