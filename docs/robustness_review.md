# Revisão de robustez e conferência de prompts.md

Data: 08/09/2026.

## Resultado da auditoria

Antes desta revisão, o repositório implementava as etapas 1–5. `record` mostrava
eventos no terminal, mas não gravava um histórico de execução em disco nem
relacionava as ações às telas. Os 70 testes originais passaram.

| Etapa | Situação após a revisão | Evidências principais |
| --- | --- | --- |
| 1. Conexão segura | Implementada; validação real pendente | `connector.py`, testes de enumeração e seleção explícita |
| 2. Leitura de estado | Implementada, ampliada com status bar | `screen_reader.py`, `monitor`, testes de estado e Busy |
| 3. Árvore estrutural | Implementada | `object_tree.py`, `inspect`, GetObjectTree e fallback limitado |
| 4. Fingerprints | Implementada; versão 2 inclui status no conteúdo | `fingerprint.py`, testes de identidade, conteúdo, ordem e popup |
| 5. Eventos e ações | Implementada e corrigida na limpeza/sanitização | `events.py`, `action_recorder.py`, `record` |
| 6. Estado → ação → estado | Implementada | `models.py`, `transition_recorder.py`, testes de sequência e falhas |
| 7. Persistência e consolidação | Implementada | `repository.py`, `explore`, `stats`, `export` |
| 8. Análise do mapa | Implementada | `analyzer.py`, `analyze`, relatórios JSON e Markdown |
| 9. Revisão de robustez | Revisão de código e testes realizada; homologação SAP pendente | Este documento e testes automatizados |

Os pedidos de parar entre etapas no arquivo original foram tratados como marcos
do desenvolvimento inicial. A solicitação atual de conferir e completar o
registrador autoriza integrar as camadas faltantes. Não foi criado um worker,
executor, gerador de scripts nem tomada automática de decisão.

## Problemas encontrados e correções

1. **Eventos sem histórico persistente.** `explore` mantém execuções, eventos
   normalizados e brutos sanitizados, observações e ocorrências de transições.
   Uma definição estrutural é deduplicada pelo hash; cada observação guarda seu
   conteúdo e momento. Transições equivalentes acumulam contagens entre execuções.
2. **Ausência de correlação e status bar.** Acrescentadas a tela anterior
   verificada, ações em ordem, tela posterior, janela/popup e `Text`,
   `MessageType`, `MessageId`, `MessageNumber`, `MessageAsPopup` de `wnd[0]/sbar`.
   A ausência da barra é permitida. Alterações de status mudam o hash de conteúdo.
3. **Perda de alterações no encerramento.** `Record=False` agora acontece com o
   sink ainda conectado; o message pump e a fila são drenados antes de `close()`.
   Record pré-existente continua preservado. Referências do conector ao engine,
   objeto SAP e sessões ficam vivas durante o CLI; o recorder retém sessão/sink.
4. **Leitura potencialmente inconsistente.** `SapSnapshotReader` exige Busy=False,
   janela, programa e número de tela disponíveis. Compara metadados antes/depois
   da árvore. O CLI rejeita também snapshots durante os quais chegaram eventos.
   Leitura e persistência acontecem fora dos callbacks, na thread do message pump.
5. **Várias requisições entre duas observações.** Quando a fila revela outra
   requisição antes de uma captura posterior, o histórico recebe
   `intermediate_screen_not_observed`, com destino desconhecido. Não se atribui
   a tela final a cada requisição anterior. Esses casos ficam fora das frequências.
6. **EndRequest perdido e desconexão.** Busy é consultado na verificação de vida
   mesmo depois de StartRequest. Após 30 segundos sem EndRequest, uma fotografia
   legível permite retomar, mantendo o trecho anterior como incompleto.
   Destroy ou desconexão preservam ações pendentes sem inventar um estado final.
7. **Falha de consumidor ocultada.** No modo `explore`, falhas de persistência
   interrompem a captura com erro; não são tratadas como gravação bem-sucedida.
   A limpeza do COM ocorre também quando o consumidor falha.
8. **Dados parcialmente persistidos.** SQLite usa transações, foreign keys,
   WAL, synchronous=FULL e timeout de lock de 5 segundos. Snapshot posterior,
   consolidação e ocorrência da transição são gravados juntos. Eventos são
   confirmados antes, como diário, e continuam disponíveis se faltar a transição.
   Saves repetidos do mesmo ID de observação/transição são idempotentes.
