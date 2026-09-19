# Lab 5 — Produção: timeouts, drenagem, serviço e container

**Tempo:** ~1h30 · **Custo:** 2 chamadas de LLM na mesa (0 com `--sem-llm`) · **Pré-requisitos:** `.env`; Docker Desktop só para o container

## O que você vai entender

| Conceito | Onde aparece |
|---|---|
| Nós `async` | a mesa do lab 4 reescrita com `await` — pré-requisito de tudo abaixo |
| `TimeoutPolicy` | `run_timeout` por nó; `NodeTimeoutError` é repetível de propósito |
| `error_handler` + `NodeError` | falha vira dado depois que o retry esgota |
| `set_node_defaults` | política para todos; o nó sobrescreve (o do modelo tem outro timeout) |
| `RunControl` + `GraphDrained` | parar no fim do superstep com checkpoint salvo, e retomar |
| `runtime.drain_requested` | o nó sabe que o processo está sendo drenado |
| `langgraph.json` + `langgraph dev` | os grafos dos labs 2–5 viram API REST + Studio |
| `langgraph_sdk` | chamar o serviço de Python, inclusive `interrupt`/`resume` |
| Testes com stub | `tests/test_grafos.py`: 11 testes, zero cota |
| `langgraph dockerfile` | imagem do LangGraph Server com os 5 grafos |

## A ideia

Produção é o mesmo grafo com três coisas a mais: **limites** (timeout, retry,
handler), **desligamento sem perda** (drenagem + checkpoint) e **uma porta**
(o servidor). O lab 4 já tinha o grafo; aqui ele ganha as três.

O grafo é a mesa paralela, agora `async` — é isso que destrava o
`TimeoutPolicy` que o lab 2 não pôde usar. E o especialista tem **duas camadas**
de proteção, porque uma só não basta (veja o passo 4):

```
especialista
  ├─ camada 1: asyncio.timeout() em volta da consulta -> o nó trata e devolve "falhou"
  └─ camada 2: TimeoutPolicy(2 s) do grafo -> NodeTimeoutError -> retry -> error_handler
```

## Passo a passo

**1. Rode a mesa async.**

```powershell
uv run python lab05/servico.py mesa --sem-llm
```

**Esperado:** igual ao lab 4 (três resultados, `tempo real da mesa: ~300 ms`).
O cabeçalho mostra os dois limites: `timeout interno=1.5s | TimeoutPolicy=2.0s`.

**2. Dispare a camada 1.**

```powershell
uv run python lab05/servico.py mesa --sem-llm --timeout-consulta 0.001
```

**Esperado:** três linhas `falhou (timeout interno de 0.001s)` e a mesa
**termina** em ~70 ms, com relatório. O nó tratou o próprio timeout com
`asyncio.timeout()`; o grafo nem soube.

**3. Dispare a camada 2 — com uma consulta.**

```powershell
uv run python lab05/servico.py mesa --sem-llm --atraso 5 "CEP 68502290"
```

**Esperado:** ~5 s e `[cep  falhou ] cep 68502290: falhou (timeout run de 2.0s)`,
seguido de `Resultados:`. A conta: `--atraso 5` trava o nó **fora** da camada 1;
o `TimeoutPolicy` cancela aos 2 s; o `RetryPolicy(max_attempts=2)` tenta de
novo (NodeTimeoutError é repetível); aos 2 s cancela de novo; o retry esgota;
o `error_handler` recebe a `Consulta` e o `NodeError`, devolve o resultado e
**roteia** para `relatar` com `Command(goto=)`.

**4. Dispare a camada 2 — com três consultas.**

```powershell
uv run python lab05/servico.py mesa --sem-llm --atraso 5
```

