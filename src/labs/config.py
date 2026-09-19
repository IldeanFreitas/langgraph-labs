"""Configuracao comum aos labs. Le o .env uma vez e expoe o modelo."""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

load_dotenv()

MODEL_ID = os.getenv("LAB_MODEL", "google_genai:gemini-3.8-flash")
POSTGRES_URI = os.getenv("POSTGRES_URI", "")


@lru_cache(maxsize=4)
def get_model(temperature: float = 0.0):
    """Modelo unico dos labs.

    Trocar de provedor = mudar LAB_MODEL no .env. Nenhum lab importa o SDK
    do provedor diretamente, entao a troca nao toca no codigo dos grafos.
    """
    if not MODEL_ID:
        raise RuntimeError("LAB_MODEL nao definido no .env")
    return init_chat_model(MODEL_ID, temperature=temperature)
