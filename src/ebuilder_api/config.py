from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EBuilderConfig:
    """Runtime configuration for e-Builder API access."""

    base_url: str
    api_token: str | None = None
    username: str | None = None
    password: str | None = None
    grant_type: str = "password"
    timeout_seconds: float = 30.0
    verify_ssl: bool = True
    user_agent: str = "ebuilder-api-wrapper/0.1.0"

    @classmethod
    def from_env(cls) -> "EBuilderConfig":
        """Create config from environment variables.

        Required:
        - EBUILDER_BASE_URL
        Optional auth mode #1:
        - EBUILDER_API_TOKEN

        Optional auth mode #2:
        - EBUILDER_USERNAME
        - EBUILDER_PASSWORD
        """
        base_url = os.getenv("EBUILDER_BASE_URL", "").strip()
        api_token = os.getenv("EBUILDER_API_TOKEN", "").strip() or None
        username = os.getenv("EBUILDER_USERNAME", "").strip() or None
        password = os.getenv("EBUILDER_PASSWORD", "").strip() or None
        timeout_seconds = float(os.getenv("EBUILDER_TIMEOUT_SECONDS", "30"))
        verify_ssl = os.getenv("EBUILDER_VERIFY_SSL", "true").lower() != "false"

        if not base_url:
            raise ValueError("Missing required env var: EBUILDER_BASE_URL")
        if not api_token and not (username and password):
            raise ValueError(
                "Provide either EBUILDER_API_TOKEN or both EBUILDER_USERNAME and EBUILDER_PASSWORD"
            )

        return cls(
            base_url=base_url.rstrip("/"),
            api_token=api_token,
            username=username,
            password=password,
            timeout_seconds=timeout_seconds,
            verify_ssl=verify_ssl,
        )
