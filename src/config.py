"""Central configuration. Every secret/tunable is read from the .env file
(or real environment variables on the host). Nothing is hardcoded — see
.env.example for the full list."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- WhatsApp Baileys bridge (localhost only) ---
    baileys_bridge_url: str = "http://localhost:3001"
    bridge_port: int = 3001
    python_webhook_url: str = "http://localhost:8000/webhook/whatsapp"
    wa_session_path: str = "./auth_session"

    # --- Database ---
    database_path: str = "./seatwatch.db"

    # --- Banner 9 (AUC registration site) ---
    # Host only, no trailing path. Verified live 2026-06-30.
    banner_base_url: str = "https://reg-prod.ec.aucegypt.edu"
    # Self-Service path prefix.
    banner_path_prefix: str = "/StudentRegistrationSsb/ssb"
    # Default active registration term. 202710 = Fall 2026 (confirmed via getTerms).
    banner_term: str = "202710"
    banner_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )

    # --- Admin page (no login; secret slug in the URL) ---
    admin_key: str = "change-me-admin-secret"

    # --- Shared secret between the Baileys bridge and this app ---
    # If set, /webhook/whatsapp rejects any POST without this token, so the
    # public port can't be used to spoof inbound WhatsApp messages.
    bridge_token: str = ""

    # --- Poller ---
    check_interval_minutes: int = 5

    # --- Public base URL (used in links shown to users) ---
    public_base_url: str = "http://localhost:8000"


settings = Settings()
