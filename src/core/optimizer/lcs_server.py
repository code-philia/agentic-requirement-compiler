"""Local HTTP bridge between ARC output traces and the trajectory inspector."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core.optimizer.lcp_analysis import analysis_data, analyze, load_trajectories


def available_runs(outputs: Path) -> list[str]:
    if not outputs.is_dir():
        return []
    return sorted(path.name for path in outputs.iterdir()
                  if path.is_dir() and (path / ".arc" / "debug.log").is_file())


def handler_for(outputs: Path):
    outputs = outputs.resolve()

    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload: object, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "http://localhost:3000")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path not in {"/runs", "/analysis"}:
                self._json({"error": "not found"}, 404)
                return
            runs = available_runs(outputs)
            if parsed.path == "/runs":
                self._json({"runs": runs})
                return
            requested = parse_qs(parsed.query).get("run", [""])[0]
            if requested not in runs:
                self._json({"error": "unknown ARC run", "runs": runs}, 404)
                return
            workspace = (outputs / requested).resolve()
            if outputs not in workspace.parents:
                self._json({"error": "invalid ARC run path"}, 400)
                return
            trajectories = load_trajectories(workspace)
            # The inspector is deliberately exhaustive: include same-agent and
            # cross-agent pairs and let the UI expose even zero/low matches.
            results = analyze(trajectories, cross_agent=True)
            self._json({"runs": runs, "run": requested,
                        **analysis_data(trajectories, results)})

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve local ARC trajectory analysis")
    parser.add_argument("outputs", type=Path, nargs="?", default=Path("outputs"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(args.outputs))
    print(f"ARC trajectory data: http://localhost:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
