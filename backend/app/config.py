"""
Central app configuration. Reads from the .env file at the project root
(D:\\HR Onboarding\\.env). Never hardcode secrets here — everything sensitive
comes from environment variables.
"""
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchor the .env lookup to this file, not the process CWD — starting uvicorn
# from any other directory must not silently drop every configured secret.
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    # --- Database ---
    DATABASE_URL: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/hr_onboarding"

    # --- Auth / JWT ---
    JWT_SECRET_KEY: str = "change-me-in-env"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 8  # 8 hours

    # --- CORS ---
    FRONTEND_ORIGINS: str = "http://127.0.0.1:5500,http://localhost:5500,http://127.0.0.1:8080,http://localhost:8080"

    # --- Portal (used to build the candidate's direct login URL) ---
    PORTAL_BASE_URL: str = "http://127.0.0.1:5500"
    # How long a one-click invite link stays valid. HR can always reissue.
    INVITE_LINK_EXPIRY_DAYS: int = 30

    # --- AI cross-form mapping agent (Azure OpenAI) ---
    # Reads the VITE_*-prefixed values already present in .env.
    # Accept either spelling: the project's .env stores these VITE_-prefixed.
    AZURE_OPENAI_ENDPOINT: str = Field(
        default="", validation_alias=AliasChoices(
            "AZURE_OPENAI_ENDPOINT", "VITE_AZURE_OPENAI_ENDPOINT"))
    AZURE_OPENAI_API_KEY: str = Field(
        default="", validation_alias=AliasChoices(
            "AZURE_OPENAI_API_KEY", "VITE_AZURE_OPENAI_API_KEY"))
    AZURE_OPENAI_API_VERSION: str = Field(
        default="2025-04-01-preview", validation_alias=AliasChoices(
            "AZURE_OPENAI_API_VERSION", "VITE_AZURE_OPENAI_API_VERSION"))
    AZURE_OPENAI_DEPLOYMENT: str = Field(
        default="", validation_alias=AliasChoices(
            "AZURE_OPENAI_DEPLOYMENT", "VITE_AZURE_OPENAI_DEPLOYMENT"))
    AI_MAPPING_ENABLED: bool = True
    AI_MAPPING_TIMEOUT: int = 30
    # LLM proposals below this confidence are discarded.
    AI_MAPPING_MIN_CONFIDENCE: float = 0.75

    # --- AI candidate-insight agent (CIF summary + anomaly flags) ---
    AI_INSIGHTS_ENABLED: bool = True
    AI_INSIGHTS_TIMEOUT: int = 30

    # --- Document validation (app/agents/doc_validator.py) ---
    # Checks that an uploaded file is the kind of document its field asks for
    # (a PAN card in the Aadhaar slot, a transfer certificate as a 10th
    # marksheet, ...). Entirely local: Tesseract OCR reads the text and a
    # keyword/pattern scorer rates it. Nothing is sent to any AI service —
    # identity documents are sensitive and stay on this server.
    #   off   -> never run; nothing is recorded
    #   warn  -> record the score and show it to the candidate and HR, but
    #            never refuse the upload (the safe default while the scorer's
    #            accuracy on real uploads is still being measured)
    #   block -> refuse an upload scoring below MISMATCH_SCORE; anything in
    #            between is accepted and flagged for HR
    DOC_VALIDATION_MODE: str = "warn"
    # Score (0-100) at or above which the file counts as the right document,
    # and below which it counts as the wrong one. In between = "please check".
    DOC_VALIDATION_MATCH_SCORE: int = 80
    DOC_VALIDATION_MISMATCH_SCORE: int = 40
    # PDFs: the embedded text layer is used when there is one (e-Aadhaar,
    # bank statements); scanned pages are rasterised and OCR'd, this many.
    DOC_VALIDATION_MAX_PAGES: int = 2
    # Tesseract. TESSERACT_CMD is only needed when it is not on PATH;
    # TESSDATA_DIR holds the language packs. backend/tessdata ships English
    # plus every major Indian script (Devanagari for Hindi/Marathi/Nepali,
    # Bengali/Assamese, Odia, Gurmukhi, Gujarati, Tamil, Telugu, Kannada,
    # Malayalam, Urdu), because candidates' Aadhaar cards and board
    # certificates are printed in English plus their state's language.
    # Two passes: OCR_PRIMARY_LANGUAGES first (fast, and the most accurate
    # read of the English that carries most documents); if that is not a
    # clear match, OCR_LANGUAGES — "auto" = every pack found in
    # TESSDATA_DIR — reads the regional half too, and the better-scoring
    # pass wins. Or list packs explicitly: "eng+hin+tam".
    TESSERACT_CMD: str = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    TESSDATA_DIR: str = "tessdata"
    OCR_PRIMARY_LANGUAGES: str = "eng+hin"
    OCR_LANGUAGES: str = "auto"
    OCR_TIMEOUT: int = 30      # seconds per page; a hung OCR must not hang the upload

    @field_validator("TESSDATA_DIR")
    @classmethod
    def _anchor_tessdata_dir(cls, v: str) -> str:
        # Relative means backend/<dir>, whatever the server's CWD is.
        path = Path(v)
        if v and not path.is_absolute():
            path = Path(__file__).resolve().parents[1] / path
        return str(path) if v else ""

    # --- Zoho People push (integrations/zoho/zoho_client.py) ---
    # Both URLs must match the data centre where the OAuth client was created.
    ZOHO_ACCOUNTS_BASE_URL: str = "https://accounts.zoho.in"
    ZOHO_PEOPLE_BASE_URL: str = "https://people.zoho.in"
    ZOHO_CLIENT_ID: str = ""
    ZOHO_CLIENT_SECRET: str = ""
    ZOHO_REFRESH_TOKEN: str = ""
    # Stopgap for a one-hour test window when you only have an access token.
    # Set it and no refresh happens; expect 401s once it lapses.
    ZOHO_ACCESS_TOKEN: str = ""
    # Read-only fallback only. Writes always require an explicit --form so a
    # stale .env value can never silently target the live Candidate form.
    ZOHO_CANDIDATE_FORM: str = ""
    # The ONLY value the "Publish to Zoho People" HR-portal button writes to.
    # Deliberately separate from ZOHO_CANDIDATE_FORM above — set this only once
    # the payload has round-tripped against that form via the CLI (see
    # integrations/zoho/README.md). Leave blank to keep the button disabled.
    ZOHO_CANDIDATE_WRITE_FORM: str = ""
    # The second button, "Publish to Zoho Candidate Form", writes ONLY here
    # (normally "Candidate"), with integrations/zoho/field_map_candidate.json.
    # Blank keeps that button refusing, independent of the one above.
    ZOHO_CANDIDATE_PROFILE_WRITE_FORM: str = ""
    ZOHO_TIMEOUT: int = 30

    # --- File uploads ---
    UPLOAD_DIR: str = "uploads"
    MAX_UPLOAD_MB: int = 10

    @field_validator("UPLOAD_DIR")
    @classmethod
    def _anchor_upload_dir(cls, v: str) -> str:
        # A relative UPLOAD_DIR must always mean backend/<dir>, no matter which
        # directory the server was started from — otherwise a restart from a
        # different CWD strands every previously uploaded document.
        path = Path(v)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[1] / path
        return str(path)

    # --- Seed Super Admin (used by init_db.py, first run only) ---
    SEED_HR_NAME: str = "HR Admin"
    SEED_HR_EMAIL: str = "hr@levelshift.com"
    SEED_HR_PASSWORD: str = "ChangeMe@123"

    # --- Seed Master Admin: the developer account above the Super Admin. ---
    # The only way this role comes into being; the API never creates one.
    # Leave the password blank and init_db skips it.
    SEED_MASTER_NAME: str = "Master Admin"
    SEED_MASTER_EMAIL: str = "master@levelshift.com"
    SEED_MASTER_PASSWORD: str = ""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        extra="ignore",
    )

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.FRONTEND_ORIGINS.split(",") if o.strip()]


settings = Settings()

# Tokens signed with the placeholder secret are forgeable by anyone who has
# read this file, so refuse to start rather than run with it.
if settings.JWT_SECRET_KEY == "change-me-in-env":
    raise RuntimeError(
        f"JWT_SECRET_KEY is still the built-in placeholder. Set a strong random "
        f"value in {_ENV_FILE} (or the environment) before starting the server."
    )
