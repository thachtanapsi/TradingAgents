"""Loopback-only web UI for GX TradingAgents runs."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import webbrowser
from copy import deepcopy
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv

_MAX_BODY_BYTES = 32 * 1024
_STATIC_FILES = {
    "/static/gx_dashboard.css": ("gx_dashboard.css", "text/css; charset=utf-8"),
    "/static/gx_dashboard.js": ("gx_dashboard.js", "text/javascript; charset=utf-8"),
    "/static/gx_history.css": ("gx_history.css", "text/css; charset=utf-8"),
    "/static/gx_history.js": ("gx_history.js", "text/javascript; charset=utf-8"),
}
_PAGE_FILES = {
    "/": "gx_dashboard.html",
    "/history": "gx_history.html",
    "/history/": "gx_history.html",
}


def load_ui_config(env_file: Path | None = None) -> dict[str, Any]:
    """Load the same GX profile as ``tradingagents-gx`` after its env file."""
    if env_file is not None:
        load_dotenv(env_file, override=True)
    from tradingagents.dataflows.config import set_config
    from tradingagents.default_config import (
        DEFAULT_CONFIG,
        _apply_env_overrides,
        apply_gx_market_info_defaults,
    )

    config = apply_gx_market_info_defaults(
        _apply_env_overrides(deepcopy(DEFAULT_CONFIG))
    )
    set_config(config)
    return config


def _asset_bytes(name: str) -> bytes:
    return files("cli").joinpath("static", name).read_bytes()


class DashboardHTTPServer(ThreadingHTTPServer):
    """HTTP transport with per-launch request authentication."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: Any):
        super().__init__(address, DashboardRequestHandler)
        self.service = service
        self.ui_token = secrets.token_urlsafe(32)

    @property
    def public_origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Avoid query strings and untrusted request data in terminal logs.
        sys.stderr.write(f"tradingagents-ui: {self.command} {urlsplit(self.path).path}\n")

    def do_GET(self) -> None:  # noqa: N802
        if not self._valid_host():
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path in _PAGE_FILES:
            page = _asset_bytes(_PAGE_FILES[path]).decode("utf-8")
            page = page.replace("__UI_TOKEN__", self.server.ui_token)
            page = page.replace("__DEFAULT_DATE__", date.today().isoformat())
            self._send_bytes(page.encode(), "text/html; charset=utf-8")
            return
        if path in _STATIC_FILES:
            name, content_type = _STATIC_FILES[path]
            self._send_bytes(_asset_bytes(name), content_type, cache=True)
            return
        if path == "/api/health":
            self._send_json({"status": "ok"})
            return
        if path == "/api/info":
            if not self._authorized(require_origin=False):
                return
            self._send_json(self.server.service.public_info())
            return
        if path == "/api/history":
            if not self._authorized(require_origin=False):
                return
            try:
                parameters = self._single_query_parameters(parsed.query)
                payload = self.server.service.list_history(parameters)
            except ValueError as exc:
                self._send_json({"error": str(exc)[:300]}, HTTPStatus.BAD_REQUEST)
                return
            except Exception:  # noqa: BLE001 - never expose archive paths or contents
                self._send_json(
                    {"error": "Unable to read research history."},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            self._send_json(payload)
            return
        history_prefix = "/api/history/"
        if path.startswith(history_prefix):
            if not self._authorized(require_origin=False):
                return
            history_id = path[len(history_prefix) :]
            if not re.fullmatch(r"[0-9a-f]{64}", history_id):
                self._send_json({"error": "Research history not found."}, HTTPStatus.NOT_FOUND)
                return
            try:
                payload = self.server.service.get_history(history_id)
            except ValueError:
                payload = None
            except Exception:  # noqa: BLE001 - never expose archive paths or contents
                self._send_json(
                    {"error": "Unable to read research history."},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            if payload is None:
                self._send_json({"error": "Research history not found."}, HTTPStatus.NOT_FOUND)
            else:
                self._send_json(payload)
            return
        prefix = "/api/runs/"
        if path.startswith(prefix):
            if not self._authorized(require_origin=False):
                return
            job = self.server.service.get_job(path[len(prefix) :])
            if job is None:
                self._send_json({"error": "Run not found."}, HTTPStatus.NOT_FOUND)
            else:
                self._send_json(job)
            return
        self._send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

    @staticmethod
    def _single_query_parameters(query: str) -> dict[str, str]:
        parsed = parse_qs(
            query,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=16,
        )
        if any(len(values) != 1 for values in parsed.values()):
            raise ValueError("Each history filter may be provided only once.")
        return {key: values[0] for key, values in parsed.items()}

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path != "/api/runs":
            self._send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
            return
        if not self._authorized(require_origin=True):
            return
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            self._send_json(
                {"error": "Content-Type must be application/json."},
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = -1
        if content_length < 1 or content_length > _MAX_BODY_BYTES:
            self._send_json({"error": "Invalid request size."}, HTTPStatus.BAD_REQUEST)
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
            job = self.server.service.start_run(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "Request body must be valid JSON."}, HTTPStatus.BAD_REQUEST)
            return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except Exception:  # noqa: BLE001 - do not expose configuration or credentials
            self._send_json(
                {"error": "Unable to start analysis."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._send_json(job, HTTPStatus.ACCEPTED)

    def _authorized(self, *, require_origin: bool) -> bool:
        if not self._valid_host():
            return False
        host = self.headers.get("Host")
        assert host is not None
        if require_origin:
            origin = self.headers.get("Origin")
            if origin != f"http://{host}":
                self._send_json({"error": "Invalid origin."}, HTTPStatus.FORBIDDEN)
                return False
        if not secrets.compare_digest(
            self.headers.get("X-TradingAgents-UI-Token", ""),
            self.server.ui_token,
        ):
            self._send_json({"error": "Invalid UI token."}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def _valid_host(self) -> bool:
        host = self.headers.get("Host")
        expected_hosts = {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }
        if host not in expected_hosts:
            self._send_json({"error": "Invalid host."}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self._send_bytes(body, "application/json; charset=utf-8", status=status)

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        cache: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.send_header(
            "Cache-Control", "public, max-age=3600" if cache else "no-store"
        )
        self.end_headers()
        self.wfile.write(body)


def create_server(service: Any, *, port: int = 8765) -> DashboardHTTPServer:
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    # Deliberately no host parameter: the UI can never be exposed on a LAN by
    # accidentally passing 0.0.0.0. A reverse proxy/auth layer is out of scope.
    return DashboardHTTPServer(("127.0.0.1", port), service)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tradingagents-gx-ui",
        description="Open the loopback-only TradingAgents GX dashboard.",
    )
    parser.add_argument("--env-file", type=Path, help="GX/LLM environment profile")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser tab")
    args = parser.parse_args()
    if args.env_file is not None and (not args.env_file.is_file() or not os_access_read(args.env_file)):
        parser.error("--env-file must be a readable file")

    from tradingagents.ui.dashboard import DashboardService

    try:
        service = DashboardService(load_ui_config(args.env_file))
        server = create_server(service, port=args.port)
    except Exception as exc:  # noqa: BLE001 - startup diagnostics stay bounded
        parser.error(f"unable to start UI: {type(exc).__name__}")
    url = server.public_origin
    print(f"TradingAgents GX UI: {url}")
    print("Press Ctrl+C to stop. Runs execute only after clicking Run analysis.")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping TradingAgents GX UI.")
    finally:
        server.server_close()


def os_access_read(path: Path) -> bool:
    """Small seam for entrypoint tests without importing platform-specific helpers."""
    try:
        with path.open("rb"):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    main()
