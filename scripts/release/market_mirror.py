"""Refresh or serve the last validated Columba snapshot on the mirror host."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
from urllib import request

from packages.resonance_shared.market_mirror import (
    MARKET_PATH,
    MARKET_UPSTREAM,
    MAX_MARKET_BYTES,
    MarketMirrorError,
    MarketMirrorSnapshot,
    RejectRedirects,
    read_market_json,
    validate_market_payload,
)


def encode_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sync_snapshot(path: Path) -> None:
    opener = request.build_opener(request.ProxyHandler({}), RejectRedirects())
    req = request.Request(MARKET_UPSTREAM, headers={"User-Agent": "AuraMarketMirror/1"})
    with opener.open(req, timeout=12) as response:
        payload = validate_market_payload(read_market_json(response))
    snapshot = MarketMirrorSnapshot(datetime.now(timezone.utc), payload)
    body = encode_json(snapshot.to_dict())
    if len(body) > MAX_MARKET_BYTES:
        raise MarketMirrorError("Encoded market snapshot exceeds the response limit.")
    descriptor, filename = tempfile.mkstemp(dir=path.parent, prefix=".prices-")
    candidate = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(body)
            temporary.flush()
            os.fsync(temporary.fileno())
        candidate.replace(path)
    finally:
        candidate.unlink(missing_ok=True)
    print(f"Market snapshot updated at {snapshot.fetched_at.isoformat()}, products={len(payload['data'])}", flush=True)


class MarketHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != MARKET_PATH:
            self._reply(HTTPStatus.NOT_FOUND, b'{"error":"not_found"}')
            return
        try:
            with self.server.snapshot_path.open("rb") as stream:
                snapshot = MarketMirrorSnapshot.parse(read_market_json(stream))
            body = encode_json(snapshot.to_dict())
        except (OSError, ValueError, MarketMirrorError):
            self._reply(HTTPStatus.SERVICE_UNAVAILABLE, b'{"error":"market_unavailable"}')
            return
        self._reply(HTTPStatus.OK, body)

    def _reply(self, status: HTTPStatus, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("serve", "sync"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18444)
    args = parser.parse_args()
    snapshot_path = args.state / "prices.json"
    if args.operation == "sync":
        sync_snapshot(snapshot_path)
    else:
        with ThreadingHTTPServer(("127.0.0.1", args.port), MarketHandler) as server:
            server.snapshot_path = snapshot_path
            server.serve_forever()


if __name__ == "__main__":
    main()
