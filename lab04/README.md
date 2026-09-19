# Lab 4 — Mesa de consultas: paralelismo e multi-agente

**Tempo:** ~1h · **Custo:** 2 chamadas de LLM por execução, em qualquer das duas partes (0 com `--sem-llm`) · **Pré-requisito:** nenhum além do `.env`

## O que você vai entender

| Conceito | Onde aparece |
|---|---|
| `Send` | fan-out dinâmico: um nó por consulta, cada um com o próprio input |
| Reducer em escrita paralela | N especialistas escrevendo `resultados` no mesmo passo |
| `input_schema` | o nó `especialista` recebe uma `Consulta`, não o estado da mesa |
| Subgrafo como nó | `StateGraph` compilado dentro de outro, conversando por chave compartilhada |
| `Command(graph=Command.PARENT)` | handoff: sair do subgrafo e rotear no pai |
| Supervisor com saída estruturada | decide o próximo membro ou encerra |
| `RemainingSteps` | encerrar com elegância antes do `GraphRecursionError` |
| `stream(subgraphs=True)` | ver os passos de dentro do subgrafo, com namespace |

## A ideia

Uma pergunta pode pedir várias consultas. A decisão de arquitetura é: elas
**dependem** uma da outra?

**Não dependem → Parte A, paralelo.** O LLM planeja tudo de uma vez, cada
consulta vira um `Send`, todas rodam no mesmo passo, e o LLM redige o relatório.
Duas chamadas, não importa se são 1 ou 3 consultas.

```
START -> planejar (LLM) -> Send x N -> especialista (Python, em paralelo) -> relatar (LLM) -> END
```

**Dependem → Parte B, supervisor.** "Endereço da empresa X" exige o CNPJ
*antes* do CEP. Um supervisor (LLM) escolhe o próximo especialista; cada
especialista é um **subgrafo** que devolve o controle via `Command.PARENT`. O
truque que segura o custo: quando o especialista de CNPJ descobre o CEP, ele
faz **handoff direto** para o de CEP, sem voltar ao supervisor — e uma rodada
de supervisor a menos é uma chamada de LLM a menos.

```
START -> supervisor (LLM) -> [cnpj] --handoff--> [cep] -> supervisor (LLM) -> END
                              subgrafo           subgrafo
```

A regra da casa continua: LLM decide, Python executa. Os quatro especialistas
são funções puras sobre a BrasilAPI, e erro vira texto no resultado — um
especialista que explode derrubaria a mesa inteira.

## Passo a passo

**1. Veja os dois desenhos.**

```powershell
uv run python lab04/mesa.py desenhar paralelo
uv run python lab04/mesa.py desenhar supervisor
```

**Esperado:** no paralelo, `planejar -.-> especialista` (um nó só — o `Send`
multiplica em tempo de execução). No supervisor, dois blocos `subgraph` e a
seta `cnpj:entregar -.-> cep:consultar`: é o handoff, desenhado graças ao
`destinations=` do `add_node`. Cole em https://mermaid.live.

**2. Rode a mesa paralela.**

```powershell
uv run python lab04/mesa.py paralelo
```

**Esperado:** `plano: [('cep', '68502290'), ('ddd', '94'), ('banco', '341')]`,
três linhas `[cep ... ms]`, `[ddd ... ms]`, `[banco ... ms]` e um parágrafo com
os três dados. *(2 chamadas de LLM: planejar e relatar.)* O `tempo real da mesa`
vai ser de vários segundos — é o LLM. Rode de novo com `--sem-llm`:
**Esperado:** `especialistas somados: ~800 ms | tempo real da mesa: ~500 ms`.
O tempo real é **menor que a soma** porque os três rodaram no mesmo passo, em
threads. Essa é a prova do paralelismo.

**3. Rode a mesa com supervisor.**

```powershell
uv run python lab04/mesa.py supervisor
```

**Esperado:** seis linhas de stream, nesta ordem:

