from contextlib import asynccontextmanager
import traceback

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import make_pool
from .routers import admin, auth, me, payouts, review, tickets, work


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = make_pool(get_settings().database_url)
    pool.open(wait=True, timeout=15)
    app.state.pool = pool
    yield
    pool.close()


app = FastAPI(title="Ekyama validation API", version="0.1.0", lifespan=lifespan)

# Allow exact configured origins plus any Vercel preview or production deployments
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().origins,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"detail": f"INTERNAL_ERROR: {type(exc).__name__}: {str(exc)}"},
    )


for r in (auth.router, me.router, work.router, payouts.router, admin.router, review.router, tickets.router):
    app.include_router(r)


@app.get("/health", tags=["meta"])
def health():
    with app.state.pool.connection() as conn:
        conn.execute("select 1")
    return {"status": "ok", "version": "v1.0.4-resilient"}


__all__ = ["app"]
