"""Kjør én gang: python -m agent.bootstrap_creds
Printer CLOB API-credentials derivert fra private key. Lagres ikke automatisk.
"""
from __future__ import annotations

from agent.config import settings


def main() -> None:
    if not settings.private_key:
        raise SystemExit("Sett POLYMARKET_PRIVATE_KEY i .env først")
    from py_clob_client.client import ClobClient

    client = ClobClient(
        settings.clob_host,
        key=settings.private_key,
        chain_id=settings.chain_id,
        signature_type=settings.signature_type,
        funder=settings.funder or None,
    )
    fn = getattr(client, "create_or_derive_api_creds", None) or getattr(
        client, "create_or_derive_api_key", None
    )
    if not fn:
        raise SystemExit("Denne SDK-versjonen har ikke create_or_derive_*")
    creds = fn()
    print("Lim inn i .env:")
    print(f"POLY_API_KEY={getattr(creds, 'api_key', creds)}")
    print(f"POLY_API_SECRET={getattr(creds, 'api_secret', '')}")
    print(f"POLY_API_PASSPHRASE={getattr(creds, 'api_passphrase', '')}")


if __name__ == "__main__":
    main()
