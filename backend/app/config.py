"""
Central app configuration. Reads from the .env file at the project root
(D:\\HR Onboarding\\.env). Never hardcode secrets here — everything sensitive
comes from environment variables.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- File uploads ---
    UPLOAD_DIR: str = "uploads"
    MAX_UPLOAD_MB: int = 10

    # --- Seed HR admin (used by init_db.py, first run only) ---
    SEED_HR_NAME: str = "HR Admin"
    SEED_HR_EMAIL: str = "hr@levelshift.com"
    SEED_HR_PASSWORD: str = "ChangeMe@123"

    model_config = SettingsConfigDict(
        env_file="../.env",
        extra="ignore",
    )

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.FRONTEND_ORIGINS.split(",") if o.strip()]


settings = Settings()
