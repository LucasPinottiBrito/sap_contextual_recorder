# Evolução para automação contextual de alteração de titularidade

## Situação após implementação das correções

As correções de parâmetros, qualidade, controles, lotes/linha do tempo e
identificação de casos foram implementadas. Consulte `gravacao_por_caso.md`
para o comportamento atual e os comandos. A avaliação abaixo descreve a base
anterior; o comparador, regras e executor continuam sendo etapas futuras.
Nesta entrega cada execução pertence a um caso e um caso pode ter várias execuções.

## Parecer

O projeto faz sentido como base de coleta. A separação entre telas, observações,
eventos e transições já permite comparar execuções. Para automatizar a alteração
de titularidade, falta transformar esse histórico em regras de ação revisadas,
com parâmetros do caso, condições de aplicação e verificação do resultado.

Não é necessário prever a ordem de todas as telas. O executor pode observar o
estado após cada ação e escolher uma regra aplicável. Entretanto, tela e botão
sozinhos não determinam a decisão: o objetivo do caso e as etapas já concluídas
também precisam participar. Uma mesma tela pode reaparecer para corrigir um erro,
confirmar dados ou iniciar outra operação.

Este documento é uma avaliação e proposta. Não implementa regras de negócio,
não altera o banco e não executa ações no SAP.

## Evidências verificadas

Consulta somente leitura a `artifacts/exploration.sqlite3`, código local e execução
de `venv/Scripts/python.exe -m unittest discover -s tests -q`: 120 testes passaram.
Esses testes usam objetos simulados; não validam a alteração de titularidade no SAP.

| Item | Resultado na amostra |
| --- | --- |
| Tentativas de gravação | 3, das quais 2 encerraram com erro sem observações |
| Execução com observações | Cerca de 39 segundos, em 08/09/2026 |
| Telas estruturais / observações | 8 / 11 |
| Eventos / eventos de ação | 50 / 15 |
| Ocorrências de transição | 11: 6 completas com ações, 4 completas sem ação e 1 incompleta |
| Definições consolidadas de transição | 9 |
| Observações com árvore truncada | 2, ambas do Backoffice |
| Propriedades Enabled e Visible | Nulas nas 1.568 ocorrências de componentes |

O fluxo observado inclui entrada em CIC0, seleção de organização, Backoffice,
Data Finder, Acerto Cadastral e Cockpit do Atendente BT. Não há identificação
de casos de titularidade nem marcação de conclusão de negócio. Esta amostra
não demonstra uma alteração completa e confirmada de titularidade.

## O que preservar

- SQLite como diário e armazenamento local; não há motivo demonstrado para migrar.
- Conexão e seleção explícita da sessão SAP.
- Eventos copiados para uma fila e processamento fora dos callbacks COM.
- Capturas que consultam Busy e rejeitam mudanças técnicas durante a leitura.
- Separação entre identidade estrutural e conteúdo observado.
- Persistência dos eventos, mesmo quando falta uma tela intermediária.
- Indicação de transições incompletas, sem inventar comandos.
- Gravações históricas como evidência imutável; agrupamentos e regras como dados derivados.

## Correções prioritárias no gravador

### 1. Separar parâmetros executáveis de textos de exibição

`events.normalize_command_array` aplica aos parâmetros o mesmo sanitizador
destinado a resumir textos da interface. Confirmei com entradas sintéticas:

| Entrada | Parâmetro persistível produzido hoje |
| --- | --- |
| String vazia | `None` |
| Texto com espaços nas extremidades | Texto sem esses espaços |
| Texto com quebra de linha | Texto com espaço no lugar da quebra |
| Texto com 250 caracteres | Texto cortado em 200 caracteres, incluindo reticências |

Isso também modifica `raw_commands`. No banco há um `sapEvent` com um parâmetro
terminando em reticências. Não é possível recuperar o conteúdo perdido pelo banco.

Proposta: preservar tipo, string vazia, espaços, quebras e comprimento dos
parâmetros não redigidos; fazer resumo apenas na exibição. A redação de segredos
continua sendo uma política separada. Todo parâmetro redigido ou cortado precisa
de um indicador explícito e de substituição por entrada do caso antes da execução.
Registrar versão do capturador e da política. Dados antigos mantêm qualidade
desconhecida quando não houver prova de integridade.

### 2. Representar a entrega em lote de comandos

