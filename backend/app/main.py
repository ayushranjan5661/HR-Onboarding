from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import admin, auth, candidate, hr, manager

app = FastAPI(title="HR Onboarding System", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(hr.router)
app.include_router(manager.router)
app.include_router(admin.router)
app.include_router(candidate.router)


@app.get("/")
def health():
    return {"status": "ok", "service": "HR Onboarding System"}
