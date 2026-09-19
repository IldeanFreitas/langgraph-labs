"""Gate 0: confirma que a BrasilAPI responde antes de qualquer lab rodar."""

import pytest

from labs.brasilapi import BrasilAPIError, get


def test_cep_valido():
    dados = get("/cep/v2/68502290")
    assert dados["state"] == "PA"
    assert "city" in dados


def test_cep_inexistente_vira_erro_tratado():
    with pytest.raises(BrasilAPIError):
        get("/cep/v2/00000000")
