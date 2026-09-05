"""FastAPI application for GoRules JDM Studio."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from backend.api import (
    auth, chat, exports, graphs, health, imports, insights, lint, simulate, tests,
)
from backend.api.errors import register_error_handlers
from backend.config import settings
from backend import agent_runtime
from backend import corpus
from backend.db import bootstrap, connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address, default_limits=[settings.rate_limit_default])


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connection.connect()
    await bootstrap.bootstrap()
    if not settings.session_secret:
        logger.warning(
            "SESSION_SECRET is not set: a new signing key is generated on every "
            "start, so guests lose their session (and the policies attached to "
            "it) whenever the API restarts. Set it in backend/.env."
        )
    await agent_runtime.startup()
    # Opened at boot rather than lazily on the first model call, so a misconfigured path
    # or an unwritable directory is reported here instead of silently costing the first
    # few samples of the session.
    corpus.open_at_boot()
    logger.info("%s %s ready (db=%s)", settings.app_name, settings.version, settings.db_path)
    try:
        yield
    finally:
        await agent_runtime.shutdown()
        corpus.store.close()
        await connection.disconnect()


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    lifespan=lifespan,
)

app.state.limiter = limiter

# Credentials are required: the session travels in a cookie so that
# EventSource, which cannot set headers, still authenticates the chat stream.
# The CORS spec forbids pairing this with a wildcard origin, so the origin list
# must stay explicit.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request.state.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


register_error_handlers(app)


@app.exception_handler(RateLimitExceeded)
async def _rate_limited(request: Request, exc: RateLimitExceeded):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "code": "RATE_LIMITED",
                "message": "Too many requests; slow down.",
                "detail": str(exc.detail),
                "request_id": getattr(request.state, "request_id", None),
            }
        },
    )


app.include_router(health.router)
app.include_router(auth.router)
# exports before graphs: FastAPI matches in registration order, and the literal
# /api/graphs/export-all would otherwise be swallowed by /api/graphs/{graph_id}.
app.include_router(exports.router)
app.include_router(graphs.router)
app.include_router(imports.router)
app.include_router(simulate.router)
app.include_router(tests.router)
app.include_router(lint.router)
app.include_router(insights.router)
app.include_router(chat.router)
