import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """Minimal .env support (KEY=VALUE lines); real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(BASE_DIR / ".env")


class Settings:
    data_dir = Path(os.environ.get("FIDELA_DATA_DIR", BASE_DIR / "data"))
    database_path = data_dir / "fidela.sqlite3"
    storage_dir = data_dir / "storage"

    gemini_api_key = os.environ.get("GEMINI_API_KEY", "")
    cors_origins = [o.strip() for o in os.environ.get("FIDELA_CORS_ORIGINS", "http://localhost:4200").split(",") if o.strip()]
    seed_demo_users = os.environ.get("FIDELA_SEED_DEMO_USERS", "1") == "1"

    # Email (Brevo). Without an API key, emails are printed to the console instead of sent.
    brevo_api_key = os.environ.get("BREVO_API_KEY", "")
    mail_from = os.environ.get("BREVO_SENDER_EMAIL", "support@fidelatranslations.ca")
    mail_from_name = os.environ.get("BREVO_SENDER_NAME", "Fidela Translations")
    # Development safety net: when set, every email goes to this address instead.
    mail_redirect = os.environ.get("FIDELA_MAIL_REDIRECT", "").strip()
    staff_emails = [e.strip() for e in os.environ.get("FIDELA_STAFF_EMAILS", "").split(",") if e.strip()]
    public_url = os.environ.get("FIDELA_PUBLIC_URL", "http://localhost:4200").rstrip("/")

    set_password_hours = 72
    reset_password_minutes = 60

    access_token_hours = 12
    page_token_minutes = 120

    max_files_per_upload = 10
    max_file_mb = 20
    allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff"}

    @property
    def secret_key(self) -> str:
        """FIDELA_SECRET_KEY, or a random key generated once and kept in the data folder."""
        if key := os.environ.get("FIDELA_SECRET_KEY"):
            return key
        path = self.data_dir / "secret.key"
        if not path.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
            path.chmod(0o600)
        return path.read_text(encoding="utf-8").strip()


settings = Settings()
