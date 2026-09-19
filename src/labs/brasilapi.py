"""Cliente HTTP compartilhado da BrasilAPI.

A BrasilAPI pede explicitamente que o volume venha de requisicoes reais, nao de
scraping automatizado. Por isso: timeout curto, um unico client reaproveitado e
cache em memoria. Nos labs ainda somamos CachePolicy no no do grafo.
"""

from __future__ import annotations

from functools import lru_cache

import httpx

BASE_URL = "https://brasilapi.com.br/api"

_client = httpx.Client(
    base_url=BASE_URL, timeout=10.0, headers={"User-Agent": "langgraph-labs/0.1"}
)


class BrasilAPIError(RuntimeError):
    """Erro de negocio da BrasilAPI (404, 400...), ja com mensagem legivel."""


@lru_cache(maxsize=256)
def get(path: str) -> dict | list:
    """GET em um caminho relativo, ex.: "/cep/v2/68502290"."""
    try:
        resp = _client.get(path)
    except httpx.RequestError as exc:  # rede, DNS, timeout
        raise BrasilAPIError(f"falha de rede ao chamar {path}: {exc}") from exc

    if resp.status_code == 404:
        raise BrasilAPIError(f"nao encontrado: {path}")
    if resp.status_code >= 400:
        raise BrasilAPIError(f"HTTP {resp.status_code} em {path}: {resp.text[:200]}")

    return resp.json()
