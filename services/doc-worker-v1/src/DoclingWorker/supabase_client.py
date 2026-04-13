"""Service-role Supabase client factory."""
from __future__ import annotations

from supabase import Client, create_client

from .config import Config


def get_client(cfg: Config) -> Client:
    return create_client(cfg.supabase_url, cfg.supabase_service_role_key)
