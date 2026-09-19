"""Lab 3 - Persistência, aprovação humana e viagem no tempo.

No lab 2 o estado morria com o processo. Aqui ele vai para o Postgres, e isso
muda o que o grafo consegue fazer:

  - parar no meio, esperar um humano e continuar em OUTRO processo
  - lembrar do usuário entre conversas diferentes (Store)
  - voltar a um checkpoint antigo e seguir por outro caminho (time travel)

Conceitos, em ordem de aparição:
  1. PostgresSaver + thread_id        -> um checkpoint por passo
  2. durability                       -> quando o checkpoint é gravado
  3. PostgresStore + runtime.store    -> memória que atravessa threads
  4. interrupt() + Command(resume=)   -> aprovação humana
  5. get_state / get_state_history    -> inspecionar a thread
  6. update_state + checkpoint_id     -> fork a partir do passado

Cada subcomando é um PROCESSO separado. É isso que prova a persistência: o
segundo comando só funciona porque o primeiro deixou o estado no banco.

Rode (na ordem do README):
  uv run python lab03/persistente.py perguntar t1 "Qual o endereço do CEP 68502290?"
  uv run python lab03/persistente.py estado t1
  uv run python lab03/persistente.py perguntar t2 "Quem é o CNPJ 00000000000191?"
  uv run python lab03/persistente.py retomar t2 sim
  uv run python lab03/persistente.py historico t1
  uv run python lab03/persistente.py voltar t1 <checkpoint_id> --valor 01310100
  uv run python lab03/persistente.py memoria

Caiu no meio (rede, 429, Ctrl+C)?  voltar <thread>  continua do último checkpoint.
Sem cota de LLM? Acrescente --sem-llm ao 'perguntar': um regex classifica.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from operator import add
from typing import Annotated, Literal

from langchain_core.exceptions import ModelRateLimitError
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.store.postgres import PostgresStore
from langgraph.types import Command, RetryPolicy, interrupt
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from labs.brasilapi import get
from labs.config import POSTGRES_URI, get_model

# ------------------------------------------------------------------ 1. estado


class State(TypedDict):
    """Tudo aqui é serializado e gravado no Postgres a cada passo.

    Por isso segredo nunca entra no estado - e por isso o log com reducer
    fica tão útil: ele é a trilha de auditoria da thread.
    """

    pergunta: str
    tipo: str
    valor: str
    resultado: str
    log: Annotated[list[str], add]


@dataclass
class Context:
    """Fixo na invocação e NÃO persistido. Ao retomar uma thread, passe de novo."""

    usuario: str
    sem_llm: bool = False


# ---------------------------------------------------- 2. classificação (LLM)


class Classificacao(BaseModel):
    tipo: Literal["cep", "cnpj", "desconhecido"] = Field(
        description="que tipo de consulta a pergunta pede"
    )
    valor: str = Field(description="apenas os dígitos extraídos da pergunta, sem pontuação")


def classificar_por_regex(pergunta: str) -> Classificacao:
    """Atalho sem LLM: 8 dígitos é CEP, 14 é CNPJ. Para quando a cota acabou."""
    digitos = re.sub(r"\D", "", pergunta)
    tipo = {8: "cep", 14: "cnpj"}.get(len(digitos), "desconhecido")
    return Classificacao(tipo=tipo, valor=digitos)


def classificar(state: State, runtime: Runtime[Context]) -> dict:
    if runtime.context.sem_llm:
        c = classificar_por_regex(state["pergunta"])
    else:
        c = (
            get_model()
            .with_structured_output(Classificacao)
            .invoke(f"Classifique a pergunta do usuário.\n\nPergunta: {state['pergunta']}")
        )
    return {"tipo": c.tipo, "valor": c.valor, "log": [f"classificado como {c.tipo}"]}


def rotear(state: State) -> str:
    return state["tipo"]


# ------------------------------------------------- 3. aprovação humana (HITL)


def aprovar(state: State) -> Command[Literal["cnpj", "__end__"]]:
    """Pausa o grafo e espera um humano. Só funciona com checkpointer.

    interrupt() grava o checkpoint e faz o invoke() retornar com a chave
    "__interrupt__". Quando alguém invocar Command(resume=X), este nó roda
    DE NOVO desde a primeira linha, e o interrupt() devolve X em vez de
    pausar. Por isso nada com efeito colateral pode vir antes dele.
    """
    aprovado = interrupt(
        {
            "pergunta": "Consultar este CNPJ na Receita? Responda sim ou não.",
            "cnpj": state["valor"],
        }
    )
    if aprovado:
        return Command(update={"log": ["aprovado por humano"]}, goto="cnpj")
    return Command(
        update={
            "resultado": "Consulta de CNPJ recusada pelo aprovador.",
            "log": ["recusado por humano"],
        },
        goto=END,
    )


# ------------------------------------------------------- 4. nós especializados


def consultar_cep(state: State) -> dict:
    d = get(f"/cep/v2/{state['valor']}")
    texto = f"{d.get('street') or '(sem logradouro)'}, {d['city']}/{d['state']}"
    return {"resultado": texto, "log": ["consultou cep"]}


def consultar_cnpj(state: State) -> dict:
    d = get(f"/cnpj/v1/{state['valor']}")
    texto = (
        f"{d['razao_social']} - {d.get('cnae_fiscal_descricao')} - "
        f"{d.get('municipio')}/{d.get('uf')}"
    )
    return {"resultado": texto, "log": ["consultou cnpj"]}


def desconhecido(state: State) -> Command[Literal["__end__"]]:
    return Command(
        update={
            "resultado": "Não entendi o tipo de consulta. Informe um CEP ou um CNPJ.",
            "log": ["tipo desconhecido - encerrado"],
        },
        goto=END,
    )


# --------------------------------------------- 5. formatação + memória (Store)


def formatar(state: State, runtime: Runtime[Context]) -> dict:
    """Formata a resposta e registra a consulta na memória do USUÁRIO.

    Checkpointer guarda a thread. Store guarda o que atravessa threads:
    namespace é uma tupla (usuário, coleção), chave é string, valor é dict.
    """
    ns = (runtime.context.usuario, "consultas")
    chave = f"{state['tipo']}:{state['valor']}"

    anterior = runtime.store.get(ns, chave)  # Item ou None
    vezes = anterior.value["vezes"] + 1 if anterior else 1
    runtime.store.put(ns, chave, {"vezes": vezes, "resultado": state["resultado"]})

    aviso = f" (já consultado {vezes - 1}x antes)" if anterior else ""
    return {
        "resultado": f"{runtime.context.usuario}, aqui está: {state['resultado']}{aviso}",
        "log": ["formatado"],
    }


# ------------------------------------------------------------------- o grafo

builder = StateGraph(State, context_schema=Context)

# Retry que respeita o 429 do free tier: o provedor pede ~17 s de espera, então
# o intervalo inicial precisa ser dessa ordem. ModelRateLimitError vem do
# langchain_core, não do SDK do provedor - o lab continua agnóstico.
builder.add_node(
    "classificar",
    classificar,
    retry_policy=RetryPolicy(max_attempts=3, initial_interval=20.0, retry_on=ModelRateLimitError),
)
builder.add_node("aprovar", aprovar)
builder.add_node("cep", consultar_cep)
builder.add_node("cnpj", consultar_cnpj)
builder.add_node("desconhecido", desconhecido)
builder.add_node("formatar", formatar)

builder.add_edge(START, "classificar")
builder.add_conditional_edges(
    "classificar",
    rotear,
    {"cep": "cep", "cnpj": "aprovar", "desconhecido": "desconhecido"},
)
builder.add_edge("cep", "formatar")
builder.add_edge("cnpj", "formatar")
builder.add_edge("formatar", END)


@contextmanager
def abrir() -> Iterator[tuple]:
    """Abre checkpointer e store no Postgres e compila o grafo com os dois.

    setup() cria as tabelas se não existirem. É idempotente, mas em produção
    roda uma vez, no deploy - não a cada requisição como aqui.
    """
    if not POSTGRES_URI:
        raise SystemExit("POSTGRES_URI vazio no .env. Rode: docker compose up -d")
    with (
        PostgresSaver.from_conn_string(POSTGRES_URI) as checkpointer,
        PostgresStore.from_conn_string(POSTGRES_URI) as store,
    ):
        checkpointer.setup()
        store.setup()
        yield builder.compile(checkpointer=checkpointer, store=store), store


# ------------------------------------------------------------------ comandos


def config_de(thread: str, checkpoint_id: str | None = None) -> dict:
    configurable = {"thread_id": thread}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable, "recursion_limit": 10}


def mostrar(saida: dict, thread: str) -> None:
    if "__interrupt__" in saida:
        for it in saida["__interrupt__"]:
            print(f"  PAUSADO: {it.value}")
        print(f"  O processo vai terminar. Retome com:  retomar {thread} sim|nao")
        return
    print(f"  {saida['resultado']}")
    print(f"  caminho: {' -> '.join(saida['log'])}")


def cmd_perguntar(args: argparse.Namespace) -> None:
    with abrir() as (grafo, _):
        print(f"\n> {args.pergunta}   [thread={args.thread} durability={args.durability}]")
        saida = grafo.invoke(
            {"pergunta": args.pergunta, "log": []},
            config=config_de(args.thread),
            context=Context(usuario=args.usuario, sem_llm=args.sem_llm),
            durability=args.durability,
        )
        mostrar(saida, args.thread)


def cmd_retomar(args: argparse.Namespace) -> None:
    decisao = args.decisao.lower() in ("sim", "s", "yes", "y")
    with abrir() as (grafo, _):
        print(f"\n> retomando thread={args.thread} com resume={decisao}")
        saida = grafo.invoke(
            Command(resume=decisao),
            config=config_de(args.thread),
            context=Context(usuario=args.usuario),  # contexto não é persistido
        )
        mostrar(saida, args.thread)


def cmd_estado(args: argparse.Namespace) -> None:
    with abrir() as (grafo, _):
        snap = grafo.get_state(config_de(args.thread))
        if not snap.values:
            print(f"thread '{args.thread}' não existe no Postgres")
            return
        print(f"thread={args.thread}")
        print(f"  checkpoint_id: {snap.config['configurable']['checkpoint_id']}")
        print(f"  próximos nós:  {snap.next or '(nenhum - terminou)'}")
        print(f"  interrupts:    {[i.value for i in snap.interrupts] or '(nenhum)'}")
        print(f"  valores:       {snap.values}")


def cmd_historico(args: argparse.Namespace) -> None:
    with abrir() as (grafo, _):
        hist = list(grafo.get_state_history(config_de(args.thread)))
        print(f"thread={args.thread}: {len(hist)} checkpoints, do mais novo ao mais antigo")
        for s in hist:
            passo = s.metadata.get("step", "?") if s.metadata else "?"
            proximo = ", ".join(s.next) if s.next else "(fim)"
            print(
                f"  passo {passo:>2}  próximo={proximo:<14} log={s.values.get('log', [])}"
                f"\n           id={s.config['configurable']['checkpoint_id']}"
            )


def cmd_voltar(args: argparse.Namespace) -> None:
    with abrir() as (grafo, _):
        # Sem checkpoint_id = o último checkpoint da thread. É assim que se
        # continua uma execução que caiu no meio (rede, 429, processo morto).
        conf = config_de(args.thread, args.checkpoint_id)
        if args.valor:
            # update_state NÃO desfaz nada: cria um checkpoint novo que descende
            # do escolhido (um fork) e devolve a config apontando para ele.
            novo = grafo.update_state(
                conf, {"valor": args.valor, "log": [f"valor trocado para {args.valor}"]}
            )
            conf = {**novo, "recursion_limit": 10}
        de = args.checkpoint_id[:8] + "..." if args.checkpoint_id else "o último checkpoint"
        print(f"\n> continuando thread={args.thread} a partir de {de}")
        # invoke(None, ...) = "continue daqui". Nós anteriores ao checkpoint não rodam.
        saida = grafo.invoke(None, config=conf, context=Context(usuario=args.usuario))
        mostrar(saida, args.thread)


def cmd_memoria(args: argparse.Namespace) -> None:
    with abrir() as (_, store):
        itens = store.search((args.usuario, "consultas"), limit=20)
        print(f"memória de '{args.usuario}': {len(itens)} itens")
        for it in itens:
            quando = it.updated_at.strftime("%d/%m %H:%M")
            print(f"  {it.key:<20} {it.value['vezes']}x  {it.value['resultado']}  ({quando})")


def cmd_desenhar(args: argparse.Namespace) -> None:
    print(builder.compile().get_graph().draw_mermaid())


def main() -> None:
    comum = argparse.ArgumentParser(add_help=False)
    comum.add_argument(
        "--usuario", default="Freitas", help="vai no contexto e no namespace do Store"
    )

    p = argparse.ArgumentParser(description="Lab 3 - cada subcomando é um processo separado")
    sub = p.add_subparsers(dest="comando", required=True)

    s = sub.add_parser("perguntar", parents=[comum], help="nova pergunta numa thread")
    s.add_argument("thread")
    s.add_argument("pergunta")
    s.add_argument("--sem-llm", action="store_true", help="classifica por regex, sem gastar cota")
    s.add_argument("--durability", choices=["sync", "async", "exit"], default="sync")
    s.set_defaults(fn=cmd_perguntar)

    s = sub.add_parser("retomar", parents=[comum], help="responde a um interrupt pendente")
    s.add_argument("thread")
    s.add_argument("decisao", help="sim ou nao")
    s.set_defaults(fn=cmd_retomar)

    s = sub.add_parser("estado", parents=[comum], help="último checkpoint da thread")
    s.add_argument("thread")
    s.set_defaults(fn=cmd_estado)

    s = sub.add_parser("historico", parents=[comum], help="todos os checkpoints da thread")
    s.add_argument("thread")
    s.set_defaults(fn=cmd_historico)

    s = sub.add_parser("voltar", parents=[comum], help="continua a partir de um checkpoint")
    s.add_argument("thread")
    s.add_argument("checkpoint_id", nargs="?", help="omita para continuar do último")
    s.add_argument("--valor", help="troca o valor antes de continuar (cria um fork)")
    s.set_defaults(fn=cmd_voltar)

    s = sub.add_parser("memoria", parents=[comum], help="o que o Store sabe do usuário")
    s.set_defaults(fn=cmd_memoria)

    s = sub.add_parser("desenhar", help="imprime o grafo em Mermaid")
    s.set_defaults(fn=cmd_desenhar)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