```
[pai ] supervisor valor=00000000000191
[cnpj] consultar  consultou: CNPJ 00000000000191: BANCO DO BRASIL SA...
[pai ] cnpj       entregou ao pai + handoff valor=70040912
[cep ] consultar  consultou: CEP 70040912: SAUN Quadra 5...
[pai ] cep        entregou ao pai
[pai ] supervisor resposta pronta
```

A coluna da esquerda é o **namespace**: `pai` é o grafo de cima, `cnpj` e
`cep` são os subgrafos. O supervisor rodou **duas** vezes, não três: entre o
CNPJ e o CEP houve handoff. *(2 chamadas de LLM.)*

**4. Leia o handoff.** Em `sub_entregar_cnpj`, o `Command` tem três partes:
`update` (o resultado e o novo `valor`), `goto="cep"` (um nó do **pai**) e
`graph=Command.PARENT`. Sem o `graph=`, o LangGraph procuraria `cep` dentro do
subgrafo — e, como o experimento G mostra, não falharia: ignoraria em silêncio.

**5. Leia o que o subgrafo NÃO entrega.** Repare que `EspecialistaState` tem
`resultado` (singular, privado) e o pai tem `resultados` (plural, com reducer).
Quando um subgrafo sai por `Command.PARENT`, ele nunca chega ao próprio `END`,
e o LangGraph **não copia** o estado dele de volta para as chaves compartilhadas
— só o `update` do `Command` chega ao pai. Foi um bug real deste lab: com a
chave compartilhada, o supervisor via `resultados` vazio e repetia o CNPJ até
o `RemainingSteps` encerrar.

## Experimentos

Todos com `--sem-llm`: mecanismo não precisa de modelo.

**A. Tire o reducer.** Em `MesaState`, troque
`resultados: Annotated[list[Resultado], add]` por `resultados: list[Resultado]`.
Rode `paralelo --sem-llm`.
**Esperado:** `InvalidUpdateError: At key 'resultados': Can receive only one
value per step. Use an Annotated key to handle multiple values.` Três nós
escreveram a mesma chave no mesmo passo e ninguém disse como juntar. Desfaça.

**B. Peça mais que o limite.**
`paralelo --sem-llm "CEP 68502290, DDD 94, banco 341 e CNPJ 00000000000191"`.
**Esperado:** o plano tem 4 itens, mas só 3 especialistas rodam —
`MAX_FAN_OUT = 3` corta em `distribuir`. Fan-out sem teto é a forma mais rápida
de estourar cota e de abusar de uma API pública.

**C. Deixe um especialista falhar.** `paralelo --sem-llm "CEP 00000000 e DDD 94"`.
**Esperado:** `[cep ...] cep 00000000: falhou (nao encontrado ...)` e o DDD
normal. A mesa termina; o relatório menciona a falha. Agora, em `consultar`,
remova o `try/except` e rode de novo.
**Esperado:** `BrasilAPIError` derruba a execução inteira — o DDD que deu
certo é perdido. Em fan-out, erro tem que virar dado. Desfaça.

**D. Plano vazio.** `paralelo --sem-llm "me conta uma piada"`.
**Esperado:** `plano: []` e `Não encontrei CEP, CNPJ, DDD ou banco na pergunta.`
`distribuir` devolveu o nome `"relatar"` em vez de uma lista de `Send` —
aresta condicional pode devolver qualquer um dos dois.

**E. Aperte o orçamento de passos.** `supervisor --sem-llm --recursion-limit 6`.
**Esperado:** o stream inteiro acontece, mas a resposta é
`Parei por limite de passos. Tinha:` seguida dos dois resultados. Na segunda
rodada, `remaining_steps` já era `< 4`, e o supervisor encerrou **sem chamar o
LLM** — degradou com os dados que tinha. Com `--recursion-limit 4`, ele para
antes de consultar qualquer coisa.

