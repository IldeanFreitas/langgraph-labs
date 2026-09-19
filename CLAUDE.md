# Contexto do projeto — Laboratório LangGraph

Repositório de estudo: 5 labs do básico ao avançado, usando a BrasilAPI como
fonte de dados. Repositório de aprendizado, não biblioteca.

## Regra zero

Nunca afirme comportamento de API, versão, limite ou licença sem confirmar na
documentação oficial (docs.langchain.com para guias, reference.langchain.com
para API). Ao corrigir algo dito antes, diga o que mudou e a fonte.
Carregue a skill `langgraph-especialista` antes de mexer em código de grafo.

## Stack (verificado em 19/set/2026)

| Pacote | Versão |
|---|---|
| `langgraph` | 1.2.11 |
| `langchain` | 1.4.2 |
| `langchain-google-genai` | 4.4.0 |
| `langgraph-checkpoint-postgres` | 3.1.2 |
| `langgraph-cli[inmem]` | 0.4.31 |

Python 3.12 + `uv`, Windows nativo. Docker Desktop roda só o Postgres.
Modelo: `google_genai:gemini-3.8-flash` (free tier).

## Restrição ativa

Free tier do Gemini. O 429 real (19/set/2026) reporta
`GenerateRequestsPerDayPerProjectPerModel-FreeTier = 20` para o
`gemini-3.8-flash`, com `retryDelay` de 4 a 50 s — é throttle de rajada, não
corte seco (uma chamada passou 30 s depois de um 429; três tentativas com 20 s e
40 s de espera falharam mais tarde no mesmo dia). Conte com **~20 chamadas/dia**
e espace-as. A cota é **por modelo**: com o 3.8 esgotado, `LAB_MODEL=google_genai:gemini-3.7-flash`
(ou `gemini-3.5-flash-lite`) abre um balde novo sem tocar em código — foi assim
que o lab 3 foi validado. O orçamento é de *chamadas ao modelo*, não de execuções: prefira
sempre a solução com menos chamadas, use `CachePolicy` nos nós, limite fan-out
a 3 itens, e ofereça `--sem-llm` onde o mecanismo não depende do modelo (lab 3).

## Estrutura

```
labs/
├── src/labs/config.py      # get_model() — único ponto que conhece o provedor
├── src/labs/brasilapi.py   # cliente httpx com lru_cache e erro tratado
├── tests/                  # gate: a BrasilAPI responde?
├── scripts/                # bootstrap.ps1, docker-reset.ps1
├── lab01/  agente_cep.py   # create_agent + tool + InMemorySaver
├── lab02/  roteador.py     # StateGraph + reducers + conditional edges
├── lab03/  persistente.py  # CLI: PostgresSaver + Store + interrupt() + time travel
├── lab04/  mesa.py         # Send (fan-out) + subgrafos + Command.PARENT + supervisor
├── lab05/  servico.py      # mesa async: TimeoutPolicy, error_handler, RunControl
├── langgraph.json          # os 5 grafos servidos por `langgraph dev` (lab 3 via fábrica)
├── Dockerfile              # gerado por `langgraph dockerfile`; .dockerignore barra o .env
├── tests/test_grafos.py    # stub de modelo (lab 2) + --sem-llm (labs 4 e 5), 11 testes
└── docs/                   # mapa do projeto: mapa.html (página) + mapa-labs.svg / mapa-servico.svg (README)
```

## Convenções

- Nenhum lab importa o SDK do provedor. Trocar de modelo = trocar `LAB_MODEL` no `.env`.
- Toda chamada externa passa por `labs/brasilapi.py`.
- `recursion_limit` explícito em todo grafo (10 a 15).
- Nó retorna **delta**, nunca o estado inteiro.
- Segredo nunca entra no estado (o estado é persistido).
- Cada lab tem README com passo a passo e **resultado esperado** por passo, mais
  experimentos que quebram o código de propósito para provar o mecanismo.

## Estado

| Lab | Tema | Status |
|---|---|---|
| 1 | `create_agent` + tool + memória de thread | Executado |
| 2 | Graph API: estado, reducers, roteamento (1 chamada de LLM) | Executado (3/5 perguntas; a 4ª caiu em 429 de cota) |
| 3 | Postgres, `Store`, `interrupt()`, time travel | Executado (12 passos + 7 experimentos; LLM via `gemini-3.7-flash`, cota do 3.8 esgotada) |
| 4 | `Send` (fan-out) + supervisor com 3 especialistas | Executado (2 partes, 7 experimentos; LLM via `gemini-3.7-flash`) |
| 5 | `langgraph dev`, Studio, testes, Docker, async + timeouts | Executado (11 passos + 6 experimentos; container só construído — rodar exige `LANGSMITH_API_KEY`) |

## Armadilhas já encontradas (não repetir)

1. **`AIMessage.content` não é string.** Na LangChain 1.x é payload cru — Gemini
   3.x devolve lista de content blocks com `signature`. Use `.text` para exibir.