9. **Cópia bruta contornava sanitização.** Parâmetros brutos, normalizados e
   payloads descritivos agora passam pela política, inclusive valores numéricos.
   Senhas continuam redigidas. `--sanitize-config` permite regras por ID/nome/tipo
   de componente e padrões no texto, além de `redact_all_text`.
10. **Saída inspect não era JSON puro.** O prompt de seleção foi movido para
    stderr; stdout fica disponível para consumir o JSON.
11. **Ausência de análise contextual.** O relatório lista frequências por
    estado/ação, múltiplos resultados, caminhos abaixo de 5%, popups, estados
    sem saída, mensagens de erro, capturas com avisos e lacunas de correlação.

## Semântica dos registros

- `before_state` é a última fotografia verificada antes de processar o lote;
  não significa uma fotografia imediatamente antes de cada tecla.
- `complete` significa que os dois extremos foram observados e correlacionados
  pelo registrador. Não significa sucesso de negócio. Mensagens SAP E/A
  permanecem disponíveis na observação posterior.
- `reason=state_change_without_action` ou `request_without_action` indica que
  o estado foi observado sem uma ação informada pelo SAP. Nenhum comando é inferido.
- Eventos de foco/progresso são guardados no diário; `Change`, `ContextMenu`,
  `AutomationFCode` e `Hit` contam como ações. A disponibilidade depende da API.
- A identidade das ações consolidadas usa a sequência, IDs relativos, membros,
  parâmetros sanitizados e detalhes. Parâmetros diferentes podem criar caminhos
  distintos; redigi-los pode agrupá-los. A política deve ser consistente entre
  execuções comparadas.
- Observações contam capturas aceitas na inicialização e nas transições, não
  cada ciclo de polling. `new_states_last_run` conta estruturas inéditas na
  execução mais recente. O export inclui o diário completo em `actions`, com
  `is_action` para distinguir eventos auxiliares.
- Execuções sem `ended_at` são identificadas como não finalizadas. Eventos sem
  ocorrência de transição podem ser investigados pelo ID da execução e horário.

## Riscos e limitações restantes

- **SAP real não validado nesta revisão.** A tentativa somente leitura de obter
  `SAPGUI` falhou com HRESULT -2147221020; nenhuma sessão foi acessada. O código
  e os testes simulados não substituem homologação no cliente/servidor do usuário.
- **Scripts externos:** a observação passiva registra as telas que consegue ler
  e os eventos que o SAP efetivamente entrega. Não intercepta o código de outro
  processo nem garante registrar cada chamada/linha de um Python ou VBS já
  existente. Para rastreio exato por chamada, é necessária instrumentação no
  script de origem; isso não foi presumido nem implementado como executor.
- A API entrega Change em lotes e possui controles sem suporte de gravação.
  Pode haver telas rápidas não observadas, eventos ausentes e reordenação
  percebida quando o consumidor está ocupado. Não há snapshot atômico universal
  de toda a interface; mudanças de conteúdo mantendo os mesmos metadados podem
  ocorrer durante uma leitura. A comparação antes/depois reduz, mas não elimina,
  esse risco.
- A árvore cobre a janela ativa, com limites de profundidade e quantidade já
  existentes. Outras janelas são detectadas pela contagem/estado ativo. Grades,
  controles customizados e valores não expostos em Text podem exigir leitores
  específicos. Avisos e truncamentos permanecem na observação; estruturas
  parciais podem gerar hashes diferentes. O relatório destaca avisos de leitura.
- Não são capturados screenshots, vídeo, código ABAP executado no servidor,
  resultado de negócio nem conformidade com um roteiro esperado. “Verificação de
  tela” aqui significa identidade técnica/estrutural e consistência da captura.
- Chamadas COM são síncronas. Uma chamada que já bloqueou dentro do SAP não pode
  ser cancelada por este loop; Busy é uma precaução, não um timeout de RPC.
  Processar SQLite e árvores na mesma thread pode atrasar o pump sob carga.
- A configuração Record pode alterar comportamento de ajuda F4 e drag-and-drop,
  conforme a API. Nenhum modo de visualização/teste é habilitado pelo programa.
