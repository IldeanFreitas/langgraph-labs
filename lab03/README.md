# Lab 3 — Persistência, aprovação humana e viagem no tempo

**Tempo:** ~1h · **Custo:** 3 chamadas de LLM no passo a passo (0 com `--sem-llm`) · **Pré-requisito:** Postgres no ar

## O que você vai entender

| Conceito | Onde aparece |
|---|---|
| `PostgresSaver` + `thread_id` | um checkpoint por passo, gravado no banco |
| `durability` | `sync`, `async` ou `exit`: quando o checkpoint é gravado |
| `interrupt()` + `Command(resume=)` | o grafo para, o processo morre, outro processo continua |
| `get_state` / `get_state_history` | inspecionar a thread: valores, próximo nó, interrupts pendentes |
| `update_state` + `checkpoint_id` | voltar no tempo e seguir por outro caminho (fork) |
| `PostgresStore` + `runtime.store` | memória do usuário que atravessa threads |
| `RetryPolicy(retry_on=...)` | repetir só o erro que vale a pena repetir (o 429 do free tier) |

## A ideia

No lab 2 o estado morria com o processo. Aqui cada passo do grafo vira uma
linha no Postgres, e isso muda o que dá para fazer: parar no meio e esperar um
humano, voltar a um ponto do passado, lembrar do usuário em outra conversa.

Por isso este lab **não é um script que roda de uma vez**. É uma CLI com
subcomandos, e cada subcomando é um processo Python separado. Se o segundo
comando funciona, é porque o primeiro deixou o estado no banco — essa é a prova.

```
START -> classificar (LLM) -> cep -----------------------> formatar -> END
                           \-> aprovar (interrupt) -> cnpj -/    ^
                           \-> desconhecido -> END               |
                                                    Store: (usuário, "consultas")
```

O grafo é o do lab 2, enxugado para CEP e CNPJ, com duas novidades: o nó
`aprovar` antes do CNPJ (dado de terceiro exige alguém dizer "sim") e o
`formatar` lendo e escrevendo no Store.

## Passo a passo

Use `t1`, `t2`, `t3` como nomes de thread. Se já rodou antes, troque os nomes
ou limpe o banco (`scripts/docker-reset.ps1`) — thread é memória, ela acumula.

**1. Suba o Postgres.**

```powershell
docker compose up -d
docker compose ps
```

**Esperado:** `langgraph-labs-pg` com status `healthy`.

**2. Veja o desenho.**

```powershell
uv run python lab03/persistente.py desenhar
```

**Esperado:** Mermaid com `classificar` saindo para `cep`, `aprovar` e
`desconhecido`; `aprovar` com duas saídas pontilhadas (`cnpj` e `__end__`).
Cole em https://mermaid.live.

**3. Pergunte e deixe o processo morrer.**

```powershell
uv run python lab03/persistente.py perguntar t1 "Qual o endereço do CEP 68502290?"
```

**Esperado:** `Freitas, aqui está: Rua Transamazônica, Marabá/PA` e o caminho
`classificado como cep -> consultou cep -> formatado`. O processo termina.
*(1 chamada de LLM.)*

**4. Abra o que ficou no banco — em outro processo.**

```powershell
uv run python lab03/persistente.py estado t1
```

**Esperado:** `checkpoint_id`, `próximos nós: (nenhum - terminou)`, e em
`valores` a pergunta, o tipo, o valor, o resultado e o log inteiro. Nada disso
está em memória: veio do Postgres.

**5. Peça algo que exige aprovação.**

```powershell
uv run python lab03/persistente.py perguntar t2 "Quem é o CNPJ 00000000000191?"
```

**Esperado:** `PAUSADO: {'pergunta': 'Consultar este CNPJ na Receita? ...', 'cnpj': '00000000000191'}`
e o processo termina **sem** consultar a BrasilAPI. *(1 chamada de LLM.)*

**6. Confira que a pausa é durável.**

```powershell
uv run python lab03/persistente.py estado t2
```

**Esperado:** `próximos nós: ('aprovar',)` e `interrupts:` com o payload do
passo 5. Feche o terminal, abra outro, rode de novo: mesma coisa. O grafo
espera o tempo que for.

**7. Aprove — de outro processo.**

```powershell
uv run python lab03/persistente.py retomar t2 sim
```

**Esperado:** `BANCO DO BRASIL SA - Bancos múltiplos, com carteira comercial - BRASILIA/DF`
com o caminho `classificado como cnpj -> aprovado por humano -> consultou cnpj -> formatado`.
Repare: `classificar` **não rodou de novo** — a classificação veio do
checkpoint. *(0 chamadas de LLM.)*

**8. Liste os checkpoints da thread 1.**

```powershell
uv run python lab03/persistente.py historico t1
```

