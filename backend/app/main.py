from app.utils import dns_fallback

# Before anything opens a connection: this machine's endpoint protection
# blocks Python's own DNS when the server runs as a program, which broke
# every Azure OpenAI and Zoho call. See app/utils/dns_fallback.py.
dns_fallback.install()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import admin, auth, candidate, hr, manager, referee

app = FastAPI(title="HR Onboarding System", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Lets the page read the download filename (e.g. the Reference Check PDF).
    expose_headers=["Content-Disposition"],
)

app.include_router(auth.router)
app.include_router(hr.router)
app.include_router(manager.router)
app.include_router(admin.router)
app.include_router(candidate.router)
app.include_router(referee.router)


@app.get("/")
def health():
    return {"status": "ok", "service": "HR Onboarding System"}