Em `SapTransitionRecorder`, `before_state` é a última observação aceita. O SAP
pode entregar comandos de mudanças locais apenas antes de uma comunicação com
o servidor. A amostra mostra a passagem do Data Finder de `f30831df9c` para
`24af8d127d` sem ação registrada; depois, o lote para a pesquisa contém `select`
da aba, preenchimento e `press`, quando a aba já aparece na observação anterior.

Não converter uma linha de `transitions` automaticamente em um passo reproduzível.
Guardar ordem explícita dos eventos e capturas por execução, IDs de lotes/requisições,
horário de recebimento e associação com o intervalo de observações. Distinguir:

- tela observada antes do recebimento do lote;
- ações entregues pelo SAP, na ordem recebida;
- mudanças locais observadas durante o intervalo;
- resultado observado depois da requisição;
- contexto anterior exato desconhecido.

Aumentar o polling pode melhorar a cobertura, mas não elimina essa limitação.
Para scripts de origem disponíveis, um invólucro que registra antes/depois de
cada chamada oferece evidência mais precisa. Para uso manual, preservar o lote
original e revisar a reconstrução sem inventar um instante para cada comando.

### 3. Ler o estado específico dos controles necessários

`SapUiNode` guarda propriedades genéricas. Acrescentar leitores por tipo, conforme
a API efetivamente disponível no cliente:

- checkbox/radio: seleção;
- combo: chave selecionada e opções relevantes;
- abas: seleção ativa;
- grids/tabelas: colunas, linhas relevantes, chave do registro, célula e seleção;
- árvores: chaves/textos dos nós relevantes e seleção;
- controles HTML/TextEdit: conteúdo ou contexto acessível necessário à regra.

Capturar por escopo e orçamento de leitura, registrando o que ficou fora. A leitura
de toda uma tabela não deve atrasar a captura de telas intermediárias. Identificar
registros por chave de negócio, e não fixar a posição da linha observada.

Não interpretar `Enabled=None` ou `Visible=None` como verdadeiro ou falso.
Uma regra deve exigir apenas propriedades que o leitor sabe obter, ou tratar
como desconhecida a condição que depende de uma propriedade indisponível.

### 4. Tornar a completude verificável

Permitir configuração de profundidade, quantidade de nós e escopos; a profundidade
12 cortou duas árvores nesta amostra. Acrescentar indicadores por campo/controle:
capturado, indisponível, omitido, truncado e redigido. Persistir também falhas de
normalização de eventos que hoje podem ficar apenas no log.

Manter a janela ativa como prioridade e, quando acessível, contexto das demais
janelas. Ao usar a última captura da janela principal sob um popup, indicar sua
idade: ela é evidência anterior, não uma leitura simultânea garantida.

Capturas visuais opcionais ajudam a revisão humana de telas difíceis, mas não
substituem propriedades técnicas ausentes nem recuperam detalhes de casos antigos.

### 5. Identificar casos, entradas e resultado

Separar uma sessão de gravação (`run`) de um caso de alteração de titularidade.
Uma sessão pode conter vários casos e um caso pode ter várias tentativas.
Registrar início/fim do caso, entradas parametrizadas, marcos, observações do
operador e resultado: confirmado, falhou, cancelado ou inconclusivo.

Instalação, novo titular e data de vigência são exemplos de entradas a confirmar
com o processo real. Não inferir conclusão apenas pelo fechamento de uma janela
ou por uma mensagem genérica de sucesso. Definir a evidência de que a instalação
ficou vinculada ao titular e vigência solicitados.

## Como consolidar várias gravações

O analisador atual agrupa por hash estrutural e sequência exata, incluindo valores
dos parâmetros. Duas pesquisas com instalações diferentes viram ações distintas.
O Backoffice também tem dois hashes na amostra, com diferenças na subárvore de
resultados. Ambos são candidatos a uma família de telas; isso não prova equivalência
de contexto ou autoriza aplicar a mesma ação.

Proposta de processamento offline:

1. Segmentar casos e separar observações de negócio confirmadas das inconclusivas.
2. Gerar candidatos a famílias usando programa, tela, papel da janela e controles
   técnicos relevantes. Restringir por sistema/cliente/processo quando necessário.
3. Mostrar diferenças: controles adicionados/removidos, valores alterados, seleção,
   mensagens e ações posteriores. Ausência de captura não significa ausência na tela.
4. Parametrizar ações usando as entradas conhecidas do caso. Classificar valores
   como constantes, entradas, valores extraídos ou expressões aprovadas; manter a
   proveniência do evento original. Não substituir números indiscriminadamente.
