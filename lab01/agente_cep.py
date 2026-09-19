"""Lab 1 - Primeiro agente com ferramenta real.

O que este arquivo demonstra, em ordem:
  1. uma funcao Python virando ferramenta que o modelo pode chamar
  2. o agente pronto do LangChain (create_agent) montando o loop
  3. checkpointer + thread_id dando memoria entre turnos
  4. streaming mostrando o que acontece DENTRO do loop

Rode:  uv run python lab01/agente_cep.py
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from labs.brasilapi import BrasilAPIError, get
from labs.config import get_model

# --------------------------------------------------------------- 1. a ferramenta


@tool
def consultar_cep(cep: str) -> str:
    """Consulta um CEP brasileiro e devolve o endereco.

    Use quando o usuario informar um CEP ou pedir o endereco de um CEP.
    O argumento deve conter apenas digitos, com 8 caracteres.
    """
    # A docstring acima NAO e comentario: e o texto que o modelo le para decidir
    # se chama esta ferramenta e com que argumento. Escreva para o modelo.
    limpo = "".join(c for c in cep if c.isdigit())
    if len(limpo) != 8:
        return f"CEP invalido: '{cep}'. Um CEP tem 8 digitos."

    try:
        d = get(f"/cep/v2/{limpo}")
    except BrasilAPIError as exc:
        # Devolver o erro como texto (em vez de levantar) deixa o modelo
        # reagir: pedir outro CEP, avisar o usuario, tentar de novo.
        return f"Nao consegui consultar: {exc}"

    return (
        f"CEP {d['cep']}: {d.get('street') or '(sem logradouro)'}, "
        f"{d.get('neighborhood') or '(sem bairro)'}, "
        f"{d['city']}/{d['state']}"
    )


# ------------------------------------------------------------------- 2. o agente

agente = create_agent(
    model=get_model(),
    tools=[consultar_cep],
    system_prompt=(
        "Voce atende consultas de endereco no Brasil. "
        "Sempre use a ferramenta para buscar CEP - nunca responda de memoria. "
        "Responda em portugues, em uma frase."
    ),
    checkpointer=InMemorySaver(),  # memoria de curto prazo, so nesta execucao
    name="agente_cep",
)

# O thread_id e o que amarra os turnos numa mesma conversa. Sem ele, cada
# invoke comeca do zero. Mude o valor e o agente "esquece" tudo.
CONFIG = {"configurable": {"thread_id": "lab01-demo"}}


# ---------------------------------------------------------------- 3. utilidades


def conversar(texto: str) -> None:
    """Envia uma mensagem e imprime cada passo do loop conforme ele acontece."""
    print(f"\n{'=' * 70}\nvoce: {texto}\n{'=' * 70}")

    entrada = {"messages": [{"role": "user", "content": texto}]}

    for chunk in agente.stream(entrada, CONFIG, stream_mode="updates", version="v2"):
        # No formato v2 todo chunk tem a mesma forma: type / ns / data.
        for no, delta in chunk["data"].items():
            if not isinstance(delta, dict):
                continue
            for msg in delta.get("messages", []):
                if getattr(msg, "tool_calls", None):
                    for tc in msg.tool_calls:
                        print(f"  [{no}] chamou {tc['name']}({tc['args']})")
                elif msg.type == "tool":
                    print(f"  [{no}] ferramenta devolveu: {msg.text}")
                elif msg.text:
                    # .content pode ser uma LISTA de content blocks (texto,
                    # raciocinio, assinatura do provedor). .text extrai so o
                    # texto e concatena - e o que voce quer exibir.
                    print(f"  [{no}] resposta: {msg.text}")


# ------------------------------------------------------------------- 4. a demo

if __name__ == "__main__":
    # Turno 1: o modelo precisa decidir chamar a ferramenta.
    conversar("Qual o endereco do CEP 68502-290?")

    # Turno 2: nenhuma menção ao CEP. So funciona porque o checkpointer
    # guardou o historico sob o mesmo thread_id.
    conversar("E em que estado fica isso?")

    # Turno 3: entrada invalida - veja o modelo reagir ao texto de erro.
    conversar("Consulta o CEP 123 para mim")

    print(f"\n{'=' * 70}")
    print("Estado final da thread:")
    estado = agente.get_state(CONFIG)
    print(f"  mensagens acumuladas: {len(estado.values['messages'])}")
    print(f"  proximos nos a executar: {estado.next or '(nenhum - terminou)'}")
