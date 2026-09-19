# Lab 2 — Graph API: estado, reducers e roteamento

**Tempo:** ~1h · **Custo:** 1 chamada de LLM por pergunta (o lab 1 gastava ~3)

## O que voce vai entender

| Conceito | Onde aparece |
|---|---|
| `StateGraph` + `TypedDict` | o estado declarado explicitamente |
| `Annotated[list, add]` | reducer: nos ANEXAM ao log em vez de sobrescrever |
| `context_schema` + `Runtime` | dado fixo da invocacao, fora do estado |
| Saida estruturada | Pydantic + `with_structured_output` |
| `add_conditional_edges` | roteamento em Python puro, custo zero |
| `Command` | atualizar estado e rotear no mesmo no |
| `RetryPolicy` | resiliencia via `set_node_defaults` (timeout so em no async) |
| `CachePolicy` | entrada repetida nao refaz o trabalho |

## A ideia

O lab 1 deixava o modelo decidir tudo, a cada turno. Mas se voce **ja sabe** que
"CEP" vai para um lugar e "DDD" para outro, pagar um LLM para redescobrir isso
toda vez e desperdicio.

Aqui o LLM faz uma coisa so: ler a pergunta e devolver `{tipo, valor}`. Dali em
diante e Python.

```
START -> classificar (LLM) -> [cep | cnpj | ddd | banco] -> formatar -> END
                            \-> desconhecido -> END   (via Command)
```

## Passo a passo

**1. Rode.**

```powershell
uv run python lab02/roteador.py
```

**Esperado:** primeiro o grafo em Mermaid, depois 5 perguntas respondidas, e no
fim `Chamadas de LLM no total: 5`. Se aparecer `429 RESOURCE_EXHAUSTED` no meio,
e o throttle do free tier (veja "O que isso custa"); espere o `retryDelay` que
a mensagem informa e rode de novo.

**2. Confira o contador.**
Foram 5 perguntas e 5 chamadas — a quinta repetiu a primeira e mesmo assim
passou pelo modelo. O cache e do **no** `cep`, nao do classificador. Olhe o
tempo.

**Esperado:** a 5a pergunta (repetida) leva o mesmo tempo no `classificar`, mas
o no `cep` responde em quase 0 ms porque o `CachePolicy` guardou o resultado.

**3. Veja o desenho.**
Copie o bloco Mermaid impresso e cole em https://mermaid.live.
**Esperado:** `classificar` com 5 setas saindo, quatro convergindo em `formatar`,
e `desconhecido` indo direto ao `END`.

**4. Prove o reducer.**
Em `State`, troque a linha do log para `log: list[str]` (sem `Annotated`).
Rode.
**Esperado:** `caminho:` mostra so a ultima etapa — cada no sobrescreveu o log
anterior. Desfaca.

**5. Prove o contexto.**
Em `perguntar()`, remova `context=Context(usuario="Freitas")`.
**Esperado:** erro ao acessar `runtime.context.usuario`. Contexto nao tem valor
padrao — ou voce passa, ou quebra. Desfaca.

**6. Prove o roteamento.**
Em `rotear()`, troque o `return` por `return "banco"` fixo.
**Esperado:** toda pergunta cai no no de banco, inclusive a de CEP — e falha na
BrasilAPI. O roteador e quem manda, nao o classificador. Desfaca.

## Exercicios

**A. Novo tipo.** Adicione `feriado` usando `GET /feriados/v1/{ano}`. Sao tres
mudancas: o `Literal` da `Classificacao`, um no novo, e uma entrada no mapa do
`add_conditional_edges`.
**Esperado:** "quais os feriados de 2026?" roteia certo sem voce tocar no prompt.

**B. Quebre o retry.** Em `consultar_cep`, troque o caminho para
`/cep/v2/00000000` (CEP inexistente).
**Esperado:** falha na **primeira** tentativa, sem retry nenhum. A politica
padrao (`default_retry_on`) so repete `ConnectionError` e HTTP 5xx; `ValueError`,
`RuntimeError` e o resto da familia "bug de codigo" nao sao repetidos — e
`BrasilAPIError` herda de `RuntimeError`. Agora troque para
`RetryPolicy(max_attempts=3, retry_on=BrasilAPIError)`.
**Esperado:** tres tentativas (esperas de 0,5 s e 1 s, mais jitter) antes de
falhar — todas com 404, que nao melhora repetindo. Retry e para erro
transitorio. O lab 3 faz a versao certa: `retry_on=ModelRateLimitError`, so
para o 429 do modelo, com intervalo do tamanho que o provedor pede.

**C. Meca o cache.** Coloque `print("  [cep] executando")` na primeira linha de
`consultar_cep` e rode.
**Esperado:** o print aparece na 1a pergunta e **nao** aparece na 5a — com
`CachePolicy`, o no nem executa. Agora comente `cache=InMemoryCache()` no
`compile()` e rode de novo.
**Esperado:** o print aparece nas duas, mas o tempo quase nao muda — porque
`labs/brasilapi.py` tem `@lru_cache` e poupa o HTTP de qualquer jeito. Sao duas
camadas com propositos diferentes: `CachePolicy` poupa o **no inteiro**;
`lru_cache` poupa so a **rede**. Desfaca.

**D. Command no lugar de aresta.** Faca `consultar_cep` devolver
`Command(update={...}, goto="formatar")` em vez de `dict`, e remova
`builder.add_edge("cep", "formatar")`.
**Esperado:** mesmo comportamento. Compare a legibilidade — aresta e melhor
quando o destino e fixo; `Command` quando depende do que o no calculou.

## O que isso custa

| | Lab 1 | Lab 2 |
|---|---|---|
| Chamadas de LLM por pergunta | ~3 | 1 |
| Roteamento | decidido pelo modelo, pode variar | deterministico, sempre igual |
| Auditavel | so pelo historico de mensagens | grafo desenhavel + log no estado |
| Flexivel para pergunta nova | sim, sem codigo | nao, exige novo no |

Esse e o trade-off central do framework. Agente quando o caminho e imprevisivel;
grafo quando voce ja conhece o caminho.

O 429 do free tier reporta `limit: 20` por dia para o `gemini-3.8-flash`
(`quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier`) e chega com um
`retryDelay` de segundos — e um throttle de rajada, nao um corte seco. Conte
com ~20 chamadas por dia: o lab 2 rende ~20 perguntas; o lab 1, ~6.

## Perguntas para levar ao lab 3

- O estado sumiu quando o processo terminou. E se a consulta levasse 10 minutos?
- E se antes de consultar o CNPJ alguem precisasse aprovar?

## Fontes

- Graph API: https://docs.langchain.com/oss/python/langgraph/graph-api
- Tolerancia a falhas: https://docs.langchain.com/oss/python/langgraph/fault-tolerance