5. Sugerir passos curtos e pontos de decisão. Preservar ordem dentro de cada passo;
   reavaliar contexto após navegação, requisição ou mudança local relevante.
6. Apresentar conflitos: contextos aparentemente iguais com ações diferentes.
   Pedir a condição de negócio ausente, sem escolher apenas pela maioria.
7. Criar regras candidatas e validá-las com exemplos positivos e negativos.

Não descartar `setFocus` ou teclas automaticamente: na seleção de organização
gravada, foco em um label e `sendVKey` participam da sequência. Redimensionamento
e ajustes visuais podem ser candidatos a exclusão, mediante verificação.

O sistema faz agrupamento e comparação. A revisão humana deve se concentrar no
significado das diferenças e na decisão de negócio, sem exigir leitura de JSON.

## Arquitetura proposta

```text
gravação -> casos e evidências -> comparação -> regras candidatas -> regras validadas
                                                                       |
SAP atual -> contexto observado + dados do caso + progresso -> selecionar regra
                                                          -> executar passo curto
                                                          -> verificar resultado
                                                          -> atualizar progresso
                                                          -> observar novamente
```

Regra = identidade + condições + ação parametrizada + resultados possíveis +
evidência de sucesso + política de repetição + exemplos de origem.

O executor não precisa de uma lista global de telas em ordem fixa. Passos locais
continuam ordenados; a navegação entre passos é guiada pelo que aparece e pelo
progresso do caso. Popups podem ter regras condicionais reutilizadas em mais de
uma etapa. Se a informação que distingue duas respostas não estiver observável,
ela precisa vir de outra fonte ou de uma decisão humana.

Uma regra pode aceitar vários resultados conhecidos. Cada resultado deve gerar
nova avaliação; nunca assumir uma única próxima tela apenas porque foi a mais comum.

Exemplo conceitual, sem afirmar que esse popup existe no fluxo gravado:

```yaml
id: confirmar_dados_titularidade
processo: alteracao_titularidade
quando:
  tela: confirmacao_titularidade_validada
  instalacao: corresponde_ao_caso
  novo_titular: corresponde_ao_caso
  dados_obrigatorios: conferidos
  conclusao: ainda_nao_confirmada
acao:
  passo: confirmar_dados
resultados:
  - titularidade_confirmada
  - validacao_adicional_conhecida
  - erro_de_negocio_conhecido
sem_resultado_confirmado: reconciliar_antes_de_repetir
```

Resolver controles por ID técnico relativo e contexto. Quando necessário, usar
nome/tipo/ancestrais e chave do registro; exigir correspondência única. Não remover
todos os índices dos IDs nem clicar por semelhança textual isolada.

Executar apenas operações estruturadas suportadas pelo executor. O texto recebido
do SAP ou presente no banco não deve ser avaliado como código Python. Preservar
sequência e tipos, resolver os controles novamente após mudanças e respeitar Busy
e condições de prontidão da tela, com prazo e registro de resultado.

Para retomada, registrar regra/versão, tentativa, evidência anterior e posterior.
Antes de repetir uma ação que confirma uma alteração, consultar se o efeito já
ocorreu. Registro local e confirmação no SAP não são uma transação atômica: uma
queda entre ambos exige reconciliação. Impor limites de repetição e de falta de
progresso. Zero regras ou regras conflitantes levam à coleta de evidência e
intervenção; essa intervenção alimenta um novo exemplo para revisão posterior.

## Evolução do SQLite

Preservar as seis tabelas atuais. Introduzir migrações versionadas e dados derivados
recalculáveis; não reescrever hashes antigos para caber no novo agrupamento.

| Nova entidade proposta | Responsabilidade |
| --- | --- |
| `cases` e associação caso/execução | Entradas, objetivo, tentativas e resultado |
| `request_batches` | Ordem e associação temporal de comandos e observações |
| `screen_families` e membros | Agrupamentos revisados, critérios e versão |
| `rules` | Condições, operações, parâmetros, pós-condições e estado de revisão |
| `rule_examples` | Evidências positivas/negativas e observações de origem |
| `executions` / `execution_steps` | Progresso, decisão, tentativas e resultados |

No primeiro incremento, implementar somente os campos e tabelas necessários à
qualidade e identificação dos casos. O restante acompanha o desenvolvimento do
comparador e do executor.

## Plano de entrega e critérios de aceitação

