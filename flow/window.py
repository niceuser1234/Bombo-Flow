"""Native window host for the dictation dashboard.

A single reusable NSWindow embedding a WKWebView that loads the bundled
single-page UI (flow/ui/index.html). JavaScript talks to Python over a JSON
message bridge: the page posts to `window.webkit.messageHandlers.bridge`, we
dispatch to the SQLite-backed store, and reply by evaluating
`window.BF.onMessage(...)` back in the page.

AppKit must only be touched from the main thread, so show() marshals onto it
with AppHelper.callAfter and builds the window lazily on first use. PyObjC does
not retain Objective-C objects on our behalf, so MainWindow keeps strong Python
references to the window, the web view and the script-message handler (a missing
handler ref is the most common crash here).
"""

import getpass
import json
import logging
import sys
from pathlib import Path

import objc
from AppKit import (
    NSApp,
    NSBackingStoreBuffered,
    NSFullUserName,
    NSPasteboard,
    NSPasteboardTypeString,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import (
    NSMakeRect,
    NSMakeSize,
    NSObject,
    NSURL,
)
from PyObjCTools import AppHelper
from WebKit import WKWebView, WKWebViewConfiguration

from .config import IS_FROZEN, PROJECT_ROOT

log = logging.getLogger(__name__)

WINDOW_TITLE = "Bomboclat Flow"
WIN_WIDTH = 960.0
WIN_HEIGHT = 640.0
MIN_WIDTH = 800.0
MIN_HEIGHT = 560.0

_FALLBACK_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<style>html,body{height:100%;margin:0;font:15px -apple-system,sans-serif;"
    "background:#1c1c1e;color:#e5e5e7;display:flex;align-items:center;"
    "justify-content:center}div{text-align:center;opacity:.7}</style></head>"
    "<body><div><h2>UI not built</h2><p>flow/ui/index.html is missing.</p>"
    "</div></body></html>"
)


def _ui_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", PROJECT_ROOT)) / "flow" / "ui"


def _first_name() -> str:
    full = ""
    try:
        full = (NSFullUserName() or "").strip()
    except Exception:
        full = ""
    if full:
        return full.split()[0]
    return getpass.getuser()


class _BridgeHandler(NSObject):
    """ObjC-visible WKScriptMessageHandler dispatching bridge calls to Python.

    WebKit always delivers script messages on the main thread, so the
    NSPasteboard access in _on_message's "copyText" handler below is safe
    without the main-thread marshalling flow/insert.py needs for its
    worker-thread paste path.
    """

    def initWithWindow_(self, window):
        self = objc.super(_BridgeHandler, self).init()
        if self is None:
            return None
        self._window = window
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        self._window._on_message(message)


class MainWindow:
    """Reusable dashboard window. Lazy: nothing is created until show()."""

    def __init__(self):
        self._window = None
        self._webview = None
        self._handler = None

    # -- public API (thread-safe) --

    def show(self):
        AppHelper.callAfter(self._show_main)

    # -- main-thread implementation --

    def _show_main(self):
        if self._window is None:
            self._build()
        NSApp.activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)

    def _build(self):
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
        )
        frame = NSMakeRect(0, 0, WIN_WIDTH, WIN_HEIGHT)
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False
        )
        window.setTitle_(WINDOW_TITLE)
        window.setReleasedWhenClosed_(False)
        window.setMinSize_(NSMakeSize(MIN_WIDTH, MIN_HEIGHT))
        window.center()

        config = WKWebViewConfiguration.alloc().init()
        self._handler = _BridgeHandler.alloc().initWithWindow_(self)
        config.userContentController().addScriptMessageHandler_name_(
            self._handler, "bridge"
        )
        # Dev nicety: enable the Web Inspector (right-click → Inspect Element).
        # Not in a shipped bundle — it hands anyone at the keyboard a console
        # on the bridge, and with it the store's delete/copy actions.
        if not IS_FROZEN:
            config.preferences().setValue_forKey_(True, "developerExtrasEnabled")

        webview = WKWebView.alloc().initWithFrame_configuration_(frame, config)
        webview.setAutoresizingMask_(1 << 1 | 1 << 4)  # width | height sizable
        window.setContentView_(webview)

        self._window = window
        self._webview = webview
        self._load()

    def _load(self):
        ui_dir = _ui_dir()
        index = ui_dir / "index.html"
        if index.exists():
            file_url = NSURL.fileURLWithPath_(str(index))
            dir_url = NSURL.fileURLWithPath_(str(ui_dir))
            self._webview.loadFileURL_allowingReadAccessToURL_(file_url, dir_url)
        else:
            log.warning("UI not built: %s missing, loading fallback", index)
            self._webview.loadHTMLString_baseURL_(_FALLBACK_HTML, None)

    # -- bridge --

    def _on_message(self, message):
        """Handle one JS → Python bridge call on the main thread and reply."""
        req_id = None
        try:
            body = message.body()
            if isinstance(body, str):
                body = json.loads(body)
            else:
                body = dict(body)
            req_id = body.get("id")
            action = body.get("action")
            payload = body.get("payload") or {}
            data = self._dispatch(action, payload)
            self._reply(req_id, True, data)
        except Exception as exc:
            log.exception("Bridge action failed")
            self._reply(req_id, False, str(exc))

    def _dispatch(self, action, payload):
        from .store import get_store

        store = get_store()

        if action == "getHome":
            insights = store.insights()
            streak = insights.get("streak") or {}
            entries = [
                {
                    "id": e["id"], "ts": e["ts"], "text": e["text"],
                    "pinned": e["pinned"], "app": e["app"],
                    "kind": e.get("kind", "dictation"),
                }
                for e in store.history()
            ]
            return {
                "userName": _first_name(),
                "stats": {
                    "totalWords": insights.get("totalWords", 0),
                    "wpm": insights.get("wpm", 0),
                    "streak": streak.get("current", 0),
                },
                "entries": entries,
            }

        if action == "getInsights":
            return store.insights()

        if action == "getDictionary":
            return {"words": store.dictionary()}

        if action == "setPinned":
            store.set_pinned(payload["id"], bool(payload["pinned"]))
            return None

        if action == "deleteEntry":
            store.delete_entry(payload["id"])
            return None

        if action == "copyText":
            text = store.get_text(payload["id"])
            if text is not None:
                pb = NSPasteboard.generalPasteboard()
                pb.clearContents()
                pb.setString_forType_(text, NSPasteboardTypeString)
            return None

        if action == "addWord":
            store.add_word(payload["word"], auto=False)
            return None

        if action == "deleteWord":
            store.delete_word(payload["word"])
            return None

        raise ValueError(f"unknown action: {action!r}")

    def _reply(self, req_id, ok, data):
        response = {"id": req_id, "ok": ok, "data": data}
        js = "window.BF.onMessage(" + json.dumps(response) + ")"
        if self._webview is not None:
            self._webview.evaluateJavaScript_completionHandler_(js, None)