2. **`TimeoutPolicy` só vale para nó async.** Em nó sync o `compile()` levanta
   `ValueError`. Timeout entra no lab 5, com nós async.
3. **PowerShell 5.1:** `comando 2>&1` com `$ErrorActionPreference='Stop'`
   transforma stderr de programa nativo em erro terminante. Os scripts usam
   `Invoke-Native`, que rebaixa a preferência e julga pelo `$LASTEXITCODE`.
4. **Não retornar código de saída pelo pipeline** numa função PowerShell — isso
   engole a saída do comando.
5. **`RetryPolicy` padrão não repete `RuntimeError`** (`default_retry_on` só
   repete `ConnectionError` e HTTP 5xx). `BrasilAPIError` herda de
   `RuntimeError`, então o retry do lab 2 nunca dispara para a BrasilAPI. Para o
   429 do modelo use `retry_on=ModelRateLimitError` (langchain_core) com
   `initial_interval` da ordem do `retryDelay` (~20 s).
6. **`@lru_cache` em `brasilapi.get` mascara o `CachePolicy`.** Medir cache de
   nó pelo tempo não funciona; meça com um print dentro do nó.
7. **Contexto (`context_schema`) não é persistido.** Ao retomar com
   `Command(resume=...)` ou `invoke(None, config)`, passe `context=` de novo, ou
   `runtime.context` vem `None`.
8. **Resume e replay re-executam o nó inteiro**, efeitos colaterais inclusos.
   `store.put` em `formatar` conta duas vezes num replay; e-mail antes do
   `interrupt()` sai duas vezes. Efeito colateral vai depois do interrupt e
   precisa ser idempotente.
9. **Subgrafo que sai por `Command.PARENT` não copia o estado dele para o pai.**
   Só o `update` do `Command` chega. Chave compartilhada só volta quando o
   subgrafo chega ao próprio `END`. Guarde o resultado em chave privada e
   entregue no `update`.
10. **Nó de subgrafo que faz handoff não pode anotar `Command[Literal["nó_do_pai"]]`**
    — o `compile()` do subgrafo valida contra os nós dele e falha. Anote só
    `-> Command` e declare o destino com `destinations=` no `add_node` do pai
    (só para o desenho).
11. **`goto` para nó do pai sem `graph=Command.PARENT` não dá erro** — só um
    aviso `wrote to unknown channel branch:to:X, ignoring it`, e a execução
    termina em silêncio.
12. **`error_handler` não segura falha de tarefa que rodou em paralelo** (bug
    aberto langchain-ai/langgraph#8277 em 1.2.11): o handler roda, mas a
    exceção sobe. Com uma tarefa só no superstep, funciona. Em fan-out, o nó
    trata a própria falha (`try/except` + `asyncio.timeout`). Há um teste que
    documenta o bug e vai falhar quando for corrigido.
13. **Handler que devolve `dict` encerra o fluxo**: as arestas de saída do nó
    que falhou não são seguidas. Para continuar, `Command(goto=...)`.
14. **`set_node_defaults(timeout=)` alcança o nó interno do handler** — ele
    precisa ser `async`, senão o `compile()` recusa.
15. **Timeout é cooperativo**: `time.sleep` num nó async não é interrompido nem
    marcado como timeout — o nó termina normalmente depois.
16. **`httpx.AsyncClient` global fica preso ao event loop em que nasceu**: com um
    loop por teste (pytest-asyncio), `Event loop is closed`. Client por chamada
    nos labs; por aplicação (lifespan) em produção.
17. **`langgraph --help`/`dev` no Windows quebra com `UnicodeEncodeError`** (emoji
    no cp1252). `$env:PYTHONUTF8 = "1"` antes.
18. **Grafo servido não pode ter checkpointer próprio**: o servidor injeta o
    dele. O lab 3 expõe `para_servidor()` que compila sem Postgres.

## Ambiente

```powershell
uv sync
uv run pytest tests/test_brasilapi.py -v    # gate
docker compose up -d                        # Postgres, lab 3+
uv run python lab02/roteador.py
uv run python lab03/persistente.py perguntar t1 "Qual o endereço do CEP 68502290?" --sem-llm
uv run python lab04/mesa.py paralelo --sem-llm ; uv run python lab04/mesa.py supervisor --sem-llm
docker exec langgraph-labs-pg psql -U langgraph -d langgraph -c "TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes, store"   # zera o lab 3
uv run python lab05/servico.py mesa --sem-llm ; uv run python lab05/servico.py drenar
$env:PYTHONUTF8 = "1"; uv run langgraph dev --no-browser     # http://127.0.0.1:2024
uv run pytest tests -v                                       # 11 testes, sem cota
docker build -t langgraph-labs:lab05 .                       # imagem do LangGraph Server
```

Tarefas do VS Code em `Ctrl+Shift+P > Tasks: Run Task`.