| Etapa | Entrega | Critério de aceitação |
| --- | --- | --- |
| 1. Fidelidade | Parâmetros preservados, marcadores de perda, captura por controle e ordenação/lotes | Campos vazios, textos longos/multilinha, seleções e grids relevantes têm testes; lacunas ficam explícitas; lote de aba não é tratado como clique instantâneo |
| 2. Gravação por caso | Início/fim, entradas, resultado e marcos | Um caso completo pode ser reconstruído e seu resultado comprovado |
| 3. Comparador | Linha do tempo, famílias, diferenças e candidatos a parâmetros | Vários casos comparáveis mostram trechos comuns, variações e conflitos com referências à origem |
| 4. Regras e simulação | Regras revisadas e avaliação offline | Contextos positivos/negativos são separados; ambiguidades e desconhecidos são reportados; nenhum acesso de escrita ao SAP |
| 5. Modo de acompanhamento | Propostas durante operação manual | Registrar regra sugerida, decisão real e divergências em casos novos |
| 6. Execução contextual | Passos validados com verificação e retomada | Casos novos concluídos com evidência de negócio; exceções/queda/duplicação testadas |

Após corrigir o gravador, usar um caso completo como referência inicial; depois,
registrar, por exemplo, 5–10 casos variados para descoberta. Esse número é apenas
um ponto de partida, não um limiar de segurança ou cobertura. Preferir casos que
tragam condições diferentes e reservar casos inteiros posteriores para validação.
Não dividir observações do mesmo caso entre descoberta e validação.

Medir: condições de decisão capturadas, casos confirmados, telas desconhecidas,
conflitos de regras, ações sugeridas incorretamente, intervenções e progresso
confirmado. Simulação histórica verifica o reconhecimento e a seleção da ação;
não comprova o efeito de executar essa ação no SAP.

## Alternativas e limites

- **Gravador nativo do SAP GUI:** usar como referência complementar de comandos
  em cenários controlados e comparar com o coletor, inclusive quanto à coexistência
  dos gravadores. Não resolve sozinho o reconhecimento contextual nem o estado
  inicial; verificar disponibilidade no cliente utilizado.
- **Instrumentação de scripts existentes:** se houver código fonte disponível,
  registrar cada chamada e suas condições antes/depois é uma alternativa melhor
  à tentativa de inferir chamadas de outro processo.
- **Integração de negócio no backend:** investigar com a equipe SAP se há uma
  interface aprovada que execute o processo completo com suas validações. O banco
  contém programas personalizados; não há evidência suficiente para indicar uma
  BAPI/OData específica que substitua este fluxo.
- **Ferramenta de RPA:** pode fornecer execução e edição visual, mas ainda exige
  as condições e resultados de negócio. Não há necessidade demonstrada de trocar
  a base Python/SQLite antes de validar um fluxo.
- **IA na análise:** útil para sugerir famílias, explicar diferenças e rascunhar
  regras a partir de evidências. A validação precisa distinguir hipótese de fato.
  IA não recupera dados que não foram gravados nem determina por frequência qual
  alteração de negócio deve ser feita.

O limite de viabilidade aparece quando duas situações exigem ações diferentes,
mas todas as informações disponíveis ao executor são iguais. Nesse caso,
acrescentar captura/consulta/entrada, ou manter esse ponto assistido. Não é
necessário abandonar a automação dos demais passos.

## Fontes e pontos de implementação

- `src/sap_explorer/events.py`: normalização, cópia dos parâmetros e tratamento
  de falhas nos callbacks.
- `src/sap_explorer/object_tree.py`: propriedades, sanitização e limites da árvore.
- `src/sap_explorer/transition_recorder.py`: associação da última observação aos lotes.
- `src/sap_explorer/fingerprint.py`: hash exato da estrutura e do conteúdo.
- `src/sap_explorer/repository.py`: assinatura exata das ações e modelo SQLite.
- `src/sap_explorer/analyzer.py`: frequências e resultados por sequência exata.
- [SAP GUI Scripting API 8.00 PL4](https://help.sap.com/doc/9215986e54174174854b0af6bb14305a/800.04/en-US/sap_gui_scripting_api.pdf): evento Change, página impressa 200, explica entrega em lote, preservação da ordem e ausência de valores pré-preenchidos não alterados; GuiGridView, página impressa 122, documenta GetCellValue. Conferir capacidades na versão instalada.

Próximo incremento recomendado: corrigir fidelidade dos parâmetros e qualidade
da captura, adicionar identificação de caso, e então construir o comparador.
Assim as próximas gravações já serão úteis à criação do executor contextual.
