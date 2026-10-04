"""统计服务的 HTTP/JSON 边界，与基础服务路由组合。

公共接口（/public/*）不要求操作者身份，只返回允许公开的结果。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from digital_trade_foundation.api import route as foundation_route
from digital_trade_foundation.errors import DomainError, ValidationError
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database

from .service import StatisticsService


def route_statistics(service: StatisticsService, method: str, path: str,
                     body: dict[str, Any] | None,
                     headers: dict[str, str] | None) -> tuple[int, dict[str, Any]] | None:
    """把统计语义请求分派到统计服务；路径不匹配时返回 None。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)

    def arg(name: str) -> str | None:
        return query.get(name, [None])[0]

    def receipt_response(receipt) -> tuple[int, dict[str, Any]]:
        return (200 if receipt.replayed else 201), receipt.__dict__

    if method == "POST" and parsed.path == "/statistics/rule-versions":
        return receipt_response(service.register_rule_version(actor_id=actor_id, **body))
    if method == "POST" and parsed.path == "/statistics/economies":
        return receipt_response(service.register_economy(actor_id=actor_id, **body))
    if method == "POST" and parsed.path == "/statistics/fx-rates":
        return receipt_response(service.register_fx_rate(actor_id=actor_id, **body))
    if method == "POST" and parsed.path == "/statistics/class-mappings":
        return receipt_response(service.register_class_mapping(actor_id=actor_id, **body))
    if method == "POST" and parsed.path == "/statistics/analyst-grants":
        return receipt_response(service.grant_analyst_scope(actor_id=actor_id, **body))
    if method == "POST" and parsed.path == "/statistics/datasets":
        return receipt_response(service.submit_dataset(actor_id=actor_id, **body))
    if method == "GET" and parsed.path == "/statistics/datasets":
        return 200, {"items": service.list_datasets(
            actor_id=actor_id, organization_id=arg("organization_id"),
            economy_code=arg("economy_code"), period=arg("period"))}
    if method == "POST" and parsed.path == "/statistics/validation/process":
        limit = body.get("limit")
        return 200, service.process_validation_queue(actor_id=actor_id, limit=limit)
    if method == "GET" and parsed.path == "/statistics/validation-runs":
        return 200, {"items": service.list_validation_runs(
            actor_id=actor_id, dataset_id=arg("dataset_id"))}
    if method == "GET" and parsed.path == "/statistics/validation-findings":
        run_id = arg("run_id")
        if not run_id:
            raise ValidationError("run_id 不能为空")
        return 200, {"items": service.list_findings(actor_id=actor_id, run_id=run_id)}
    if method == "POST" and parsed.path == "/statistics/exemptions":
        return receipt_response(service.approve_exemption(actor_id=actor_id, **body))
    if method == "POST" and parsed.path == "/statistics/reviews":
        return receipt_response(service.add_review(actor_id=actor_id, **body))
    if method == "GET" and parsed.path == "/statistics/reviews":
        return 200, {"items": service.list_reviews(
            actor_id=actor_id, target_type=arg("target_type") or "",
            target_id=arg("target_id") or "")}
    if method == "POST" and parsed.path == "/statistics/candidates":
        return receipt_response(service.create_candidate(actor_id=actor_id, **body))
    if method == "GET" and parsed.path == "/statistics/candidates":
        candidate_id = arg("candidate_id")
        if not candidate_id:
            raise ValidationError("candidate_id 不能为空")
        return 200, service.candidate_report(actor_id=actor_id, candidate_id=candidate_id)
    if method == "POST" and parsed.path == "/statistics/cosignatures":
        return receipt_response(service.cosign_candidate(actor_id=actor_id, **body))
    if method == "POST" and parsed.path == "/statistics/release-lock/acquire":
        return 200, service.acquire_release_lock(actor_id=actor_id,
                                                 candidate_id=body.get("candidate_id", ""))
    if method == "POST" and parsed.path == "/statistics/release-lock/release":
        return 200, service.release_release_lock(actor_id=actor_id,
                                                 candidate_id=body.get("candidate_id", ""))
    if method == "GET" and parsed.path == "/statistics/release-lock":
        return 200, {"lock": service.lock_state(actor_id=actor_id)}
    if method == "POST" and parsed.path == "/statistics/releases/publish":
        return receipt_response(service.publish_release(actor_id=actor_id, **body))
    if method == "GET" and parsed.path == "/statistics/releases":
        return 200, {"items": service.list_releases(actor_id=actor_id)}
    if method == "GET" and parsed.path == "/statistics/release-metrics":
        release_id = arg("release_id")
        if not release_id:
            raise ValidationError("release_id 不能为空")
        return 200, service.release_metrics(actor_id=actor_id, release_id=release_id,
                                            indicator=arg("indicator"))
    if method == "POST" and parsed.path == "/statistics/corrections":
        return receipt_response(service.create_correction(actor_id=actor_id, **body))
    if method == "GET" and parsed.path == "/statistics/corrections":
        return 200, {"items": service.list_corrections(actor_id=actor_id,
                                                       release_id=arg("release_id"))}
    if method == "POST" and parsed.path == "/statistics/derived-indicators":
        return receipt_response(service.create_derived_indicator(actor_id=actor_id, **body))
    if method == "GET" and parsed.path == "/statistics/recompute":
        release_id = arg("release_id")
        if not release_id:
            raise ValidationError("release_id 不能为空")
        return 200, service.recompute_release(actor_id=actor_id, release_id=release_id,
                                              indicator=arg("indicator"))
    if method == "GET" and parsed.path == "/public/releases":
        return 200, {"items": service.public_releases()}
    if method == "GET" and parsed.path == "/public/indicators":
        return 200, service.public_indicators(release_id=arg("release_id"),
                                              indicator=arg("indicator"))
    return None


def route(domain: DomainService, statistics: StatisticsService, method: str, path: str,
          body: dict[str, Any] | None, headers: dict[str, str] | None = None
          ) -> tuple[int, dict[str, Any]]:
    """先匹配统计路由，再回落到基础服务路由。"""

    try:
        result = route_statistics(statistics, method, path, body, headers)
        if result is not None:
            return result
        return foundation_route(domain, method, path, body, headers)
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为组合路由调用。"""

    domain: DomainService
    statistics: StatisticsService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.domain, self.statistics, self.command, self.path, body,
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
    """启动统计资料接收、校验与版本发布服务。"""

    parser = argparse.ArgumentParser(description="统计资料接收、校验与版本发布服务")
    parser.add_argument("--database", default="statistics.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.domain = DomainService(database)
    Handler.statistics = StatisticsService(database)
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
