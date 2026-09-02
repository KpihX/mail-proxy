"""
Human-in-the-Loop web UI for mail-proxy.

Ported from `tg-proxy` / `tick-proxy`. Launches a local web server on an
OS-assigned free port (see `_find_free_port()`) showing the action payload for
review. The user can edit the payload, add a comment, then approve or reject.
The chosen port is printed with the review URL on every invocation, so it is
never guessed.

A fixed port is deliberately NOT used: two concurrent `mail-proxy do`
invocations would collide on it and the second HITL server would fail to bind.
Binding to port 0 lets the kernel hand out a guaranteed-free port instead.

100% Web UI — no TUI fallback. If no browser, the URL is printed for SSH access.
"""

import json
import logging
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

from .exceptions import MailAPIError, MailProxyError

logger = logging.getLogger(__name__)

HITL_TIMEOUT: int | None = 600  # seconds (None = wait forever)

TEMPLATES_DIR = Path(__file__).parent / "templates"
TEMPLATE_PATH = TEMPLATES_DIR / "hitl.html"
STYLESHEET_PATH = TEMPLATES_DIR / "hitl.css"

# Action → custom template mapping (fallback to hitl.html when not listed)
_ACTION_TEMPLATES: dict[str, Path] = {
    "admin auth login": TEMPLATES_DIR / "auth_login.html",
    "message-send": TEMPLATES_DIR / "message-review.html",
    "message-reply": TEMPLATES_DIR / "message-review.html",
    "message-forward": TEMPLATES_DIR / "message-review.html",
    "message-draft": TEMPLATES_DIR / "message-review.html",
}


class HITLResponse:
    """The outcome of one HITL review.

    Attributes:
        status (str): "approved" or "rejected".
        payload (Any): The payload as (possibly) edited by the reviewer.
        comment (str): Free-text reviewer comment.
        edited (bool): True when the reviewer changed the payload.

    Examples:
        >>> HITLResponse("approved", {"to": ["a@b.fr"]}).status
        'approved'
        >>> HITLResponse("rejected", None, "wrong recipient").comment
        'wrong recipient'
    """

    def __init__(
        self,
        status: str,
        payload: Any = None,
        comment: str = "",
        edited: bool = False,
    ) -> None:
        self.status = status
        self.payload = payload
        self.comment = comment
        self.edited = edited


