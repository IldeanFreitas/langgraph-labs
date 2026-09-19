# Lab 1 — Primeiro agente com ferramenta real

**Tempo:** ~1h · **Pre-requisito:** `bootstrap.ps1` rodado e `GOOGLE_API_KEY` preenchida no `.env`

## O que voce vai entender

| Conceito | Onde aparece |
|---|---|
| Ferramenta (`@tool`) | uma funcao Python que o modelo decide chamar sozinho |
| Loop do agente | modelo → ferramenta → resultado → modelo, ate parar |
| Estado de mensagens | o historico que cresce a cada turno |
| Checkpointer + `thread_id` | o que faz o turno 2 lembrar do turno 1 |
| Streaming | ver o que acontece dentro do loop, nao so o fim |

## Passo a passo

**1. Confirme a fundacao.**

```powershell
uv run pytest tests/test_brasilapi.py -v
```
**Esperado:** 2 testes passando. Se falhar, pare aqui — o problema e rede ou API.

**2. Confirme que o modelo responde.**

```powershell
uv run python -c "from labs.config import get_model; print(get_model().invoke('responda apenas: ok').content)"
```
**Esperado:** `ok`. Se der erro de credencial, revise `GOOGLE_API_KEY` no `.env`.

**3. Rode o agente.**

```powershell
uv run python lab01/agente_cep.py
```
**Esperado:** tres blocos de conversa. No primeiro voce ve, em ordem:
`chamou consultar_cep({'cep': '68502290'})` → `ferramenta devolveu: CEP 68502-290...` → `resposta: ...Maraba/PA`.
Ao final, `mensagens acumuladas` deve estar em torno de 9 e `proximos nos` vazio.

**4. Observe o turno 2.**
A pergunta "E em que estado fica isso?" nao cita CEP nenhum. O modelo responde
porque o `InMemorySaver` guardou o historico sob `thread_id="lab01-demo"`.

**5. Prove que o `thread_id` e o que importa.**
Em `agente_cep.py`, na linha do `CONFIG`, troque `"lab01-demo"` por `"outra"`.
Rode de novo.
**Esperado:** o turno 2 agora se perde ou pergunta de que endereco voce fala —
porque comecou uma conversa vazia.
Desfaca a mudanca antes de seguir.

**6. Observe o turno 3.**
`consultar_cep` recebe `"123"` e devolve **texto de erro em vez de levantar excecao**.
**Esperado:** o modelo le esse texto e responde pedindo um CEP valido.
Esse e o padrao: ferramenta que falha devolve mensagem, nao explode. Quem
levanta excecao tira do modelo a chance de se recuperar.

## Exercicios

**A. Segunda ferramenta.** Crie `consultar_ddd(ddd: str)` usando `GET /ddd/v1/{ddd}`
(a resposta traz `state` e `cities`). Adicione a `tools=[...]` e pergunte
*"quais cidades tem DDD 94?"*.
**Esperado:** o modelo escolhe a ferramenta certa sem voce dizer qual.

**B. Duas ferramentas no mesmo turno.** Pergunte
*"o CEP 68502290 fica em area de DDD 94?"*.
**Esperado:** duas chamadas de ferramenta antes da resposta final. Veja no stream.

**C. Troque o modo de streaming.** Em `conversar()`, troque `stream_mode="updates"`
por `"messages"`.
**Esperado:** a resposta chega token a token, nao de uma vez. Compare os dois —
`updates` serve para logar o fluxo, `messages` para a interface do usuario.

**D. Tire a docstring** de `consultar_cep` e rode.
**Esperado:** o modelo erra a escolha ou o argumento. A docstring e parte do contrato.

## Perguntas para levar ao lab 2

- Quantas chamadas ao modelo aconteceram para responder uma pergunta simples?
- Se voce ja sabe que "CEP" vai para `consultar_cep` e "DDD" para `consultar_ddd`,
  precisa mesmo de um LLM decidindo isso a cada turno?

O lab 2 responde as duas construindo o mesmo comportamento com `StateGraph` —
e usando **uma** chamada de modelo em vez de tres.

## Fontes

- `create_agent`: https://docs.langchain.com/oss/python/langchain/agents
- Streaming: https://docs.langchain.com/oss/python/langgraph/streaming
- Checkpointers: https://docs.langchain.com/oss/python/langgraph/checkpointers
- BrasilAPI: https://brasilapi.com.br/docs
