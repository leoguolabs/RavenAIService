"""
请求日志中间件
记录所有API请求的详细信息
"""

import time
import json
import logging
import uuid
from typing import Callable, Optional
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.utils.metrics import record_http_request

logger = logging.getLogger(__name__)

SENSITIVE_HEADER_NAMES = {"authorization", "cookie", "set-cookie"}
SENSITIVE_BODY_PATHS = {
    "/admin/auth/login",
    "/api/v1/users/auth/login",
    "/api/v1/users/auth/register",
    "/api/v1/users/auth/change-password",
}


def _route_template(request: Request) -> str:
    """Return the matched route template (e.g. ``/api/v1/logs/{log_id}``).

    Using the template instead of the raw path keeps the Prometheus ``route``
    label low-cardinality (design Decision 2). Unmatched paths (404s) collapse
    to ``unmatched`` rather than leaking the raw, high-cardinality URL.
    """
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    return "unmatched"


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """请求日志中间件"""
    
    def __init__(
        self,
        app: ASGIApp,
        exclude_paths: list = None,
        include_body: bool = False,
        max_body_size: int = 1024
    ):
        super().__init__(app)
        self.exclude_paths = exclude_paths or ["/health", "/docs", "/redoc", "/openapi.json"]
        self.include_body = include_body
        self.max_body_size = max_body_size

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """处理请求"""
        # 生成请求ID
        request_id = str(uuid.uuid4())
        
        # 跳过不需要记录的路径
        if any(request.url.path.startswith(path) for path in self.exclude_paths):
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        
        # 记录请求开始时间
        start_time = time.time()
        
        # 获取请求信息
        request_info = await self._get_request_info(request, request_id)
        
        # 记录请求开始日志
        logger.debug(f"Request started: {json.dumps(request_info, ensure_ascii=False)}")
        
        # 处理请求
        try:
            response = await call_next(request)
            
            # 计算处理时间
            process_time = time.time() - start_time
            
            # 记录响应信息
            response_info = self._get_response_info(response, process_time, request_id)
            
            # 记录请求完成日志
            logger.debug(f"Request completed: {json.dumps(response_info, ensure_ascii=False)}")
            
            # 添加响应头
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time"] = str(process_time)

            # 记录 Prometheus HTTP 指标（使用路由模板，低基数；从不抛出）
            record_http_request(
                method=request.method,
                route=_route_template(request),
                status_code=response.status_code,
                duration_seconds=process_time,
            )

            return response

        except Exception as e:
            # 计算处理时间
            process_time = time.time() - start_time

            # 记录错误信息
            error_info = {
                "request_id": request_id,
                "error": str(e),
                "error_type": type(e).__name__,
                "process_time": process_time
            }

            logger.error(f"Request failed: {json.dumps(error_info, ensure_ascii=False)}")

            # 未处理异常按 500 计入 Prometheus，再重新抛出交由异常处理器
            record_http_request(
                method=request.method,
                route=_route_template(request),
                status_code=500,
                duration_seconds=process_time,
            )

            # 重新抛出异常
            raise e

    async def _get_request_info(self, request: Request, request_id: str) -> dict:
        """获取请求信息"""
        # 基础信息
        request_info = {
            "request_id": request_id,
            "method": request.method,
            "url": str(request.url),
            "path": request.url.path,
            "query_params": dict(request.query_params),
            "headers": self._sanitize_headers(dict(request.headers)),
            "client_ip": self._get_client_ip(request),
            "user_agent": request.headers.get("user-agent", ""),
            "timestamp": time.time()
        }
        
        # 获取请求体（如果启用且不是文件上传）
        if (
            self.include_body
            and request.method in ["POST", "PUT", "PATCH"]
            and request.url.path.rstrip("/") not in SENSITIVE_BODY_PATHS
        ):
            content_type = request.headers.get("content-type", "")
            
            # 不记录文件上传请求的body
            if not content_type.startswith("multipart/form-data"):
                try:
                    body = await request.body()
                    if len(body) <= self.max_body_size:
                        if content_type.startswith("application/json"):
                            request_info["body"] = json.loads(body.decode("utf-8"))
                        else:
                            request_info["body"] = body.decode("utf-8", errors="ignore")
                    else:
                        request_info["body"] = f"<body too large: {len(body)} bytes>"
                except Exception as e:
                    request_info["body"] = f"<failed to read body: {str(e)}>"
        
        return request_info

    def _get_response_info(self, response: Response, process_time: float, request_id: str) -> dict:
        """获取响应信息"""
        return {
            "request_id": request_id,
            "status_code": response.status_code,
            "headers": self._sanitize_headers(dict(response.headers)),
            "process_time": process_time,
            "timestamp": time.time()
        }

    def _sanitize_headers(self, headers: dict) -> dict:
        """Mask credentials before request/response headers are written to logs."""
        return {
            key: ("***" if key.lower() in SENSITIVE_HEADER_NAMES else value)
            for key, value in headers.items()
        }

    def _get_client_ip(self, request: Request) -> str:
        """获取客户端IP地址"""
        # 优先从代理头获取真实IP
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip
        
        # 获取远程地址
        if hasattr(request.client, "host"):
            return request.client.host
        
        return "unknown"
