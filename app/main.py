import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import init_db, now, transaction
from .routers import auth, documents, uploads, users
from .security import hash_password

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fidela")

DEMO_USERS = [
    ("manager@fidela.local", "Daniel Ortiz", "manager", "Manager-2026!"),
    ("translator@fidela.local", "Laura Gomez", "translator", "Translator-2026!"),
    ("client@fidela.local", "Maria Client", "client", "Client-2026!"),
]


def seed_demo_users() -> None:
    """Only on an empty database, for local development (disable with FIDELA_SEED_DEMO_USERS=0)."""
    with transaction() as db:
        if db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        for email, name, role, password in DEMO_USERS:
            db.execute(
                "INSERT INTO users (email, full_name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (email, name, hash_password(password), role, now()),
            )
    log.warning("Created demo users (change these before any real use):")
    for email, _, role, password in DEMO_USERS:
        log.warning("  %-11s %-26s %s", role, email, password)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if settings.seed_demo_users:
        seed_demo_users()
    if not settings.gemini_api_key:
        log.warning("GEMINI_API_KEY is not set: uploads will be stored but AI processing will fail")
    yield


app = FastAPI(title="Fidela API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth.router)
app.include_router(uploads.router)
app.include_router(documents.router)
app.include_router(users.router)


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok", "ai_configured": bool(settings.gemini_api_key)}