**F. Tire a guarda.** Comente o `if state["remaining_steps"] < 4` no
`supervisor` e rode `supervisor --sem-llm --recursion-limit 3`.
**Esperado:** `GraphRecursionError: Recursion limit of 3 reached without
hitting a stop condition`. É a diferença entre encerrar e explodir. Desfaça.

**G. Quebre o handoff.** Em `sub_entregar_cnpj`, apague `graph=Command.PARENT`
do primeiro `return`. Rode `supervisor --sem-llm`.
**Esperado:** nenhum erro — e é isso que assusta. Um aviso
`wrote to unknown channel branch:to:cep, ignoring it`, depois o stream para em
`[pai ] cnpj  valor=70040912` e a resposta sai vazia. O `goto="cep"` foi
procurado **dentro do subgrafo**, não existe lá, foi ignorado; o subgrafo
terminou normalmente e copiou `valor` para o pai (é assim que chave
compartilhada volta quando o subgrafo chega ao próprio `END`); e como `cnpj` não
tem aresta de saída no pai, a execução acabou ali, sem CEP e sem supervisor.
Handoff errado não explode — some. Desfaça.

## Exercícios

**A. Terceiro especialista no supervisor.** Adicione um subgrafo `ddd` e faça
o de CEP entregar para ele (o CEP traz o estado; o DDD, não — então o handoff
precisa de uma tabela UF → DDD ou de uma rodada do supervisor). Compare o custo
das duas soluções em chamadas de LLM.

**B. Streaming custom.** Em `consultar`, emita progresso com
`get_stream_writer()({"tipo": tipo, "fase": "inicio"})` e leia com
`stream_mode=["updates", "custom"]`.
**Esperado:** as três consultas da mesa paralela aparecem começando **antes**
de qualquer uma terminar.

**C. Supervisor de verdade.** Remova o handoff (o `cnpj` sempre devolve ao
supervisor) e rode a pergunta B com LLM.
**Esperado:** 3 chamadas em vez de 2. Esse é o preço do supervisor puro: uma
rodada por decisão. Handoff é a otimização; supervisor é a auditoria.

**D. Mesa persistente.** Compile `grafo_supervisor` com o `PostgresSaver` do
lab 3 e coloque um `interrupt()` no `sub_consultar_cnpj`.
**Esperado:** o interrupt dentro do subgrafo funciona — desde que o **pai**
tenha o checkpointer. Subgrafo herda persistência de cima.

## O que isso custa

| | Paralelo (A) | Supervisor com handoff (B) | Supervisor puro (exercício C) |
|---|---|---|---|
| Chamadas de LLM | 2, fixo | 2 | 1 + número de decisões |
| Consultas dependentes | não | sim | sim |
| Auditável | plano + resultados | stream com namespace | idem, com cada decisão explícita |
| Latência | ~max(consultas) + 2 LLM | soma das consultas + 2 LLM | soma + N LLM |

Ordem de preferência: workflow determinístico > agente único > multi-agente.
Este lab só usa "multi-agente" quando o custo é o mesmo de um agente só.

## Perguntas para levar ao lab 5

- Como rodar isso como serviço, com Studio para ver o grafo executando?
- Os especialistas são síncronos. Com `async`, o `TimeoutPolicy` que o lab 2
  não pôde usar passa a funcionar — quanto muda?

## Fontes

- Graph API (`Send`, `Command`, `Command.PARENT`, `RemainingSteps`): https://docs.langchain.com/oss/python/langgraph/graph-api
- Subgrafos: https://docs.langchain.com/oss/python/langgraph/use-subgraphs
- Multi-agente (handoffs): https://docs.langchain.com/oss/python/langchain/multi-agent/handoffs
- Streaming (`subgraphs=True`, `version="v2"`): https://docs.langchain.com/oss/python/langgraph/streaming
- Erro `INVALID_CONCURRENT_GRAPH_UPDATE`: https://docs.langchain.com/oss/python/langgraph/errors/INVALID_CONCURRENT_GRAPH_UPDATE
