"""Lab 2 - Graph API: estado, reducers, roteamento e custo.

O lab 1 gastou ~3 chamadas de modelo por pergunta. Aqui o mesmo comportamento
sai com UMA chamada: o LLM so classifica; o resto e Python deterministico.

Conceitos, em ordem de aparicao:
  1. State com TypedDict + Annotated (reducer)
  2. contexto de runtime (context_schema + Runtime)
  3. no com saida estruturada  -> 1 unica chamada de LLM
  4. add_conditional_edges     -> roteamento sem LLM
  5. Command                   -> atualizar estado e rotear no mesmo no
  6. RetryPolicy / CachePolicy (TimeoutPolicy so em no async - ver lab 5)
  7. recursion_limit

Rode:  uv run python lab02/roteador.py
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from operator import add
from typing import Annotated, Literal

from langgraph.cache.memory import InMemoryCache
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import CachePolicy, Command, RetryPolicy
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from labs.brasilapi import get
from labs.config import get_model

# Contador para provar o ponto do lab: quantas vezes o LLM foi chamado.
CHAMADAS_LLM = 0


# ------------------------------------------------------------------ 1. estado


class State(TypedDict):
    pergunta: str
    tipo: str
    valor: str
    resultado: str
    # Annotated com reducer: cada no ANEXA ao log em vez de sobrescrever.
    # Sem o reducer, o ultimo no a escrever apagaria os anteriores.
    log: Annotated[list[str], add]


# ---------------------------------------------------------------- 2. contexto


@dataclass
class Context:
    """Fixo na invocacao - nao muda durante a execucao, nao e persistido."""

    usuario: str


# ---------------------------------------------- 3. classificacao: 1 chamada LLM


class Classificacao(BaseModel):
    tipo: Literal["cep", "cnpj", "ddd", "banco", "desconhecido"] = Field(
        description="que tipo de consulta a pergunta pede"
    )
    valor: str = Field(description="apenas os digitos extraidos da pergunta, sem pontuacao")


def classificar(state: State) -> dict:
    global CHAMADAS_LLM
    CHAMADAS_LLM += 1

    c = (
        get_model()
        .with_structured_output(Classificacao)
        .invoke(f"Classifique a pergunta do usuario.\n\nPergunta: {state['pergunta']}")
    )
    return {"tipo": c.tipo, "valor": c.valor, "log": [f"classificado como {c.tipo}"]}


# ----------------------------------------- 4. roteamento: Python, sem LLM algum


def rotear(state: State) -> str:
    """Funcao Python pura. Custo zero, resultado sempre o mesmo."""
    return state["tipo"]


# ------------------------------------------------------- 5. nos especializados


def consultar_cep(state: State) -> dict:
    d = get(f"/cep/v2/{state['valor']}")
    texto = f"{d.get('street') or '(sem logradouro)'}, {d['city']}/{d['state']}"
    return {"resultado": texto, "log": ["consultou cep"]}


def consultar_ddd(state: State) -> dict:
    d = get(f"/ddd/v1/{state['valor']}")
    cidades = ", ".join(d["cities"][:5])
    texto = f"DDD {state['valor']} ({d['state']}): {cidades}..."
    return {"resultado": texto, "log": ["consultou ddd"]}


def consultar_banco(state: State) -> dict:
    d = get(f"/banks/v1/{state['valor']}")
    texto = f"{d['name']} - {d['fullName']} (ISPB {d['ispb']})"
    return {"resultado": texto, "log": ["consultou banco"]}


def consultar_cnpj(state: State) -> dict:
    d = get(f"/cnpj/v1/{state['valor']}")
    texto = f"{d['razao_social']} - {d.get('cnae_fiscal_descricao')} - {d.get('uf')}"
    return {"resultado": texto, "log": ["consultou cnpj"]}


def desconhecido(state: State) -> Command[Literal["__end__"]]:
    """Command: atualiza o estado E decide para onde ir, no mesmo no.

    Aqui corta o caminho direto para o fim, sem passar por 'formatar'.
    """
    return Command(
        update={
            "resultado": (
                "Nao entendi o tipo de consulta. Informe um CEP, CNPJ, DDD ou codigo de banco."
            ),
            "log": ["tipo desconhecido - encerrado"],
        },
        goto=END,
    )


# ---------------------------------------- 6. formatacao: tambem sem chamar LLM


def formatar(state: State, runtime: Runtime[Context]) -> dict:
    texto = f"{runtime.context.usuario}, aqui esta: {state['resultado']}"
    return {"resultado": texto, "log": ["formatado"]}


# ------------------------------------------------------------------- o grafo

builder = StateGraph(State, context_schema=Context)

# Politicas padrao para todos os nos - evita repetir em cada add_node.
#
# TimeoutPolicy NAO entra aqui: na 1.2.x, timeout de no so vale para nos ASYNC,
# porque execucao sincrona em Python nao pode ser cancelada com seguranca no
# processo. Nos deste lab sao sync. O lab 5 retoma timeout com nos async.
builder.set_node_defaults(
    retry_policy=RetryPolicy(max_attempts=3),
)

builder.add_node("classificar", classificar)

# CachePolicy: entrada igual nao refaz o trabalho. Economiza chamada a
# BrasilAPI e respeita o pedido dela de nao gerar volume automatizado.
for nome, fn in [
    ("cep", consultar_cep),
    ("cnpj", consultar_cnpj),
    ("ddd", consultar_ddd),
    ("banco", consultar_banco),
]:
    builder.add_node(nome, fn, cache_policy=CachePolicy(ttl=300))

builder.add_node("desconhecido", desconhecido)
builder.add_node("formatar", formatar)

builder.add_edge(START, "classificar")

# O mapa diz ao LangGraph quais destinos existem - e o que permite desenhar
# o grafo corretamente e validar os nomes na compilacao.
builder.add_conditional_edges(
    "classificar",
    rotear,
    {
        "cep": "cep",
        "cnpj": "cnpj",
        "ddd": "ddd",
        "banco": "banco",
        "desconhecido": "desconhecido",
    },
)

for nome in ["cep", "cnpj", "ddd", "banco"]:
    builder.add_edge(nome, "formatar")
builder.add_edge("formatar", END)

grafo = builder.compile(cache=InMemoryCache())


# ---------------------------------------------------------------------- demo


def perguntar(texto: str) -> None:
    inicio = time.perf_counter()
    saida = grafo.invoke(
        {"pergunta": texto, "log": []},
        context=Context(usuario="Freitas"),
        config={"recursion_limit": 10},
    )
    ms = (time.perf_counter() - inicio) * 1000
    print(f"\n> {texto}")
    print(f"  {saida['resultado']}")
    print(f"  caminho: {' -> '.join(saida['log'])}  ({ms:.0f} ms)")


if __name__ == "__main__":
    print("=" * 70)
    print("Diagrama do grafo (cole em mermaid.live se quiser ver desenhado):")
    print("=" * 70)
    print(grafo.get_graph().draw_mermaid())

    print("=" * 70)
    perguntar("Qual o endereco do CEP 68502290?")
    perguntar("Que cidades tem DDD 94?")
    perguntar("Qual banco tem o codigo 341?")
    perguntar("Me conta uma piada")

    print("\n" + "=" * 70)
    print("Cache: a MESMA pergunta de novo")
    perguntar("Qual o endereco do CEP 68502290?")

    print("\n" + "=" * 70)
    print(f"Chamadas de LLM no total: {CHAMADAS_LLM}  (uma por pergunta)")
    print("No lab 1, UMA pergunta ja custava ~3.")
