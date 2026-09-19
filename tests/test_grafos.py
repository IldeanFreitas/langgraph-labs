"""Testes dos grafos sem gastar cota de modelo.

Duas técnicas:
  - stub do modelo (lab 2): `get_model` é trocado por um objeto que devolve a
    classificação pronta. Testa roteamento, reducer e contexto sem rede de LLM.
  - --sem-llm (labs 4 e 5): o próprio grafo tem o caminho determinístico.

A BrasilAPI é chamada de verdade (poucas requisições, com cache). Se ela
cair, o gate em test_brasilapi.py falha primeiro e aponta o culpado.
"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphDrained, NodeTimeoutError
from langgraph.runtime import RunControl

from labs.brasilapi import aget

# ------------------------------------------------------------ lab 2 (stub)


class ModeloFalso:
    """Imita o pedaço de ChatModel que o lab 2 usa: with_structured_output().invoke()."""

    def __init__(self, resposta):
        self.resposta = resposta
        self.chamadas = 0

    def with_structured_output(self, _schema):
        return self

    def invoke(self, _prompt):
        self.chamadas += 1
        return self.resposta


def test_lab02_roteia_cep_com_modelo_stubado(monkeypatch):
    import roteador

    falso = ModeloFalso(roteador.Classificacao(tipo="cep", valor="68502290"))
    monkeypatch.setattr(roteador, "get_model", lambda: falso)

    saida = roteador.grafo.invoke(
        {"pergunta": "tanto faz, o stub decide", "log": []},
        context=roteador.Context(usuario="Teste"),
        config={"recursion_limit": 10},
    )

    assert falso.chamadas == 1  # uma chamada de "LLM" por pergunta
    assert saida["log"] == ["classificado como cep", "consultou cep", "formatado"]
    assert saida["resultado"].startswith("Teste, aqui esta:")  # contexto chegou ao formatar
    assert "/PA" in saida["resultado"]


def test_lab02_desconhecido_vai_direto_ao_fim(monkeypatch):
    import roteador

    monkeypatch.setattr(
        roteador,
        "get_model",
        lambda: ModeloFalso(roteador.Classificacao(tipo="desconhecido", valor="")),
    )
    saida = roteador.grafo.invoke(
        {"pergunta": "me conta uma piada", "log": []},
        context=roteador.Context(usuario="Teste"),
        config={"recursion_limit": 10},
    )
    # Command(goto=END) no nó desconhecido: não passa por formatar
    assert saida["log"] == ["classificado como desconhecido", "tipo desconhecido - encerrado"]
    assert "aqui esta" not in saida["resultado"]


# ---------------------------------------------------------- lab 4 (sem-llm)


def test_lab04_paralelo_tres_especialistas():
    import mesa

    saida = mesa.grafo_paralelo.invoke(
        {"pergunta": mesa.PERGUNTA_A, "resultados": []},
        context=mesa.Context(sem_llm=True),
        config={"recursion_limit": 10},
    )
    assert [r["tipo"] for r in saida["resultados"]] == ["cep", "ddd", "banco"]
    assert all(r["ms"] >= 0 for r in saida["resultados"])
    assert "ITAÚ" in saida["relatorio"]


def test_lab04_supervisor_handoff_cnpj_para_cep():
    import mesa

    saida = mesa.grafo_supervisor.invoke(
        {"pergunta": mesa.PERGUNTA_B, "resultados": []},
        context=mesa.Context(sem_llm=True),
        config={"recursion_limit": 12},
    )
    tipos = [r["tipo"] for r in saida["resultados"]]
    assert tipos == ["cnpj", "cep"]  # o handoff levou do CNPJ ao CEP sem o supervisor
    assert saida["valor"] == "70040912"  # o CEP veio do registro do CNPJ
    assert "SAUN" in saida["resposta"]


# ------------------------------------------------------ lab 5 (async, timeouts)


async def test_brasilapi_aget_usa_mesmo_contrato_do_get():
    dados = await aget("/cep/v2/68502290")
    assert dados["state"] == "PA"


async def test_lab05_timeout_interno_vira_dado_em_fan_out(monkeypatch):
    import servico

    async def aget_lento(_path):  # sem rede e sem cache: a lentidão é garantida
        await asyncio.sleep(1)
        return {}

    monkeypatch.setattr(servico, "aget", aget_lento)
    saida = await servico.grafo.ainvoke(
        {"pergunta": servico.PERGUNTA, "resultados": []},
        context=servico.Context(sem_llm=True, timeout_consulta_s=0.001),
        config={"recursion_limit": 10},
    )
    assert len(saida["resultados"]) == 3
    assert all("timeout interno" in r["texto"] for r in saida["resultados"])
    assert saida["relatorio"]  # a mesa terminou, apesar das três falhas


async def test_lab05_timeout_policy_dispara_handler_com_uma_consulta():
    import servico

    saida = await servico.grafo.ainvoke(
        {"pergunta": "CEP 68502290", "resultados": []},
        context=servico.Context(sem_llm=True, atraso_s=5.0),
        config={"recursion_limit": 10},
    )
    (r,) = saida["resultados"]
    assert r["ms"] == -1 and "timeout run" in r["texto"]  # veio do error_handler
    assert saida["relatorio"]  # o handler devolveu Command(goto="relatar")


async def test_lab05_bug_8277_handler_nao_segura_falha_em_paralelo():
    """Documenta o bug aberto langchain-ai/langgraph#8277 (1.2.11).

    Quando este teste FALHAR, o bug foi corrigido: atualize o README do lab 5
    e o CLAUDE.md, e considere remover a camada 1 do especialista.
    """
    import servico

    with pytest.raises(NodeTimeoutError):
        await servico.grafo.ainvoke(
            {"pergunta": servico.PERGUNTA, "resultados": []},
            context=servico.Context(sem_llm=True, atraso_s=5.0),
            config={"recursion_limit": 10},
        )


async def test_lab05_drenagem_salva_checkpoint_e_retoma():
    import servico

    grafo = servico.builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "teste-drenagem"}, "recursion_limit": 10}
    contexto = servico.Context(sem_llm=True, atraso_s=0.5)

    control = RunControl()
    asyncio.get_running_loop().call_later(0.2, control.request_drain, "teste")
    with pytest.raises(GraphDrained) as exc:
        await grafo.ainvoke(
            {"pergunta": servico.PERGUNTA, "resultados": []},
            config=config,
            context=contexto,
            control=control,
        )
    assert exc.value.reason == "teste"

    parcial = grafo.get_state(config)
    assert parcial.next == ("relatar",)  # parou no fim do superstep dos especialistas
    assert len(parcial.values["resultados"]) == 3

    final = await grafo.ainvoke(None, config=config, context=contexto)
    assert final["relatorio"].startswith("Resultados:")
