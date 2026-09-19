"""Cliente HTTP compartilhado da BrasilAPI.

A BrasilAPI pede explicitamente que o volume venha de requisicoes reais, nao de
scraping automatizado. Por isso: timeout curto, um unico client reaproveitado e
cache em memoria. Nos labs ainda somamos CachePolicy no no do grafo.

Duas excecoes, de proposito:
  - BrasilAPIError (RuntimeError): 404, 400... O RetryPolicy padrao do LangGraph
    NAO repete RuntimeError - e 404 nao melhora repetindo.
  - BrasilAPIIndisponivel (ConnectionError): rede, DNS, timeout. O RetryPolicy
    padrao repete ConnectionError. Tambem e BrasilAPIError, entao um
    `except BrasilAPIError` continua pegando as duas.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import httpx

BASE_URL = "https://brasilapi.com.br/api"
_HEADERS = {"User-Agent": "langgraph-labs/0.1"}

_client = httpx.Client(base_url=BASE_URL, timeout=10.0, headers=_HEADERS)


class BrasilAPIError(RuntimeError):
    """Erro de negocio da BrasilAPI (404, 400...), ja com mensagem legivel. Nao repita."""


class BrasilAPIIndisponivel(BrasilAPIError, ConnectionError):
    """Rede, DNS ou timeout ao chamar a BrasilAPI. Vale repetir."""


def _checar(resp: httpx.Response, path: str) -> dict | list:
    if resp.status_code == 404:
        raise BrasilAPIError(f"nao encontrado: {path}")
    if resp.status_code >= 400:
        raise BrasilAPIError(f"HTTP {resp.status_code} em {path}: {resp.text[:200]}")
    return resp.json()


@lru_cache(maxsize=256)
def get(path: str) -> dict | list:
    """GET em um caminho relativo, ex.: "/cep/v2/68502290"."""
    try:
        resp = _client.get(path)
    except httpx.RequestError as exc:  # rede, DNS, timeout
        raise BrasilAPIIndisponivel(f"falha de rede ao chamar {path}: {exc}") from exc
    return _checar(resp, path)


_acache: dict[str, Any] = {}


async def aget(path: str) -> dict | list:
    """Versao async de get(), para nos async (lab 5). Mesmo cache, mesmas excecoes.

    O AsyncClient e aberto por chamada, nao global: um client async fica preso
    ao event loop em que nasceu, e com mais de um loop (pytest-asyncio abre um
    por teste; servidores tambem podem) aparece "Event loop is closed". Em
    producao, abra um por aplicacao no lifespan do servidor.
    """
    if path in _acache:
        return _acache[path]
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0, headers=_HEADERS) as c:
            resp = await c.get(path)
    except httpx.RequestError as exc:
        raise BrasilAPIIndisponivel(f"falha de rede ao chamar {path}: {exc}") from exc
    dados = _checar(resp, path)
    _acache[path] = dados
    return dados