**Esperado:** `NodeTimeoutError: Node 'especialista' exceeded its run timeout`
— o handler **não** segurou. É o bug aberto
[langchain-ai/langgraph#8277](https://github.com/langchain-ai/langgraph/issues/8277)
(1.2.11): quando o nó que falha roda em paralelo com outras tarefas no mesmo
superstep, o handler executa mas a exceção sobe mesmo assim. Com uma tarefa só
(passo 3) funciona. **É por isso que a camada 1 existe**: em fan-out, o nó não
pode depender do handler. O teste `test_lab05_bug_8277_...` vai falhar no dia em
que o upstream corrigir — aí este passo muda.

**5. Drene no meio.**

```powershell
uv run python lab05/servico.py drenar
```

**Esperado:**

```
[cep] drenagem pedida (sigterm simulado) - concluindo
[ddd] drenagem pedida (sigterm simulado) - concluindo
[banco] drenagem pedida (sigterm simulado) - concluindo
DRENADO (sigterm simulado) no fim do superstep. Checkpoint salvo.
resultados já gravados: 3
próximos nós: ('relatar',)
... processo novo: invoke(None, config) continua de onde parou
```

e o relatório. O `RunControl.request_drain()` (aqui um timer; em produção,
o handler de SIGTERM) não mata nada: os três especialistas **terminam**, o
LangGraph grava o checkpoint e levanta `GraphDrained` antes de `relatar`. O
"processo novo" é `invoke(None, config)` — o mesmo mecanismo do lab 3.

**6. Suba o serviço.** Em um terminal:

```powershell
$env:PYTHONUTF8 = "1"        # o CLI imprime emoji; sem isso o console cp1252 quebra
uv run langgraph dev --no-browser
```

**Esperado:** `Application started up`, API em http://127.0.0.1:2024, docs em
http://127.0.0.1:2024/docs, e o Studio em
https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024. O
[langgraph.json](../langgraph.json) expõe cinco grafos; o servidor **injeta**
checkpointer e store (em memória no `dev`) — por isso o lab 3 entra por uma
fábrica `para_servidor()` que compila sem Postgres.

**7. Chame pelo SDK.** Em outro terminal:

```powershell
uv run python -c "
from langgraph_sdk import get_sync_client
c = get_sync_client(url='http://127.0.0.1:2024')
print([a['graph_id'] for a in c.assistants.search()])
r = c.runs.wait(None, 'lab05_servico', input={'pergunta': 'CEP 68502290 e DDD 94', 'resultados': []}, context={'sem_llm': True})
print([x['texto'] for x in r['resultados']])
"
```

**Esperado:** a lista com os cinco `graph_id` e os dois resultados. O `context`
viaja pela API igual ao `context=` do `invoke`.

**8. Aprovação humana pelo servidor.** Ainda com o `dev` no ar:

```powershell
uv run python -c "
from langgraph_sdk import get_sync_client
c = get_sync_client(url='http://127.0.0.1:2024')
t = c.threads.create()
r = c.runs.wait(t['thread_id'], 'lab03_persistente', input={'pergunta': 'CNPJ 00000000000191', 'log': []}, context={'usuario': 'Freitas', 'sem_llm': True})
print('pausado:', r['__interrupt__'][0]['value'])
print('next:', c.threads.get_state(t['thread_id'])['next'])
r = c.runs.wait(t['thread_id'], 'lab03_persistente', command={'resume': True}, context={'usuario': 'Freitas'})
print(' -> '.join(r['log']))
"
```

**Esperado:** `pausado: {...'cnpj': '00000000000191'}`, `next: ['aprovar']` e
`classificado como cnpj -> aprovado por humano -> consultou cnpj -> formatado`.
É o lab 3 inteiro sem Postgres nem CLI: thread, interrupt, `get_state` e
`Command(resume=)` viraram chamadas HTTP.

**9. Rode os testes.** `Ctrl+C` no servidor e:

```powershell
uv run pytest tests -v
```

**Esperado:** `11 passed` em ~13 s. Abra
[tests/test_grafos.py](../tests/test_grafos.py): o lab 2 é testado com um
`ModeloFalso` no lugar do `get_model` (roteamento, reducer e contexto sem
gastar cota); os labs 4 e 5 usam o caminho `--sem-llm`; a drenagem e os dois
timeouts têm teste próprio; e o bug #8277 tem um teste que **documenta** o
comportamento atual.

**10. Construa a imagem.**

```powershell
uv run langgraph dockerfile Dockerfile     # já está no repositório
docker build -t langgraph-labs:lab05 .
```

**Esperado:** imagem `langgraph-labs:lab05` de ~1,1 GB sobre
`langchain/langgraph-api:3.12`, com os cinco grafos em `LANGSERVE_GRAPHS`.
Confira que o segredo ficou de fora:

```powershell
docker run --rm --entrypoint sh langgraph-labs:lab05 -c "ls /deps/labs"
```

**Esperado:** sem `.env` na lista — é o [.dockerignore](../.dockerignore).

**11. Rodar o container — não executado aqui.** O LangGraph Server precisa de
`LANGSMITH_API_KEY` (plano Developer, gratuito) para subir, mais Postgres e
Redis. O CLI monta tudo:

```powershell
$env:LANGSMITH_API_KEY = "lsv2_..."
uv run langgraph up          # porta 8123
```

Este passo **não foi executado** neste repositório porque o `.env` não tem a
chave. Com ela, o passo 7 funciona igual trocando a URL para
`http://localhost:8123`. O mesmo vale para o tracing:
`LANGSMITH_TRACING=true` + a chave, e cada run aparece em smith.langchain.com.

## Experimentos

**A. Bloqueie o loop.** No `especialista`, troque
`await asyncio.sleep(runtime.context.atraso_s)` por
`time.sleep(runtime.context.atraso_s)` e rode o passo 3.
**Esperado:** nenhum timeout — o nó **termina** depois de 5 s com o resultado
certo. O cancelamento é cooperativo: o watchdog do `TimeoutPolicy` só roda
quando o nó cede o loop, e um `time.sleep` nunca cede. Timeout em nó async só
vale para código async de verdade. Desfaça.

**B. Handler sync.** Tire o `async` de `especialista_falhou`.
**Esperado:** `ValueError: Node timeouts are only supported for async nodes ...
Node '__error_handler__especialista' is sync` — no `compile()`, antes de
qualquer execução. O handler é um nó, e o timeout padrão do
`set_node_defaults` vale para ele. Desfaça.

**C. Handler que devolve dict.** Em `especialista_falhou`, troque o `return`
por `return {"resultados": [resultado]}` e rode o passo 3.
**Esperado:** `KeyError: 'relatorio'` — o grafo terminou logo depois do
handler. As arestas de saída do nó que falhou **não** são seguidas; quem
decide para onde ir é o `Command(goto=)` do handler. Desfaça.

**D. Drene tarde.** `drenar --apos 5`.
**Esperado:** `terminou antes do pedido de drenagem`. Drenagem só age em
fronteira de superstep; se não há próximo superstep, não há o que drenar.

**E. Sem timeout no modelo (com LLM, 1 chamada).** Em `add_node("planejar", ...)`
remova `timeout=timeout_modelo` e rode `mesa` sem `--sem-llm`.
**Esperado:** `NodeTimeoutError` em `planejar` — o nó herdou os 2 s do
`set_node_defaults`, e um LLM raramente responde em 2 s. Padrão é para o caso
comum; nó de modelo é exceção. Desfaça.

**F. Run sem thread no servidor.** No passo 7, o `runs.wait(None, ...)` roda
sem `thread_id`. Olhe o log do `langgraph dev`.
**Esperado:** `UserWarning: durability has no effect when no checkpointer is
present`. Run sem thread é stateless: nada é gravado. O passo 8 cria a thread
antes justamente por isso.

## Exercícios

**A. `idle_timeout` com heartbeat.** Faça um especialista que processa uma
lista em partes e chame `runtime.heartbeat()` a cada parte, com
`TimeoutPolicy(idle_timeout=1, refresh_on="heartbeat")`.
**Esperado:** o nó dura mais de 1 s sem estourar, porque cada heartbeat zera o
relógio de ociosidade; tire o heartbeat e ele estoura.

**B. SIGTERM de verdade.** Em `cmd_drenar`, troque o timer por
`signal.signal(signal.SIGINT, lambda *_: control.request_drain("ctrl+c"))` e
aperte `Ctrl+C` durante os especialistas.
**Esperado:** o mesmo `DRENADO`, agora por sinal — é assim que um container
recebe o pedido de parada do orquestrador.

**C. Studio.** Abra a URL do passo 6 no navegador, escolha `lab03_persistente`,
envie `{"pergunta": "CNPJ 00000000000191"}` com contexto `{"usuario": "Você", "sem_llm": true}`.
**Esperado:** o grafo para em `aprovar` na tela; retome pela interface.

**D. Container de verdade.** Pegue a chave gratuita em smith.langchain.com,
rode o passo 11 e repita o passo 8 contra a porta 8123.
**Esperado:** igual — agora com Postgres e Redis reais por baixo.

## O que isso custa

| | Lab 4 (sync) | Lab 5 (async) |
|---|---|---|
| Timeout por nó | impossível (`compile()` recusa) | `TimeoutPolicy`, cooperativo |
| Falha em fan-out | `try/except` no nó | `try/except` + `asyncio.timeout` no nó; handler como rede de segurança |
| Desligamento | mata o processo, perde o passo | drena, grava checkpoint, retoma |
| Porta de entrada | CLI | API REST + Studio + SDK, sem código novo |
| Chamadas de LLM | 2 | 2 (as mesmas) |

O que **não** mudou é o custo: virar serviço não gasta uma chamada a mais.

## Armadilhas que este lab encontrou

1. `error_handler` + tarefas paralelas no mesmo superstep: a exceção sobe mesmo
   assim (bug #8277, aberto em 1.2.11). Defesa dentro do nó.
2. Handler que devolve `dict` encerra o fluxo; para continuar, `Command(goto=)`.
3. `set_node_defaults(timeout=)` alcança o nó interno do handler — ele precisa
   ser `async`.
4. `httpx.AsyncClient` global fica preso ao event loop em que nasceu: com um
   loop por teste, `Event loop is closed`. Um client por chamada aqui; um por
   aplicação (lifespan) em produção.
5. `langgraph --help` no Windows: `UnicodeEncodeError` por causa dos emojis.
   `$env:PYTHONUTF8 = "1"` resolve.
6. `time.sleep` num nó async não é interrompido pelo timeout — e não é marcado.

## Fontes

- Tolerância a falhas (`TimeoutPolicy`, `error_handler`, `RunControl`): https://docs.langchain.com/oss/python/langgraph/fault-tolerance
- Servidor local e Studio: https://docs.langchain.com/oss/python/langgraph/local-server
- CLI e `langgraph.json`: https://docs.langchain.com/langsmith/cli
- Estrutura da aplicação: https://docs.langchain.com/oss/python/langgraph/application-structure
- Bug do handler em paralelo: https://github.com/langchain-ai/langgraph/issues/8277
