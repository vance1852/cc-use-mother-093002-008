"""统计资料接收、校验与版本发布服务的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from digital_trade_foundation.errors import DomainError
from digital_trade_foundation.storage import Database

from .service import StatService


def _created(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def route(service: StatService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到统计领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    segments = [segment for segment in parsed.path.split("/") if segment]
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            return 200, {"status": "ok"}
        if method == "GET" and segments == ["public", "stat", "releases"]:
            period = query.get("report_period", [None])[0]
            return 200, service.get_public_release(period)
        if segments[:1] == ["stat"]:
            rest = segments[1:]
            if method == "POST" and rest == ["rules-versions"]:
                return _created(service.register_rules_version(actor_id=actor_id, **body))
            if method == "GET" and rest == ["rules-versions"]:
                return 200, {"items": service.list_rules_versions(actor_id=actor_id)}
            if method == "POST" and rest == ["datasets"]:
                return _created(service.submit_dataset(actor_id=actor_id, **body))
            if method == "GET" and rest == ["datasets"]:
                return 200, {"items": service.list_datasets(
                    actor_id=actor_id,
                    organization_id=query.get("organization_id", [None])[0],
                    period=query.get("period", [None])[0])}
            if method == "GET" and len(rest) == 2 and rest[0] == "datasets":
                return 200, service.get_dataset(actor_id=actor_id, dataset_id=rest[1])
            if method == "POST" and rest == ["validations"]:
                return _created(service.enqueue_validation(actor_id=actor_id, **body))
            if method == "POST" and rest == ["validations", "process-next"]:
                return _created(service.process_next_validation(actor_id=actor_id, **body))
            if method == "GET" and rest == ["validations"]:
                return 200, {"items": service.list_validation_runs(
                    actor_id=actor_id, dataset_id=query.get("dataset_id", [None])[0])}
            if method == "GET" and rest == ["findings"]:
                return 200, {"items": service.list_findings(
                    actor_id=actor_id,
                    dataset_id=query.get("dataset_id", [None])[0],
                    run_id=query.get("run_id", [None])[0],
                    status=query.get("status", [None])[0])}
            if method == "POST" and rest == ["exemptions"]:
                return _created(service.approve_exemption(actor_id=actor_id, **body))
            if method == "POST" and rest == ["review-opinions"]:
                return _created(service.record_review_opinion(actor_id=actor_id, **body))
            if method == "GET" and rest == ["review-opinions"]:
                return 200, {"items": service.list_review_opinions(
                    actor_id=actor_id,
                    target_type=query.get("target_type", [""])[0],
                    target_id=query.get("target_id", [""])[0])}
            if method == "POST" and rest == ["analyst-scopes"]:
                return _created(service.grant_analyst_scope(actor_id=actor_id, **body))
            if method == "POST" and rest == ["derived-indicators"]:
                return _created(service.create_derived_indicator(actor_id=actor_id, **body))
            if method == "GET" and rest == ["derived-indicators"]:
                return 200, {"items": service.list_derived_indicators(
                    actor_id=actor_id,
                    rules_version=query.get("rules_version", [None])[0])}
            if method == "POST" and rest == ["candidates"]:
                return _created(service.assemble_candidate(actor_id=actor_id, **body))
            if method == "GET" and len(rest) == 2 and rest[0] == "candidates":
                return 200, service.get_candidate(actor_id=actor_id, candidate_id=rest[1])
            if method == "POST" and len(rest) == 3 and rest[0] == "candidates" \
                    and rest[2] == "countersign":
                return _created(service.countersign_candidate(
                    actor_id=actor_id, candidate_id=rest[1], **body))
            if method == "POST" and len(rest) == 3 and rest[0] == "candidates" \
                    and rest[2] == "publish":
                return _created(service.publish_candidate(
                    actor_id=actor_id, candidate_id=rest[1], **body))
            if method == "POST" and rest == ["locks", "acquire"]:
                return _created(service.acquire_lock(actor_id=actor_id, **body))
            if method == "POST" and rest == ["locks", "release"]:
                return _created(service.release_lock(actor_id=actor_id, **body))
            if method == "GET" and len(rest) == 2 and rest[0] == "locks":
                return 200, service.get_lock(actor_id=actor_id, lock_name=rest[1])
            if method == "GET" and rest == ["releases"]:
                return 200, {"items": service.list_releases(
                    actor_id=actor_id,
                    report_period=query.get("report_period", [None])[0])}
            if method == "GET" and len(rest) == 2 and rest[0] == "releases":
                return 200, service.get_release(actor_id=actor_id, version_id=rest[1])
            if method == "GET" and len(rest) == 3 and rest[0] == "releases" \
                    and rest[2] == "snapshot":
                return 200, service.get_snapshot(actor_id=actor_id, version_id=rest[1])
            if method == "GET" and len(rest) == 3 and rest[0] == "releases" \
                    and rest[2] == "recompute":
                return 200, service.recompute_indicator(
                    actor_id=actor_id, version_id=rest[1],
                    indicator_code=query.get("indicator_code", [""])[0])
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: StatService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动统计发布 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动统计资料接收、校验与版本发布服务")
    parser.add_argument("--database", default="stat_release.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = StatService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
