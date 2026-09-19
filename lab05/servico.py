"""Lab 5 - Produção: timeouts, drenagem, serviço e testes.

O grafo é a mesa paralela do lab 4, reescrita com nós ASYNC. Só isso já
destrava o que o lab 2 não pôde usar: TimeoutPolicy. Em cima, o que falta
para o grafo virar serviço:

  1. nós async + TimeoutPolicy      -> run_timeout/idle_timeout -> NodeTimeoutError
  2. error_handler por nó           -> falha (ou timeout) vira dado, não exceção
  3. set_node_defaults              -> a mesma política para todos; o nó sobrescreve
  4. RunControl.request_drain()     -> parar no fim do superstep, checkpoint salvo
  5. runtime.drain_requested        -> o nó sabe que o processo está sendo drenado
  6. langgraph.json + langgraph dev -> o grafo vira API REST + Studio (README)
  7. tests/test_grafos.py           -> stub de modelo, sem cota

Rode:
  uv run python lab05/servico.py mesa                 # como o lab 4, mas async
  uv run python lab05/servico.py mesa --atraso 5      # especialista demora 5 s > timeout de 2 s
  uv run python lab05/servico.py drenar               # drena no meio, retoma do checkpoint
  uv run langgraph dev --no-browser                   # serviço em http://127.0.0.1:2024

Sem cota? --sem-llm troca planejar/relatar por regex e template.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from operator import add
from typing import Annotated, Literal

from langchain_core.exceptions import ModelRateLimitError
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphDrained, NodeError, NodeTimeoutError
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, RetryPolicy, Send, TimeoutPolicy
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from labs.brasilapi import BrasilAPIError, aget
from labs.config import get_model

MAX_FAN_OUT = 3
TIMEOUT_ESPECIALISTA_S = 2.0  # a BrasilAPI responde em ~150 ms; 2 s é folga, não meta
TIMEOUT_MODELO_S = 90.0  # o LLM pode levar dezenas de segundos (e um retry de 429)


@dataclass
class Context:
    sem_llm: bool = False
    atraso_s: float = 0.0  # trava o nó ANTES da consulta: quem pega é o TimeoutPolicy
    timeout_consulta_s: float = 1.5  # limite da consulta em si: quem pega é o próprio nó


# ---------------------------------------------------------------- estado


class Consulta(TypedDict):
    tipo: str
    valor: str


class Resultado(TypedDict):
    tipo: str
    valor: str
    texto: str
    ms: int


class MesaState(TypedDict):
    pergunta: str
    plano: list[Consulta]
    resultados: Annotated[list[Resultado], add]
    relatorio: str


Tipo = Literal["cep", "cnpj", "ddd", "banco"]


class ConsultaPlanejada(BaseModel):
    tipo: Tipo
    valor: str = Field(description="apenas dígitos")


class Plano(BaseModel):
    consultas: list[ConsultaPlanejada] = Field(description="uma entrada por consulta pedida")


# ---------------------------------------------------------- especialistas
# Iguais aos do lab 4, mas async: `await aget(...)` libera o event loop, e é
# isso que permite ao TimeoutPolicy cancelar o nó no meio.


async def _cep(valor: str) -> str:
    d = await aget(f"/cep/v2/{valor}")
    return f"CEP {valor}: {d.get('street') or '(sem logradouro)'}, {d['city']}/{d['state']}"


async def _cnpj(valor: str) -> str:
    d = await aget(f"/cnpj/v1/{valor}")
    return f"CNPJ {valor}: {d['razao_social']} - {d.get('municipio')}/{d.get('uf')}"


async def _ddd(valor: str) -> str:
    d = await aget(f"/ddd/v1/{valor}")
    return f"DDD {valor} ({d['state']}): {', '.join(d['cities'][:5])}..."


async def _banco(valor: str) -> str:
    d = await aget(f"/banks/v1/{valor}")
    return f"Banco {valor}: {d['name']} (ISPB {d['ispb']})"


ESPECIALISTAS: dict[str, Callable[[str], Awaitable[str]]] = {
    "cep": _cep,
    "cnpj": _cnpj,
    "ddd": _ddd,
    "banco": _banco,
}


# ------------------------------------------------------------------- nós


def planejar_por_regex(pergunta: str) -> list[Consulta]:
    tipos = {8: "cep", 14: "cnpj", 2: "ddd", 3: "banco"}
    plano = []
    for g in re.findall(r"\d[\d.\-/]*\d", pergunta):
        digitos = re.sub(r"\D", "", g)
        if digitos and (tipo := tipos.get(len(digitos))):
            plano.append(Consulta(tipo=tipo, valor=digitos))
    return plano


async def planejar(state: MesaState, runtime: Runtime[Context]) -> dict:
    if runtime.context.sem_llm:
        return {"plano": planejar_por_regex(state["pergunta"])}
    plano = await (
        get_model()
        .with_structured_output(Plano)
        .ainvoke(f"Liste as consultas que a pergunta pede.\n\nPergunta: {state['pergunta']}")
    )
    return {"plano": [Consulta(tipo=c.tipo, valor=c.valor) for c in plano.consultas]}


def distribuir(state: MesaState) -> list[Send] | str:
    plano = state["plano"][:MAX_FAN_OUT]
    return [Send("especialista", c) for c in plano] if plano else "relatar"


async def especialista(consulta: Consulta, runtime: Runtime[Context]) -> dict:
    """Nó async com DUAS camadas de proteção.

    1. asyncio.timeout() em volta da consulta: o nó trata o próprio timeout e
       devolve a falha como dado. Funciona sempre, inclusive em fan-out.
    2. TimeoutPolicy do grafo (2 s) como rede de segurança para o que o nó não
       previu - aqui simulado por --atraso, que trava o nó fora da camada 1.
       O cancelamento é cooperativo: só funciona porque o nó cede o loop
       (`await`); um time.sleep() bloqueante não seria interrompido.
    """
    inicio = time.perf_counter()
    if runtime.context.atraso_s:
        await asyncio.sleep(runtime.context.atraso_s)  # fora da camada 1, de propósito
    if runtime.drain_requested:
        print(f"    [{consulta['tipo']}] drenagem pedida ({runtime.drain_reason}) - concluindo")
    limite = runtime.context.timeout_consulta_s
    try:
        async with asyncio.timeout(limite):
            texto = await ESPECIALISTAS[consulta["tipo"]](consulta["valor"])
    except TimeoutError:
        texto = f"{consulta['tipo']} {consulta['valor']}: falhou (timeout interno de {limite}s)"
    except BrasilAPIError as exc:  # 404 ou rede: vira dado, como no lab 4
        texto = f"{consulta['tipo']} {consulta['valor']}: falhou ({exc})"
    ms = int((time.perf_counter() - inicio) * 1000)
    return {
        "resultados": [
            Resultado(tipo=consulta["tipo"], valor=consulta["valor"], texto=texto, ms=ms)
        ]
    }


async def especialista_falhou(consulta: Consulta, error: NodeError) -> Command:
    """error_handler: roda depois que o retry esgota. Recebe o mesmo input do
    nó (a Consulta do Send) e o erro.

    Três detalhes que a doc não grita:
    - É async porque o handler também é um nó, e o timeout padrão do
      set_node_defaults vale para ele: handler sync + timeout = compile() recusa.
    - Devolve Command(goto=...) porque as arestas de saída do nó que falhou
      NÃO são seguidas depois do handler; com um dict o grafo termina ali.
    - langgraph 1.2.11, issue #8277: se o nó que falhou rodava em paralelo com
      outras tarefas no mesmo superstep, o handler roda mas a exceção sobe
      mesmo assim. Com uma consulta só, funciona. Por isso a camada 1 existe.
    """
    exc = error.error
    if isinstance(exc, NodeTimeoutError):
        causa = f"timeout {exc.kind} de {exc.timeout}s"
    else:
        causa = f"{type(exc).__name__}: {exc}"
    texto = f"{consulta['tipo']} {consulta['valor']}: falhou ({causa})"
    resultado = Resultado(tipo=consulta["tipo"], valor=consulta["valor"], texto=texto, ms=-1)
    return Command(update={"resultados": [resultado]}, goto="relatar")


async def relatar(state: MesaState, runtime: Runtime[Context]) -> dict:
    if not state["resultados"]:
        return {"relatorio": "Não encontrei CEP, CNPJ, DDD ou banco na pergunta."}
    linhas = "\n".join(f"- {r['texto']}" for r in state["resultados"])
    if runtime.context.sem_llm:
        return {"relatorio": f"Resultados:\n{linhas}"}
    resposta = await get_model().ainvoke(
        "Responda à pergunta em português, em um parágrafo curto, usando SÓ os dados abaixo. "
        "Se algum dado falhou, diga isso.\n\n"
        f"Pergunta: {state['pergunta']}\n\nDados:\n{linhas}"
    )
    return {"relatorio": resposta.text}


# ----------------------------------------------------------------- grafo

builder = StateGraph(MesaState, context_schema=Context)

# Padrão para TODOS os nós. Só funciona porque os nós são async: com um nó
# sync, o compile() recusa o timeout.
builder.set_node_defaults(
    timeout=TimeoutPolicy(run_timeout=TIMEOUT_ESPECIALISTA_S),
    retry_policy=RetryPolicy(max_attempts=2),  # NodeTimeoutError é repetível de propósito
)

# Os nós do modelo sobrescrevem: timeout maior e retry só para 429.
retry_429 = RetryPolicy(max_attempts=3, initial_interval=20.0, retry_on=ModelRateLimitError)
timeout_modelo = TimeoutPolicy(run_timeout=TIMEOUT_MODELO_S)
builder.add_node("planejar", planejar, retry_policy=retry_429, timeout=timeout_modelo)
builder.add_node("relatar", relatar, retry_policy=retry_429, timeout=timeout_modelo)

# O especialista herda o padrão (2 s, 2 tentativas) e ganha o handler.
builder.add_node(
    "especialista", especialista, input_schema=Consulta, error_handler=especialista_falhou
)

builder.add_edge(START, "planejar")
builder.add_conditional_edges("planejar", distribuir, ["especialista", "relatar"])
builder.add_edge("especialista", "relatar")
builder.add_edge("relatar", END)

# Sem checkpointer: é este que o langgraph.json expõe. O servidor injeta o dele.
grafo = builder.compile()


# -------------------------------------------------------------- comandos

PERGUNTA = "Me diga o endereço do CEP 68502290, as cidades do DDD 94 e o nome do banco 341."


def mostrar(saida: dict, total_ms: int) -> None:
    print(f"  plano: {[(c['tipo'], c['valor']) for c in saida['plano']]}")
    for r in saida["resultados"]:
        ms = f"{r['ms']:>5} ms" if r["ms"] >= 0 else " falhou "
        print(f"  [{r['tipo']:<5} {ms}] {r['texto']}")
    if total_ms >= 0:
        print(f"  tempo real da mesa: {total_ms} ms")
    print(f"\n  {saida['relatorio']}")


async def cmd_mesa(args: argparse.Namespace) -> None:
    print(
        f"\n> {args.pergunta}   [atraso={args.atraso}s | timeout interno={args.timeout_consulta}s"
        f" | TimeoutPolicy={TIMEOUT_ESPECIALISTA_S}s]"
    )
    inicio = time.perf_counter()
    saida = await grafo.ainvoke(
        {"pergunta": args.pergunta, "resultados": []},
        context=Context(
            sem_llm=args.sem_llm, atraso_s=args.atraso, timeout_consulta_s=args.timeout_consulta
        ),
        config={"recursion_limit": 10},
    )
    mostrar(saida, int((time.perf_counter() - inicio) * 1000))


async def cmd_drenar(args: argparse.Namespace) -> None:
    """Simula um SIGTERM no meio da execução.

    Em produção: signal.signal(SIGTERM, lambda *_: control.request_drain("sigterm")).
    Aqui um timer faz o mesmo papel, para o resultado ser reproduzível.
    """
    from langgraph.runtime import RunControl

    saver = InMemorySaver()  # em produção, o PostgresSaver do lab 3
    grafo_ck = builder.compile(checkpointer=saver)
    config = {"configurable": {"thread_id": "drenagem"}, "recursion_limit": 10}
    contexto = Context(sem_llm=True, atraso_s=args.atraso)

    control = RunControl()
    asyncio.get_running_loop().call_later(args.apos, control.request_drain, "sigterm simulado")

    print(f"\n> {args.pergunta}   [drenar após {args.apos}s; especialistas levam {args.atraso}s]")
    try:
        await grafo_ck.ainvoke(
            {"pergunta": args.pergunta, "resultados": []},
            config=config,
            context=contexto,
            control=control,
        )
        print("  terminou antes do pedido de drenagem - aumente --atraso ou reduza --apos")
        return
    except GraphDrained as exc:
        snap = grafo_ck.get_state(config)
        print(f"  DRENADO ({exc.reason}) no fim do superstep. Checkpoint salvo.")
        print(f"  resultados já gravados: {len(snap.values.get('resultados', []))}")
        print(f"  próximos nós: {snap.next}")

    print("  ... processo novo: invoke(None, config) continua de onde parou")
    saida = await grafo_ck.ainvoke(None, config=config, context=contexto)
    mostrar(saida, -1)


def main() -> None:
    p = argparse.ArgumentParser(description="Lab 5 - mesa async, timeouts e drenagem")
    sub = p.add_subparsers(dest="comando", required=True)

    s = sub.add_parser("mesa", help="mesa paralela com nós async e TimeoutPolicy")
    s.add_argument("pergunta", nargs="?", default=PERGUNTA)
    s.add_argument("--sem-llm", action="store_true")
    s.add_argument("--atraso", type=float, default=0.0, help="trava o nó N s antes da consulta")
    s.add_argument("--timeout-consulta", type=float, default=1.5, help="limite interno da consulta")
    s.set_defaults(fn=cmd_mesa)

    s = sub.add_parser("drenar", help="RunControl: parar no meio e retomar do checkpoint")
    s.add_argument("pergunta", nargs="?", default=PERGUNTA)
    s.add_argument("--apos", type=float, default=0.5, help="segundos até o pedido de drenagem")
    s.add_argument(
        "--atraso", type=float, default=1.0, help="segundos de espera em cada especialista"
    )
    s.set_defaults(fn=cmd_drenar)

    args = p.parse_args()
    asyncio.run(args.fn(args))


if __name__ == "__main__":
    main()