class HITLServer(BaseHTTPRequestHandler):
    """Single-request HTTP handler serving the review page and its submission."""

    active_requests: ClassVar[dict[str, dict[str, Any]]] = {}

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr access log (stdout must stay pure JSON).

        Args:
            format (str): Unused format string.
            *args (Any): Unused arguments.

        Returns:
            None: No output is ever produced.

        Examples:
            >>> HITLServer.log_message(None, "%s", "x") is None   # doctest: +SKIP
            True
        """
        return

    def do_GET(self) -> None:
        """Serve the review page for `/review?id=<uuid>`.

        Returns:
            None

        Examples:
            >>> # GET /review?id=<known>  → 200 with the HTML form
            >>> # GET /review?id=<unknown> → 404
        """
        if self.path == "/assets/hitl.css":
            self.send_response(200)
            self.send_header("Content-type", "text/css; charset=utf-8")
            self.end_headers()
            self.wfile.write(STYLESHEET_PATH.read_bytes())
            return
        if self.path.startswith("/review"):
            query = self.path.split("?")[-1]
            req_id = query.split("id=")[-1] if "id=" in query else ""
            if req_id not in self.active_requests:
                self.send_error(404, "Review request not found.")
                return
            req = self.active_requests[req_id]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(self._render(req_id, req).encode("utf-8"))
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        """Collect the reviewer's decision on `/submit` and unblock the caller.

        Returns:
            None

        Examples:
            >>> # POST /submit {"id": …, "status": "approved"} → {"ok": true}
            >>> # POST /submit with unknown id → 404
        """
        if self.path != "/submit":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        post = json.loads(self.rfile.read(length).decode("utf-8"))
        req_id = post.get("id", "")
        if req_id not in self.active_requests:
            self.send_error(404)
            return
        req = self.active_requests[req_id]
        payload_raw = post.get("payload")
        if isinstance(payload_raw, str):
            try:
                payload = json.loads(payload_raw)
            except (json.JSONDecodeError, ValueError):
                payload = payload_raw
        else:
            payload = payload_raw if payload_raw is not None else req.get("payload")
        req["result"] = HITLResponse(
            post.get("status", "rejected"),
            payload,
            post.get("comment", ""),
            bool(post.get("edited", False)),
        )
        req["event"].set()
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))

    def _render(self, req_id: str, req: dict) -> str:
        """Fill the HTML template with the payload under review.

        Args:
            req_id (str): The review request uuid.
            req (dict): The in-flight request context.

        Returns:
            str: The rendered HTML page.

        Examples:
            >>> # returns the form pre-filled with the pretty-printed payload
            >>> # returns an explicit error page when the template file is missing
        """
        try:
            payload_display = json.dumps(req["payload"], indent=2, default=str)
            payload_safe = (
                json.dumps(req["payload"], default=str)
                .replace("\\", "\\\\")
                .replace("'", "\\'")
            )
        except (TypeError, ValueError):
            safe = str(req.get("payload", {}))
            payload_display = safe
            payload_safe = safe[:100]
        try:
            action = req.get("func_name", "")
            template = _ACTION_TEMPLATES.get(action, TEMPLATE_PATH)
            html = template.read_text()
        except FileNotFoundError:
            return f"<html><body><h2>Template not found: {TEMPLATE_PATH}</h2></body></html>"
        html = html.replace("{{FUNC_NAME}}", req.get("func_name", "unknown"))
        html = html.replace("{{PAYLOAD_JSON}}", payload_display)
        html = html.replace("{{PAYLOAD_JSON_SAFE}}", payload_safe)
        html = html.replace("{{REQUEST_ID}}", req_id)
        account_id = ""
        account_email = ""
        acct = None
        if isinstance(req.get("payload"), dict):
            account_id = req["payload"].get("account_id", "")
        if account_id:
            try:
                from .config import get_account

                acct = get_account(account_id)
                account_email = acct.email
            except (ValueError, KeyError):
                pass
        html = html.replace("{{ACCOUNT_ID}}", account_id)
        html = html.replace("{{ACCOUNT_EMAIL}}", account_email)

        # Resolve signature to rendered HTML so the HITL preview shows the
        # actual signature (logo + text), never the raw keyword "default".
        resolved_sig = ""
        if isinstance(req.get("payload"), dict) and acct is not None:
            sig_val = req["payload"].get("signature")
            # Missing key → "default"; explicit "" → no signature
            if sig_val is None:
                sig_val = "default"
            try:
                from .api.smtp import SMTPClient

                _, html_sig, logo = SMTPClient(acct)._resolve_signature(sig_val)
                resolved_sig = html_sig
                # Convert CID image reference to base64 data URI for display
                if logo and 'src="cid:' in resolved_sig:
                    import base64 as _b64

                    b64 = _b64.b64encode(logo).decode()
                    resolved_sig = resolved_sig.replace(
                        'src="cid:sig_logo"',
                        f'src="data:image/png;base64,{b64}"',
                    )
            except (MailProxyError, MailAPIError, FileNotFoundError, KeyError):
                pass
        html = html.replace("{{RESOLVED_SIG_HTML}}", resolved_sig)
        return html


def request_approval(action: str, payload: Any) -> HITLResponse:
    """Open the HITL web form and block until the reviewer decides.

    Args:
        action (str): Action name shown in the form, e.g. `message-delete`.
        payload (Any): The payload submitted for review (JSON-serialisable).

    Returns:
        HITLResponse: The decision, including the (possibly edited) payload.
        A timeout produces `status="rejected"`.

    Examples:
        >>> request_approval("message-send", {"to": ["a@b.fr"]}).status
        'approved'
        >>> request_approval("raw", {"command": "STATUS"}).status  # user clicked Reject
        'rejected'
    """
    req_id = str(uuid.uuid4())
    event = threading.Event()
    req_context: dict[str, Any] = {
        "func_name": action,
        "payload": payload,
        "event": event,
        "result": None,
    }
    HITLServer.active_requests[req_id] = req_context

    server = HTTPServer(("127.0.0.1", 0), HITLServer)
    port = int(server.server_address[1])
    url = f"http://127.0.0.1:{port}/review?id={req_id}"

    print("\n🚀 [HITL] ACTION REVIEW REQUIRED", file=sys.stderr)
    print(f"🔗 {url}", file=sys.stderr)
    print(f"📝 Action: {action}", file=sys.stderr)
    print(
        "If the browser doesn't open, connect from a machine with a GUI:",
        file=sys.stderr,
    )
    print(f"   ssh -L {port}:localhost:{port} your-host", file=sys.stderr)

    def serve() -> None:
        while not event.is_set():
            server.handle_request()

    threading.Thread(target=serve, daemon=True).start()

    import webbrowser

    try:
        webbrowser.open(url)
    except OSError:  # pragma: no cover - headless host
        logger.warning("Failed to open browser for HITL URL: %s", url)

    if not event.wait(timeout=HITL_TIMEOUT):
        logger.warning("HITL timeout expired for %s (id=%s)", action, req_id)
        HITLServer.active_requests.pop(req_id, None)
        server.server_close()
        return HITLResponse(
            "rejected", None, "HITL timeout expired (no response received)", False
        )

    result: HITLResponse = req_context["result"]
    HITLServer.active_requests.pop(req_id, None)
    server.server_close()
    return result
