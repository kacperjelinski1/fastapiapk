from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import settings
from app.database import engine
from app.routers import auth, receptions, clients, finances, calls, sms_reviews, imports, users, assist, warranty, top_secret, settings as settings_router

app = FastAPI(
    title="Multi-Servis API",
    version="2.0.0",
    description="Centralne API dla Multi-Servis Android, serwisu, klientów, nagrań, SMS, opinii i integracji Assist/Breeze.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (
    auth.router,
    receptions.router,
    clients.router,
    finances.router,
    calls.router,
    sms_reviews.router,
    imports.router,
    users.router,
    assist.router,
    warranty.router,
    top_secret.router,
    settings_router.router,
):
    app.include_router(router)


@app.get("/")
def root():
    return {
        "app": "Multi-Servis API",
        "status": "running",
        "version": "2.0.0",
        "auth_required": settings.auth_required,
    }


@app.get("/health")
def health():
    try:
        with engine.connect() as connection:
            result = connection.execute(
                text("SELECT current_database() database_name,current_user database_user,version() postgres_version")
            ).mappings().one()
            schemas = connection.execute(
                text("SELECT schema_name FROM information_schema.schemata WHERE schema_name IN (core,service,assist,breeze,config) ORDER BY schema_name")
            ).scalars().all()
        return {
            "status": "ok",
            "database": "connected",
            "database_name": result["database_name"],
            "database_user": result["database_user"],
            "postgres_version": result["postgres_version"],
            "schemas": schemas,
        }
    except Exception as exc:
        return {"status":"error","database":"disconnected","error":str(exc)}


@app.get("/capabilities")
def capabilities():
    return {
        "receptions": ["create","list","search","details","edit","status","cancel","notify-client","status-history","notes","media","ocr"],
        "clients": ["list","search","by-phone","details","edit","phones","devices","history"],
        "owner_finances": ["get","update","reports"],
        "calls": ["list","create","match-phone","attach-reception","recording-upload","recording-stream"],
        "sms": ["import","list","auto-match"],
        "reviews": ["create","list","verify","reject"],
        "phone_import": ["contacts","unsaved-numbers","selected-contacts","calls"],
        "users": ["login-ready","roles","create","edit","disable","password"],
        "assist": ["client-installations","licenses","alerts"],
        "top_secret": ["read","write"],
        "settings": ["read"],
    }