- A proteção padrão evita senhas, mas **não identifica automaticamente todos os
  CPFs, nomes, documentos ou informações comerciais**. O arquivo de exemplo é
  apenas um ponto de partida. Metadados técnicos da sessão, incluindo usuário,
  ficam no banco. Erros COM de diagnóstico podem conter texto do servidor.
- SQLite não é criptografado e cresce com o histórico. Usar arquivo em disco
  local com permissões adequadas; retenção, criptografia, rotação e migrações
  futuras não fazem parte desta implementação. WAL não deve ser usado em
  compartilhamento de rede. Locks prolongados e disco cheio encerram com erro.
- Em término abrupto do processo, eventos ainda na fila não estão garantidos em
  disco; as transações já confirmadas permanecem. Não há reconstrução de eventos
  que o SAP não entregou. Um crash pode impedir restaurar Record.

## Validação automatizada e manual

Resultado após a correção do vínculo COM: **120 testes passaram**, incluindo um fluxo
integrado simulado do CLI até SQLite, exportação e análise. A compilação com
`python -m compileall -q main.py src tests` também passou.

Execute `python -m unittest discover -s tests -v`.

A suíte inclui os 70 testes originais e cenários de ações simples/em lote,
popup aberto/fechado, erro de status, Busy, troca de tela durante traversal,
desconexão, EndRequest ausente, várias requisições no mesmo pump, erro transitório,
flush final de Change, falha do consumidor, sanitização numérica/bruta,
deduplicação, rollback, locks, reabertura do banco e análise de ramificações.
O modo `inspect` também passou a exigir uma captura estável, e o monitor preserva
a última observação válida quando o estado fica indisponível.

O README contém os comandos e o roteiro de homologação das etapas 6–9.
Os testes não executam ações no SAP nem usam dados reais.

## Antes de um futuro worker

Será necessário definir critérios de sucesso de negócio, precondições e
pós-condições, política para estados inesperados, timeouts de ações, retomada,
idempotência, tratamento de mensagens e autorização de operações. O mapa de
frequências é evidência histórica; não deve autorizar uma ação automaticamente.

## Correção posterior: AssertionError no makepy ao iniciar explore

O traceback enviado pelo usuário demonstrou que a enumeração/seleção da sessão
funcionou, mas `WithEvents` falhou em `genpy.WriteClassBody` ao gerar as classes
da biblioteca SAP. A falha aconteceu antes de configurar `session.Record`.
Essa ocorrência não é, por si só, evidência de scripting desabilitado.

Foi acrescentado `com_events.py` como fallback automático: lê a interface de
saída e seus DISPIDs na type library, cria um gateway de eventos e usa
Advise/Unadvise. A descoberta usa connection points ou a coclass correspondente
à interface da sessão quando EnumConnectionPoints não é implementado. Não há
IID fixo no código de produção, edição do pacote pywin32 ou limpeza de cache.
Interfaces ambíguas são rejeitadas. Callbacks usam despacho dinâmico para evitar
retornar à geração de classes. A mensagem de erro preserva ambas as causas
quando o fallback também falha.

Além dos testes simulados de descoberta/fallback, foi testada a entrega de um
Change através de gateways COM reais do Windows, com conversão do componente
IDispatch e desconexão por Unadvise. A descoberta também foi validada lendo a
biblioteca `sapfewse.ocx` instalada: ela expõe `ISapSessionEvents` com Change,
StartRequest, EndRequest e os demais callbacks implementados. Esse teste não
abre uma sessão SAP nem comprova a entrega de eventos pelo servidor real.

## Referências da correção

- Código instalado do pywin32: `client/genpy.py`, `server/policy.py`,
  `server/util.py` e `server/connect.py` (geração, EventHandlerPolicy e gateways).
- Biblioteca de tipos SAP instalada, lida diretamente em modo de inspeção.

- [SAP GUI Scripting API 7.70 PL08](https://help.sap.com/doc/9215986e54174174854b0af6bb14305a/770.08/en-US/sap_gui_scripting_api.pdf): GuiSession.Record, eventos Change/StartRequest/EndRequest/Destroy e GuiStatusbar.
- [pywin32: WithEvents e fechamento do sink](https://github.com/mhammond/pywin32/blob/main/com/win32com/client/__init__.py). O código e a documentação da implementação instalada foram inspecionados localmente.
