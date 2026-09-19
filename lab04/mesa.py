"""Lab 4 - Mesa de consultas: paralelismo e multi-agente.

Uma pergunta pode pedir várias consultas. Duas formas de organizar a mesa:

  A. PARALELO (orquestrador-trabalhador)
     planejar (LLM) -> Send x N -> especialistas em paralelo -> relatar (LLM)
     Para consultas INDEPENDENTES. 2 chamadas de LLM, não importa quantas consultas.

  B. SUPERVISOR (subgrafos + handoff)
     supervisor (LLM) -> subgrafo cnpj --handoff--> subgrafo cep -> supervisor (LLM) -> fim
     Para consultas DEPENDENTES (o CEP só existe depois do CNPJ). 2 chamadas de LLM.

Conceitos, em ordem de aparição:
  1. Send                     -> fan-out dinâmico, cada alvo com seu próprio input
  2. reducer em escrita paralela -> N nós escrevendo a mesma chave no mesmo passo
  3. subgrafo como nó         -> StateGraph compilado, com chaves compartilhadas
  4. Command(graph=Command.PARENT) -> sair do subgrafo e rotear no pai (handoff)
  5. supervisor com saída estruturada -> decide o próximo membro ou encerra
  6. RemainingSteps           -> encerrar com elegância antes do recursion_limit
  7. stream(subgraphs=True)   -> ver o que acontece dentro do subgrafo

Rode:
  uv run python lab04/mesa.py paralelo
  uv run python lab04/mesa.py supervisor
  uv run python lab04/mesa.py desenhar paralelo | supervisor

Sem cota? --sem-llm troca planejar/relatar/supervisor por regex e template.
"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from operator import add
from typing import Annotated, Literal

from langchain_core.exceptions import ModelRateLimitError
from langgraph.graph import END, START, StateGraph
from langgraph.managed import RemainingSteps
from langgraph.runtime import Runtime
from langgraph.types import Command, RetryPolicy, Send
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from labs.brasilapi import BrasilAPIError, get
from labs.config import get_model

MAX_FAN_OUT = 3  # limite de especialistas por pergunta (cota e educação com a BrasilAPI)
RETRY_429 = RetryPolicy(max_attempts=3, initial_interval=20.0, retry_on=ModelRateLimitError)


@dataclass
class Context:
    sem_llm: bool = False


# ------------------------------------------------------------ especialistas
# Funções Python puras: recebem (tipo, valor), devolvem texto. Erro vira texto,
# não exceção - um especialista que explode derrubaria a mesa inteira.


def _cep(valor: str) -> str:
    d = get(f"/cep/v2/{valor}")
    return f"CEP {valor}: {d.get('street') or '(sem logradouro)'}, {d['city']}/{d['state']}"


def _cnpj(valor: str) -> str:
    d = get(f"/cnpj/v1/{valor}")
    return (
        f"CNPJ {valor}: {d['razao_social']} - {d.get('municipio')}/{d.get('uf')} "
        f"(CEP {d.get('cep')})"
    )


def _ddd(valor: str) -> str:
    d = get(f"/ddd/v1/{valor}")
    return f"DDD {valor} ({d['state']}): {', '.join(d['cities'][:5])}..."


def _banco(valor: str) -> str:
    d = get(f"/banks/v1/{valor}")
    return f"Banco {valor}: {d['name']} (ISPB {d['ispb']})"


ESPECIALISTAS = {"cep": _cep, "cnpj": _cnpj, "ddd": _ddd, "banco": _banco}
Tipo = Literal["cep", "cnpj", "ddd", "banco"]


class Resultado(TypedDict):
    tipo: str
    valor: str
    texto: str
    ms: int


def consultar(tipo: str, valor: str) -> Resultado:
    inicio = time.perf_counter()
    try:
        texto = ESPECIALISTAS[tipo](valor)
    except BrasilAPIError as exc:
        texto = f"{tipo} {valor}: falhou ({exc})"
    return Resultado(
        tipo=tipo, valor=valor, texto=texto, ms=int((time.perf_counter() - inicio) * 1000)
    )


# ======================================================================
# Parte A - PARALELO: planejar -> Send x N -> especialistas -> relatar
# ======================================================================


class Consulta(TypedDict):
    """O que cada especialista recebe. É o payload do Send, não o estado da mesa."""

    tipo: str
    valor: str


class MesaState(TypedDict):
    pergunta: str
    plano: list[Consulta]
    # N especialistas escrevem aqui NO MESMO PASSO. Sem reducer:
    # InvalidUpdateError (INVALID_CONCURRENT_GRAPH_UPDATE).
    resultados: Annotated[list[Resultado], add]
    relatorio: str


class ConsultaPlanejada(BaseModel):
    tipo: Tipo
    valor: str = Field(description="apenas dígitos")


class Plano(BaseModel):
    consultas: list[ConsultaPlanejada] = Field(description="uma entrada por consulta pedida")


def planejar_por_regex(pergunta: str) -> list[Consulta]:
    """Sem LLM: 8 dígitos = CEP, 14 = CNPJ, 2 = DDD, 3 = banco."""
    tipos = {8: "cep", 14: "cnpj", 2: "ddd", 3: "banco"}
    grupos = re.findall(r"\d[\d.\-/]*\d", pergunta)
    plano = []
    for g in grupos:
        digitos = re.sub(r"\D", "", g)
        if digitos and (tipo := tipos.get(len(digitos))):
            plano.append(Consulta(tipo=tipo, valor=digitos))
    return plano


def planejar(state: MesaState, runtime: Runtime[Context]) -> dict:
    """UMA chamada de LLM decide todas as consultas de uma vez."""
    if runtime.context.sem_llm:
        return {"plano": planejar_por_regex(state["pergunta"])}
    plano = (
        get_model()
        .with_structured_output(Plano)
        .invoke(f"Liste as consultas que a pergunta pede.\n\nPergunta: {state['pergunta']}")
    )
    return {"plano": [Consulta(tipo=c.tipo, valor=c.valor) for c in plano.consultas]}


def distribuir(state: MesaState) -> list[Send] | str:
    """Aresta condicional que devolve Sends: um por consulta, cada um com SEU input.

    Todos rodam no mesmo passo, em paralelo. O nome no Send é o nó de destino;
    o dicionário é o estado que aquele nó vai receber.
    """
    plano = state["plano"][:MAX_FAN_OUT]
    if not plano:
        return "relatar"
    return [Send("especialista", c) for c in plano]


def especialista(consulta: Consulta) -> dict:
    """Recebe a Consulta do Send (input_schema), devolve delta para o estado da MESA."""
    return {"resultados": [consultar(consulta["tipo"], consulta["valor"])]}


def relatar(state: MesaState, runtime: Runtime[Context]) -> dict:
    if not state["resultados"]:
        return {"relatorio": "Não encontrei CEP, CNPJ, DDD ou banco na pergunta."}
    linhas = "\n".join(f"- {r['texto']}" for r in state["resultados"])
    if runtime.context.sem_llm:
        return {"relatorio": f"Resultados:\n{linhas}"}
    resposta = get_model().invoke(
        "Responda à pergunta em português, em um parágrafo curto, usando SÓ os dados abaixo.\n\n"
        f"Pergunta: {state['pergunta']}\n\nDados:\n{linhas}"
    )
    return {"relatorio": resposta.text}


mesa = StateGraph(MesaState, context_schema=Context)
mesa.add_node("planejar", planejar, retry_policy=RETRY_429)
mesa.add_node("especialista", especialista, input_schema=Consulta)
mesa.add_node("relatar", relatar, retry_policy=RETRY_429)
mesa.add_edge(START, "planejar")
mesa.add_conditional_edges("planejar", distribuir, ["especialista", "relatar"])
mesa.add_edge("especialista", "relatar")  # relatar só roda quando TODOS os Sends terminam
mesa.add_edge("relatar", END)
grafo_paralelo = mesa.compile()


# ======================================================================
# Parte B - SUPERVISOR: subgrafos + handoff via Command.PARENT
# ======================================================================


def ultimo(_atual: str, novo: str) -> str:
    """Reducer 'o último vence'. Chave compartilhada que um subgrafo atualiza
    via Command.PARENT precisa de reducer no pai (regra da doc)."""
    return novo


class SupervisorState(TypedDict):
    pergunta: str
    valor: Annotated[str, ultimo]  # argumento para o próximo especialista
    resultados: Annotated[list[Resultado], add]
    resposta: str
    remaining_steps: RemainingSteps  # preenchido pelo LangGraph, nunca por você


class EspecialistaState(TypedDict):
    """Estado do subgrafo. Compartilha só 'valor' com o pai (é a entrada).

    'resultado' é privado. Quem leva o resultado ao pai é o Command.PARENT do
    nó entregar: como o subgrafo sai por ele e nunca chega ao próprio END, o
    LangGraph NÃO copia o estado do subgrafo de volta - só o update do Command.
    """

    valor: Annotated[str, ultimo]
    resultado: Resultado


def sub_consultar_cnpj(state: EspecialistaState) -> dict:
    return {"resultado": consultar("cnpj", state["valor"])}


def sub_entregar_cnpj(state: EspecialistaState) -> Command:
    """Handoff: o CNPJ trouxe um CEP, então passa DIRETO ao especialista de CEP
    do grafo pai, sem gastar uma rodada do supervisor.

    Sem Literal na anotação: os destinos são nós do PAI, e o compile() do
    subgrafo validaria contra os nós dele. Quem declara isso para o desenho é o
    destinations= no add_node do pai.
    """
    entrega = {"resultados": [state["resultado"]]}
    cep = re.search(r"CEP (\d{8})", state["resultado"]["texto"])
    if cep:
        return Command(update={**entrega, "valor": cep.group(1)}, goto="cep", graph=Command.PARENT)
    return Command(update=entrega, goto="supervisor", graph=Command.PARENT)


def sub_consultar_cep(state: EspecialistaState) -> dict:
    return {"resultado": consultar("cep", state["valor"])}


def sub_entregar_cep(state: EspecialistaState) -> Command:
    return Command(
        update={"resultados": [state["resultado"]]}, goto="supervisor", graph=Command.PARENT
    )


def montar_subgrafo(nome: str, consultar_fn, entregar_fn):
    sg = StateGraph(EspecialistaState)
    sg.add_node("consultar", consultar_fn)
    sg.add_node("entregar", entregar_fn)
    sg.add_edge(START, "consultar")
    sg.add_edge("consultar", "entregar")
    return sg.compile(name=nome)


subgrafo_cnpj = montar_subgrafo("especialista_cnpj", sub_consultar_cnpj, sub_entregar_cnpj)
subgrafo_cep = montar_subgrafo("especialista_cep", sub_consultar_cep, sub_entregar_cep)


class Decisao(BaseModel):
    proximo: Literal["cnpj", "cep", "fim"] = Field(
        description="qual especialista chamar agora, ou 'fim' se os dados já bastam"
    )
    valor: str = Field(default="", description="argumento para o especialista, só dígitos")
    resposta: str = Field(
        default="", description="resposta final ao usuário, só quando proximo=fim"
    )


def supervisor(
    state: SupervisorState, runtime: Runtime[Context]
) -> Command[Literal["cnpj", "cep", "__end__"]]:
    """Decide o próximo membro. Cada rodada aqui custa 1 chamada de LLM."""
    feitos = "\n".join(f"- {r['texto']}" for r in state["resultados"]) or "(nada ainda)"

    # RemainingSteps: se o orçamento de passos está acabando, encerra com o que
    # tem em vez de estourar GraphRecursionError no meio de uma consulta.
    if state["remaining_steps"] < 4:
        return Command(
            update={"resposta": f"Parei por limite de passos. Tinha:\n{feitos}"}, goto=END
        )

    if runtime.context.sem_llm:
        if not state["resultados"]:
            d = Decisao(proximo="cnpj", valor=re.sub(r"\D", "", state["pergunta"])[-14:])
        else:
            d = Decisao(proximo="fim", resposta=f"Resultados:\n{feitos}")
    else:
        d = (
            get_model()
            .with_structured_output(Decisao)
            .invoke(
                "Você coordena especialistas de CNPJ e CEP. Decida o próximo passo.\n"
                "Se os dados já respondem a pergunta, proximo=fim e escreva a resposta.\n\n"
                f"Pergunta: {state['pergunta']}\n\nJá consultado:\n{feitos}"
            )
        )

    if d.proximo == "fim":
        return Command(update={"resposta": d.resposta}, goto=END)
    return Command(update={"valor": d.valor}, goto=d.proximo)


sup = StateGraph(SupervisorState, context_schema=Context)
sup.add_node("supervisor", supervisor, retry_policy=RETRY_429)
# destinations só serve para o desenho: quem roteia de verdade é o Command.PARENT.
sup.add_node("cnpj", subgrafo_cnpj, destinations=("cep", "supervisor"))
sup.add_node("cep", subgrafo_cep, destinations=("supervisor",))
sup.add_edge(START, "supervisor")
grafo_supervisor = sup.compile()


# ------------------------------------------------------------------ comandos

PERGUNTA_A = "Me diga o endereço do CEP 68502290, as cidades do DDD 94 e o nome do banco 341."
PERGUNTA_B = "Qual o endereço completo da empresa de CNPJ 00000000000191?"


def cmd_paralelo(args: argparse.Namespace) -> None:
    print(f"\n> {args.pergunta}")
    inicio = time.perf_counter()
    saida = grafo_paralelo.invoke(
        {"pergunta": args.pergunta, "resultados": []},
        context=Context(sem_llm=args.sem_llm),
        config={"recursion_limit": 10},
    )
    total = int((time.perf_counter() - inicio) * 1000)
    print(f"  plano: {[(c['tipo'], c['valor']) for c in saida['plano']]}")
    for r in saida["resultados"]:
        print(f"  [{r['tipo']:<5} {r['ms']:>5} ms] {r['texto']}")
    soma = sum(r["ms"] for r in saida["resultados"])
    print(f"  especialistas somados: {soma} ms | tempo real da mesa: {total} ms (inclui LLM)")
    print(f"\n  {saida['relatorio']}")


def cmd_supervisor(args: argparse.Namespace) -> None:
    print(f"\n> {args.pergunta}   [recursion_limit={args.recursion_limit}]")
    resposta = ""
    # subgraphs=True: os passos DENTRO dos subgrafos aparecem, com o namespace
    # ("cnpj:<id>",) dizendo em qual nó do pai o subgrafo está rodando.
    for chunk in grafo_supervisor.stream(
        {"pergunta": args.pergunta, "resultados": []},
        context=Context(sem_llm=args.sem_llm),
        config={"recursion_limit": args.recursion_limit},
        stream_mode="updates",
        subgraphs=True,
        version="v2",
    ):
        onde = chunk["ns"][0].split(":")[0] if chunk["ns"] else "pai"
        for no, delta in chunk["data"].items():
            resumo = ""
            if isinstance(delta, dict):
                if "resultado" in delta:  # dentro do subgrafo (chave privada)
                    resumo = f"consultou: {delta['resultado']['texto'][:50]}..."
                elif "resultados" in delta:  # o que o Command.PARENT entregou ao pai
                    resumo = "entregou ao pai" + (
                        f" + handoff valor={delta['valor']}" if "valor" in delta else ""
                    )
                elif "valor" in delta:
                    resumo = f"valor={delta['valor']}"
                elif "resposta" in delta:
                    resposta = delta["resposta"]
                    resumo = "resposta pronta"
            print(f"  [{onde:<4}] {no:<10} {resumo}")
    print(f"\n  {resposta}")


def cmd_desenhar(args: argparse.Namespace) -> None:
    g = grafo_paralelo if args.qual == "paralelo" else grafo_supervisor
    print(g.get_graph(xray=True).draw_mermaid())


def main() -> None:
    p = argparse.ArgumentParser(description="Lab 4 - mesa de consultas")
    sub = p.add_subparsers(dest="comando", required=True)

    s = sub.add_parser("paralelo", help="Send: consultas independentes em paralelo")
    s.add_argument("pergunta", nargs="?", default=PERGUNTA_A)
    s.add_argument("--sem-llm", action="store_true")
    s.set_defaults(fn=cmd_paralelo)

    s = sub.add_parser("supervisor", help="subgrafos + handoff: consultas dependentes")
    s.add_argument("pergunta", nargs="?", default=PERGUNTA_B)
    s.add_argument("--sem-llm", action="store_true")
    s.add_argument("--recursion-limit", type=int, default=12)
    s.set_defaults(fn=cmd_supervisor)

    s = sub.add_parser("desenhar", help="Mermaid de um dos grafos")
    s.add_argument("qual", choices=["paralelo", "supervisor"])
    s.set_defaults(fn=cmd_desenhar)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
