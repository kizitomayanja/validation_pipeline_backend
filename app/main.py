from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import make_pool
from .routers import admin, auth, me, payouts, review, work


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = make_pool(get_settings().database_url)
    pool.open(wait=True, timeout=15)
    app.state.pool = pool
    yield
    pool.close()


app = FastAPI(title="Ekyama validation API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
for r in (auth.router, me.router, work.router, payouts.router, admin.router, review.router):
    app.include_router(r)


@app.get("/health", tags=["meta"])
def health():
    with app.state.pool.connection() as conn:
        conn.execute("select 1")
    return {"status": "ok"}


__all__ = ["app"]