**Esperado:** 5 checkpoints, do mais novo ao mais antigo, passos `3, 2, 1, 0, -1`.
Cada um diz qual é o **próximo** nó a rodar e como estava o `log` naquele
instante. Copie o `id` do checkpoint com `próximo=cep` (passo 1).

**9. Volte a esse ponto e troque o CEP.**

```powershell
uv run python lab03/persistente.py voltar t1 <id-do-passo-1> --valor 01310100
```

**Esperado:** `Avenida Paulista, São Paulo/SP` com o caminho
`classificado como cep -> valor trocado para 01310100 -> consultou cep -> formatado`.
Só `cep` e `formatar` rodaram; `classificar` não, porque veio antes do
checkpoint escolhido. *(0 chamadas de LLM.)*

**10. Veja que nada foi apagado.**

```powershell
uv run python lab03/persistente.py historico t1
```

**Esperado:** 8 checkpoints. Os 5 originais continuam lá; os 3 novos (passos
`2, 3, 4`) descendem do passo 1. `update_state` não desfaz — ele **cria um
ramo**. A thread virou uma árvore.

**11. Repita a pergunta do passo 3, em outra thread.**

```powershell
uv run python lab03/persistente.py perguntar t3 "Qual o endereço do CEP 68502290?"
```

**Esperado:** a mesma resposta, mais o sufixo `(já consultado 1x antes)`.
A thread `t3` nunca viu esse CEP; quem viu foi o **usuário** Freitas, e isso
mora no Store, não no checkpoint. *(1 chamada de LLM.)*

**12. Abra a memória do usuário.**

```powershell
uv run python lab03/persistente.py memoria
uv run python lab03/persistente.py memoria --usuario Ana
```

**Esperado:** três itens para Freitas (`cep:68502290` com `2x`, `cep:01310100`,
`cnpj:00000000000191`) e zero para Ana. O namespace é `(usuário, "consultas")`:
cada um só enxerga o seu.

## Experimentos

Todos funcionam com `--sem-llm` — não gaste cota para ver mecanismo.

**A. Recuse.** `perguntar t4 "CNPJ 33000167000101"` e depois `retomar t4 nao`.
**Esperado:** `Consulta de CNPJ recusada pelo aprovador.`, caminho
`classificado como cnpj -> recusado por humano`. O nó `cnpj` nunca rodou —
o `Command(goto=END)` dentro de `aprovar` desviou dele.

**B. Prove que contexto não é persistido.** Em `cmd_retomar`, apague a linha
`context=Context(...)`. Pergunte um CNPJ numa thread nova e retome.
**Esperado:** `AttributeError: 'NoneType' object has no attribute 'usuario'`
dentro de `formatar`. O estado voltou do banco; o contexto, não. Toda
invocação — inclusive a que retoma — precisa passar o seu. Desfaça.

**C. Troque a durabilidade.** `perguntar t5 "CEP 01310100" --durability exit`
e depois `historico t5`.
**Esperado:** **1** checkpoint, contra 5 com `sync`. Com `exit` o LangGraph só
grava ao terminar — é mais rápido, mas não há para onde voltar no tempo nem de
onde retomar se o processo cair no meio. Agora
`perguntar t6 "CNPJ 33000167000101" --durability exit` e `estado t6`.
**Esperado:** o interrupt **foi** gravado (`próximos nós: ('aprovar',)`).
Pausar conta como "sair", então a aprovação funciona em qualquer modo.

**D. Efeito colateral antes do `interrupt()`.** Em `aprovar`, coloque
`print("enviando e-mail de aviso...")` **antes** do `interrupt(...)`. Pergunte
um CNPJ e retome.
**Esperado:** o print aparece duas vezes — uma no `perguntar`, outra no
`retomar`. Ao retomar, o nó roda inteiro de novo desde a primeira linha; o
`interrupt()` só deixa de pausar porque agora tem o valor do `resume`. Se aquilo
fosse um e-mail de verdade, teria ido duas vezes. Desfaça.

**E. Replay também repete efeitos.** Rode `voltar t1 <id-do-passo-1>` **sem**
`--valor`.
**Esperado:** mesma resposta do passo 3, mas com `(já consultado Nx antes)` e
o contador do Store subiu. Voltar no tempo re-executa os nós depois do
checkpoint — inclusive o `store.put` do `formatar`. Time travel é para
depurar e explorar; efeitos colaterais nesses nós precisam ser idempotentes.

**F. Tire o checkpointer.** Em `abrir()`, troque
`builder.compile(checkpointer=checkpointer, store=store)` por
`builder.compile(store=store)`. Pergunte um CNPJ e retome.
**Esperado:** o `perguntar` ainda devolve `PAUSADO` (o `interrupt()` em si não
exige checkpointer), mas o `retomar` falha com
`RuntimeError: Cannot use Command(resume=...) without checkpointer`. Sem
checkpoint não há de onde continuar. Desfaça.

**G. Recupere de uma falha no meio.** `perguntar t7 "CEP 00000000" --sem-llm`.
**Esperado:** `BrasilAPIError: nao encontrado: /cep/v2/00000000` — o nó `cep`
explodiu e o processo morreu. Agora `estado t7`.
**Esperado:** `próximos nós: ('cep',)`. O checkpoint de antes da falha está lá.
Rode `voltar t7` (sem id, sem valor).
**Esperado:** falha igual — continuar do último checkpoint refaz a mesma
tarefa com o mesmo dado. Rode `voltar t7 --valor 01310100`.
**Esperado:** `Avenida Paulista, São Paulo/SP`, com `classificar` intocado.
É o mesmo mecanismo que salva uma execução derrubada por 429, queda de rede ou
`Ctrl+C`: `invoke(None, config)` continua de onde o último checkpoint parou.

## Exercícios

**A. Aprovação com correção.** Faça o humano poder consertar o CNPJ: retome com
`Command(resume={"aprovado": True, "cnpj": "33000167000101"})` e, em `aprovar`,
use o dicionário para atualizar `valor` antes de ir para `cnpj`.
**Esperado:** a consulta sai com o CNPJ corrigido, e o `log` registra a troca.

**B. Pausa estática.** Remova o nó `aprovar`, ligue `classificar` direto em
`cnpj` no mapa do `add_conditional_edges`, e compile com
`interrupt_before=["cnpj"]`. Retome com `invoke(None, config)`.
**Esperado:** mesmo comportamento de pausa, sem código de `interrupt()` no nó.
Compare: `interrupt_before` é bom para depurar; `interrupt()` é para quando a
pausa depende de dados (payload, condição, valor de retorno).

**C. Preferência no Store.** Grave `("Freitas", "preferencias")` → `{"formato": "curto"}`
via `store.put` num subcomando novo, e faça `formatar` ler isso e omitir o
prefixo `Freitas, aqui está:` quando `formato == "curto"`.
**Esperado:** o mesmo grafo muda de comportamento por usuário, sem mudar o
estado da thread.

**D. Segundo usuário.** Rode o passo 3 com `--usuario Ana` numa thread nova.
**Esperado:** Ana não recebe `(já consultado ...)` — o namespace isola — e
`memoria --usuario Ana` passa a ter 1 item.

## O que isso custa

| | `InMemorySaver` (labs 1–2) | `PostgresSaver` (lab 3) |
|---|---|---|
| Sobrevive ao fim do processo | não | sim |
| Aprovação humana entre processos | não | sim |
| Time travel | só na mesma execução | qualquer hora |
| Custo por passo | zero | 1 gravação (`sync`) ou 1 ao final (`exit`) |
| Infra | nenhuma | um Postgres |

O `durability` é o dial: `sync` grava antes de seguir (mais seguro, mais lento),
`async` grava enquanto o próximo nó roda (padrão), `exit` grava só no fim.
Escolha por nó crítico, não por gosto.

Chamadas de LLM no passo a passo: 3 — só nas perguntas novas. Retomar e voltar
no tempo custam zero, porque a classificação já está no checkpoint. É outro
motivo para separar "decidir" (LLM) de "executar" (Python): o que é
determinístico pode ser refeito de graça.

## Sobre a cota do Gemini

O free tier do `gemini-3.8-flash` reporta `GenerateRequestsPerDayPerProjectPerModel-FreeTier = 20`
e devolve `429` com `retryDelay` de ~17 s quando você faz rajadas. O
`RetryPolicy` do nó `classificar` espera 20 s antes de repetir, e só repete
`ModelRateLimitError` — erro da BrasilAPI não é repetido, porque 404 não melhora
tentando de novo. Se a cota acabar mesmo, você tem duas saídas: `--sem-llm`
classifica por regex e o resto do lab segue igual; ou, como a cota é **por
modelo**, `$env:LAB_MODEL = "google_genai:gemini-3.7-flash"` abre um balde novo
sem mudar uma linha de código — é o motivo de `get_model()` ser o único ponto
que conhece o provedor.

## Perguntas para levar ao lab 4

- E se a pergunta pedisse CEP **e** CNPJ ao mesmo tempo? Hoje o grafo escolhe um.
- Com três especialistas e uma pergunta ambígua, quem decide a ordem?

## Fontes

- Checkpointers: https://docs.langchain.com/oss/python/langgraph/checkpointers
- Interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- Time travel: https://docs.langchain.com/oss/python/langgraph/use-time-travel
- Stores: https://docs.langchain.com/oss/python/langgraph/stores
- Memória: https://docs.langchain.com/oss/python/langgraph/memory
- Referência `Runtime`: https://reference.langchain.com/python/langgraph/runtime/
