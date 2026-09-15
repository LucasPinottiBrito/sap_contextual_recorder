# SAP Explorer — monitoramento e histórico de sessões SAP GUI

Projeto de exploração contextual do SAP GUI. O programa localiza o SAP GUI
Scripting Engine, permite selecionar explicitamente uma sessão e pode descrever
e monitorar o estado técnico da tela atual, inspecionar sua estrutura e ouvir
os eventos expostos pela sessão. O modo `explore` combina essas informações em
um histórico persistente de telas, ações e transições, com análise de caminhos.

O gravador gera um roteiro contextual com prévias Python dos comandos capturados.
Essas prévias precisam de revisão; o projeto não reproduz ações no SAP.

## Gravador com interface gráfica

```powershell
python main.py
# equivalente:
python main.py record
```

1. Selecione explicitamente a sessão na janela (sistema, cliente, usuário e transação).
2. Informe um nome e clique em **Iniciar gravação**.
3. Use o SAP. A janela acompanha telas, popups, comandos e mensagens. Selecione
   uma ocorrência para consultar seus detalhes completos.
4. Opcionalmente, registre uma etapa e o resultado esperado na tela exibida.
5. Clique em **Finalizar e gerar arquivos**. Fechar a janela também solicita
   encerramento e aguarda a gravação dos arquivos.

São gerados automaticamente em `artifacts/`:

- `recording_<run-id>.json`: arquivo completo para a futura geração do script contextual;
- `recording_<run-id>.md`: roteiro legível com passos gerais, ações e resultados;
- `exploration.sqlite3`: diário persistido durante a captura, compartilhado entre execuções.

Cada roteiro contém somente sua execução. O JSON inclui árvores e valores dos
controles, IDs, comandos e parâmetros tipados, encadeamentos, sequência de captura,
contexto anterior/posterior, mensagens SAP e anotações. Mensagens observadas são
candidatas a resultado esperado e exigem revisão; captura completa não confirma
sucesso funcional. O modo gráfico preserva textos completos, respeitando a política
de sanitização, inclusive nas mensagens de popup e barra de status.

```powershell
python main.py record --interval 0.5 --artifacts artifacts/gravacoes --grid-rows 50 --grid-columns 30
python main.py record --sanitize-config config/sanitization.example.json
# Recuperar/regenerar o roteiro pelo ID da execução, sem SAP:
python main.py context --run-id ID_DA_EXECUCAO --artifacts artifacts/gravacoes
```

O histórico completo fica no banco; a janela mantém as últimas 1.500 ocorrências.
Os limites de captura são configuráveis, e controles não lidos permanecem marcados
como parciais. Consulte [o guia do gravador contextual](docs/gravador_contextual.md)
para detalhes do formato, referências oficiais e roteiro de homologação.

## Gravação por caso e correções de fidelidade

A captura agora preserva parâmetros integrais, registra seleções e amostras de
controles, explicita perdas e reúne eventos em lotes com uma linha do tempo.
O banco antigo recebe migração aditiva com backup automático; dados perdidos
nas gravações anteriores não são reconstruídos.

Consulte [o guia de gravação por caso](docs/gravacao_por_caso.md) para configurar
entradas, registrar marcos e informar o resultado da alteração de titularidade.

```powershell
python main.py explore --case-file config/case.local.json
python main.py cases
python main.py case-mark --case-id ID_DO_CASO --label "Dados conferidos"
python main.py case-finish --case-id ID_DO_CASO --outcome confirmed --evidence "Resultado conferido no SAP"
```

Crie `config/case.local.json` a partir de `config/case.example.json`, preenchendo
os dados reais do caso. Encerre a gravação antes de usar `case-finish`.
Cada execução pertence a um caso; CTRL+C não confirma sucesso de negócio.

## Gravar uma exploração completa

Com o SAP já aberto e autenticado:

```powershell
python main.py explore
```

Selecione a sessão listada e use o SAP. O terminal mostra fingerprints,
transação/programa/tela, ações e mensagens da barra de status. `CTRL+C` encerra,
grava alterações finais entregues pelo SAP e remove o listener. O histórico é
salvo em `artifacts/exploration.sqlite3`, preservado entre execuções.

```powershell
python main.py stats
python main.py export
python main.py analyze
```

Esses três comandos funcionam sem o SAP aberto. O export gera
`artifacts/exploration.json`, com definições de telas, observações detalhadas,
diário de eventos, transições consolidadas e histórico por execução. A análise
gera `artifacts/exploration_report.json` e `artifacts/exploration_report.md`, com
frequências, ramificações, caminhos raros, popups e pontos de atenção.

Opções disponíveis:

```powershell
python main.py explore --db artifacts/teste.sqlite3 --interval 0.5 --sanitize-config config/sanitization.example.json
python main.py stats --db artifacts/teste.sqlite3
python main.py export --db artifacts/teste.sqlite3 --output artifacts/teste.json
python main.py analyze --db artifacts/teste.sqlite3 --artifacts artifacts/teste
python main.py --help
```

`--session-index N` permite escolher explicitamente o índice da listagem sem
prompt. Sem essa opção a seleção é interativa, mesmo com uma única sessão.
`--interval` controla o polling complementar de estados locais; a correlação
das requisições usa eventos e Busy, sem uma espera fixa presumida para o SAP.

`record` abre a interface gráfica. `record-cli` mostra eventos no terminal.
`explore` grava pelo terminal e também gera o roteiro contextual ao encerrar.

### O que significa verificar telas

O registrador verifica transação, programa, número da tela, janela ativa,
popups, estrutura dos componentes e status bar. Compara os identificadores
antes/depois da captura e relaciona a última observação aceita às ações recebidas
e à observação posterior. Não faz validação contra um roteiro esperado, não
captura screenshots e não decide se uma operação de negócio foi bem-sucedida.

Quando a tela intermediária não foi observada, a transição fica incompleta;
quando a ação não foi entregue pelo SAP, isso fica explícito. Esses registros
não inventam comandos ou estados. Árvores parciais mantêm seus avisos.

### Acompanhamento de scripts externos

Inicie `explore` na mesma sessão antes de executar seu script pelo mecanismo
habitual. O observador acompanha as telas acessíveis e os eventos entregues
pelo SAP. Ele não inicia o script e não intercepta suas linhas: a captura de
cada chamada feita por outro processo não é garantida. Scripts rápidos podem
produzir lacunas explicitadas no histórico. Para registro exato por chamada,
o script de origem precisará ser instrumentado.

### Sanitização antes de persistir

Senhas são redigidas por padrão. Para CPF, documentos, nomes e informações
comerciais, adapte `config/sanitization.example.json`:

- `field_patterns`: expressões regulares sobre ID, nome ou tipo do componente;
- `text_patterns`: expressões regulares substituídas no conteúdo por `[REDACTED]`;
- `redact_all_text`: omite todos os conteúdos textuais sujeitos à política.

A política é aplicada também aos parâmetros brutos dos eventos, à barra de
status e ao título da janela. Forma e ordem dos comandos são preservadas,
respeitando redação; parâmetros executáveis não recebem o limite de texto da exibição. Identificadores técnicos e metadados
da sessão continuam no histórico. A configuração de exemplo não reconhece
automaticamente todos os dados sensíveis. Use a mesma política entre execuções
que deseja comparar, pois ela afeta o hash de conteúdo e o agrupamento de ações.

### Situação dos requisitos

A conferência completa de `prompts.md`, correções, limitações e pendências de
homologação estão em [docs/robustness_review.md](docs/robustness_review.md).
As etapas 6–9 foram acrescentadas nesta revisão. A validação de eventos/telas
com SAP real continua necessária; não havia objeto `SAPGUI` acessível no ambiente
da revisão.

## Requisitos

- Windows;
- SAP GUI for Windows instalado e já aberto;
- Python 3.10 ou superior;
- SAP GUI Scripting permitido no servidor e habilitado no cliente;
- acesso do usuário à sessão SAP que será inspecionada.

O acesso COM é feito por `pywin32`. A interface usa `tkinter/ttk`, incluído na
instalação padrão do Python para Windows (componente Tcl/Tk).

## Instalação

### Windows: arquivos CMD

1. Execute **`instalar_ambiente.cmd`**. Ele verifica Python 3.10+ e Tcl/Tk,
   cria `venv` (ou reutiliza o existente), instala `requirements.txt` e confere
   as dependências. Não exige ativar o ambiente nem privilégios de administrador.
2. Execute **`iniciar_aplicacao.cmd`**. Ele usa exclusivamente
   `venv\Scripts\python.exe` para abrir o gravador e selecionar a sessão SAP.

Os dois arquivos podem ser abertos por duplo clique ou chamados de outra pasta.
Mantenha-os junto de `main.py` e `requirements.txt`. O iniciador informa quando
é necessário executar o instalador. Um `venv` inválido não é apagado automaticamente.
As credenciais SAP são informadas no próprio SAP; os CMD não armazenam senhas.

Para execução sem pausa e opções adicionais:

```powershell
.\instalar_ambiente.cmd --no-pause
.\iniciar_aplicacao.cmd --artifacts artifacts/gravacoes
```

Em automações, a variável `SAP_EXPLORER_NO_PAUSE=1` desativa pausas de erro.
Ambos retornam código zero em sucesso e código diferente de zero em falha.

### Instalação manual

No PowerShell, a partir da raiz do projeto:

```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Habilitar e verificar SAP GUI Scripting

O scripting precisa estar liberado nos dois lados:

1. **Servidor SAP:** solicite à equipe Basis que confirme o parâmetro de perfil
   `sapgui/user_scripting = TRUE` (normalmente verificado na transação `RZ11`).
   Políticas adicionais do ambiente podem limitar o scripting por usuário.
2. **Cliente SAP GUI:** abra **SAP Logon Options** → **Accessibility & Scripting**
   → **Scripting** e marque **Enable scripting**. O nome exato pode variar de
   acordo com o idioma/versão do SAP GUI.
3. Abra o SAP Logon, conecte-se a um sistema e mantenha pelo menos uma sessão
   autenticada aberta.

Se a empresa bloquear essas opções, a liberação deve ser feita pela equipe SAP
responsável; o programa não tenta contornar a configuração.

Referências oficiais: [SAP GUI Scripting API](https://help.sap.com/docs/sap_gui_for_windows/b47d018c3b9b45e897faf66a6c0885a8/45a62269a13d4522997bedf3e6ff56f8.html)
e [habilitação no cliente](https://help.sap.com/docs/intelligent-robotic-process-automation/desktop-studio-developer-guide/enabling-scripting-on-client-side).

## Executar

Com uma sessão SAP já aberta:

```powershell
python main.py
```

A janela mostra as sessões encontradas e aguarda uma seleção explícita antes de
gravar. Os comandos de terminal (`monitor`, `inspect`, `record-cli` e `explore`)
mostram uma lista numerada; digite o índice mostrado entre colchetes.

Exemplo ilustrativo:

```text
INFO: [0] conexão=0 sessão=0 | descrição=Quality | sistema=QAS | cliente=100 ...
Selecione uma sessão [0-0]: 0
INFO: Conexão com a sessão confirmada.
```

### Monitorar mudanças de tela

```powershell
python main.py monitor
```

Após a seleção da sessão, o programa consulta o estado a cada segundo e escreve
uma linha somente quando mudam transação, programa, número da tela, janela ativa
ou quantidade de janelas. Alterações transitórias de `Busy` não geram linhas.
Use `CTRL+C` para encerrar de forma limpa.

Exemplo:

```text
10:21:03  VA01 | SAPMV45A | 0101 | wnd[0] | Criar ordem
10:21:08  VA01 | SAPMV45A | 4001 | wnd[0] | Criar ordem
10:21:11  VA01 | SAPMV45A | 4001 | wnd[1] | Informação
```

O estado também pode ser serializado por `SapScreenState.to_dict()` ou
`SapScreenState.to_json()`.

### Inspecionar a estrutura da janela ativa

```powershell
python main.py inspect
python main.py inspect --json
```

O primeiro comando mostra o estado atual, a estratégia utilizada, a quantidade
de componentes e uma árvore resumida. O segundo escreve em `stdout` um JSON com
o `SapScreenState` e sua `ui_tree`; mensagens de diagnóstico permanecem em
`stderr`.

A captura tenta primeiro `session.GetObjectTree`, disponível a partir do SAP GUI
for Windows 7.70 PL3. O escopo é limitado à janela ativa e nove
propriedades genéricas são solicitadas. Uma etapa adicional lê estados específicos
dos controles, dentro dos limites configurados. Em versões anteriores ou em caso de falha, o
programa percorre `Children` recursivamente, com limites padrão de 24 níveis e
2.000 componentes, proteção contra ciclos e isolamento de erros por componente.
Se uma propriedade opcional não for aceita, `GetObjectTree` é repetido com o
conjunto básico (`Id`, `Type`, `Name`, `Text`, `Tooltip` e `Changeable`). Consulte
a [documentação oficial de GetObjectTree](https://help.sap.com/docs/sap_gui_for_windows/b47d018c3b9b45e897faf66a6c0885a8/a4e022f6c155414d9c8ae8eba422cac5.html).

Antes de qualquer saída, `Text` e `Tooltip` passam por
`default_text_sanitizer`. Ele redige campos de senha, remove quebras de linha e
limita textos a 200 caracteres. Integrações podem fornecer outra função por meio
do argumento `sanitizer` de `SapObjectTreeReader`. O SAP também documenta que o
texto de `GuiPasswordField` normalmente não pode ser lido e retorna vazio.

### Fingerprints estáveis

`fingerprint_screen(SapScreenState)` produz dois hashes SHA-256:

- `structural_hash`: transação, programa, número da tela, janela ativa e a
  estrutura formada por `Id`, `Type`, `SubType`, `Name` e relações entre nós;
- `content_hash`: inclui o `structural_hash` e acrescenta títulos, textos,
  tooltips e os estados `Changeable`, `Enabled` e `Visible`.

Antes do hash, textos são normalizados em Unicode NFC, os prefixos locais
`/app/con[n]/ses[n]/` são removidos dos IDs e filhos/raízes são ordenados pela
representação JSON canônica. Assim, ordem de leitura e índices da sessão local
não mudam a identidade. A hierarquia entre componentes permanece preservada.

Campos transitórios como `Busy`, `IsActive`, ID da sessão, origem da captura,
avisos e marcações de truncamento não participam dos hashes. Textos também não
participam do hash estrutural; por isso valores como `Documento 12345` e
`Documento 87654` mantêm a mesma identidade estrutural e mudam apenas o hash de
conteúdo. Não existem regras específicas para transações ou documentos SAP.

### Ouvir eventos da sessão no terminal

```powershell
python main.py record-cli
```

Depois de selecionar explicitamente a sessão, o comando conecta um listener COM
e tenta ativar `session.Record = True`. Use o SAP normalmente: cada evento
recebido aparece em uma linha curta no terminal. Pressione `CTRL+C` para encerrar.
`Record` volta para `False` somente quando este programa o havia ativado; se já
estava ativo, seu estado é preservado. Eventos finais são drenados antes de
remover o listener.

O CLI não habilita `elementVisualizationMode`, portanto a captura permanece
passiva e o evento `Hit` normalmente não aparecerá. Nenhum callback chama
`Press`, `SendVKey`, `FindById` ou outra operação de interação.

#### Eventos confirmados na API oficial

A interface de eventos de `GuiSession` documenta:

- `Change(Session, Component, CommandArray)`: emitido somente em modo de
  gravação, imediatamente antes de `StartRequest`; pode trazer uma ou mais
  linhas de comando;
- `StartRequest(Session)` e `EndRequest(Session)`: antes do bloqueio e depois do
  desbloqueio da sessão em uma comunicação com o servidor;
- `FocusChanged(Session, NewFocusedControl)` e
  `HistoryOpened(Session, NewFocusedControl)`;
- `ContextMenu(Session, Component)`: limitado a menus de contexto de `GuiShell`
  e não emitido para menus em cache;
- `Hit(Session, Component, InnerObject)`: somente quando
  `elementVisualizationMode` está ativo;
- `Destroy(Session)`, `Activated(Session)`, `Error(...)`,
  `ProgressIndicator(percentage, Text)`, `AbapScriptingEvent(param)` e
  `AutomationFCode(Session, FunctionCode)`; este último é específico do SAP
  Workplace.

Essas assinaturas e restrições vêm da
[documentação oficial dos eventos de GuiSession](https://help.sap.com/docs/sap_gui_for_windows/b47d018c3b9b45e897faf66a6c0885a8/3075476b6f834449a090411ee3bbbfb4.html)
e da [referência completa da API](https://help.sap.com/doc/9215986e54174174854b0af6bb14305a/770.08/en-US/sap_gui_scripting_api.pdf).
O código não pressupõe que `Change` exista: se `Record` estiver indisponível ou
for bloqueado, os demais listeners permanecem anexados e o CLI mostra um aviso.

O parâmetro de servidor `sapgui/user_scripting_disable_recording = TRUE`
desabilita os eventos de scripting e impede a gravação. A condição é consultada
em `session.Info.ScriptingModeRecordingDisabled`; a aplicação não tenta
contorná-la. Consulte o
[SAP GUI Scripting Security Guide](https://help.sap.com/doc/97d2d0bc2ed248a4a85a0bec608704f8/770.00/en-US/sap_gui_scripting_sec_guide.pdf).

#### Decisões de implementação COM

`SapActionRecorder` usa `win32com.client.WithEvents` e mantém referências fortes
à sessão e ao sink retornado. O vínculo, `pythoncom.PumpWaitingMessages()` e
`sink.close()` são executados na mesma thread. Os callbacks apenas copiam e
normalizam a carga para uma fila; a saída ocorre fora do callback, evitando
bloquear a entrega COM. A implementação segue o ciclo de vida documentado no
[código do pywin32](https://github.com/mhammond/pywin32/blob/main/com/win32com/client/__init__.py)
e o [exemplo oficial de eventos](https://github.com/mhammond/pywin32/blob/main/com/win32com/test/testMSOfficeEvents.py).

Se `WithEvents` falhar na geração automática de classes (`makepy/genpy`,
incluindo `AssertionError` em `enumEntry.desc.desckind`), o programa tenta
automaticamente um vínculo direto com o connection point COM da sessão.
O módulo `com_events.py` descobre o IID e os DISPIDs na biblioteca de tipos
do SAP, sem gerar classes, modificar o pywin32 ou apagar o cache `gen_py`.
Os argumentos COM dos callbacks são convertidos por despacho dinâmico.
O log de sucesso dessa alternativa é **“Listener COM direto ativo”**.
Caso ambas as estratégias falhem, a mensagem informa as duas causas.

`SapActionEvent.raw_commands` preserva a forma e a ordem do `CommandArray` como
tipos JSON, aplicando a política de redação também à cópia bruta, preservando espaços,
strings vazias e textos longos; indicadores de qualidade descrevem perdas. Já
`commands` contém `command_type`, `member_name` e
`parameters` separados. Nenhuma dessas formas é código Python. Objetos COM vivos
não são serializados. Componentes identificados como `GuiPasswordField` ou por
ID/nome de senha têm todos os parâmetros redigidos como `[REDACTED]`; a saída do
CLI também não imprime a carga bruta.

## Testes unitários

Os testes não exigem SAP nem uma instalação funcional do COM; eles usam objetos
falsos com a interface mínima das coleções do SAP GUI Scripting:

```powershell
python -m unittest discover -s tests -v
```

Eles verificam enumeração completa, seleção explícita, conexão vazia, ausência
do SAP GUI/Scripting Engine, bloqueio pelo servidor, índice inválido, referência
de sessão expirada, comparação e serialização de estados, detecção de mudanças,
campos COM ausentes, falhas transitórias durante o monitoramento, normalização e
serialização da árvore, fallback recursivo, limites, sanitização, fingerprints
estrutural/de conteúdo, normalização de `CommandArray`, callbacks simulados,
serialização de eventos, bloqueio de recording e limpeza do sink COM.

## Teste manual com SAP real

1. Instale as dependências e confirme as configurações de scripting acima.
2. Feche o SAP GUI e execute `python main.py`: a janela deve mostrar uma mensagem
   clara informando que o objeto `SAPGUI` não pôde ser obtido e permitir atualizar.
3. Abra o SAP Logon sem autenticar em um sistema e execute novamente: dependendo
   da versão, o resultado deve indicar nenhuma conexão ou nenhuma sessão.
4. Autentique-se e mantenha uma sessão aberta. Execute `inspect` e confirme que ela
   aparece com sistema, cliente, usuário, transação, programa e tela coerentes.
5. Abra uma segunda sessão em **Sistema → Criar sessão** (e, se disponível, uma
   conexão para outro sistema). Execute novamente e confirme que todas aparecem,
   sem seleção automática da primeira.
6. Escolha uma sessão que não seja a primeira. Confira no log os índices
   `(conexão, sessão)` e os metadados confirmados.
7. Verifique visualmente que nenhuma transação, campo ou tela do SAP foi alterada.
8. Para conferir o tratamento de referência inválida, liste as sessões, feche no
   SAP a sessão que pretende escolher antes de digitar o índice e pressione
   Enter. O CLI deve encerrar com erro, sem tentar usar outra sessão.

### Roteiro manual do monitor

1. Entre no SAP e execute `python main.py monitor`.
2. Selecione explicitamente a sessão desejada.
3. Abra uma transação e confirme uma linha com transação, programa, tela e
   `wnd[0]` coerentes.
4. Navegue por algumas telas e confirme que uma nova linha aparece quando o
   número da tela ou outro identificador técnico muda.
5. Abra um popup e confirme a janela ativa `wnd[1]`, o texto do popup e a
   indicação interna de mais de uma janela.
6. Feche o popup e confirme o retorno para `wnd[0]`.
7. Troque de transação e confirme a alteração de transação/programa/tela.
8. Permaneça alguns segundos sem navegar: não devem aparecer linhas repetidas.
9. Pressione `CTRL+C` e confirme a mensagem de encerramento limpo.

Alguns ambientes exibem avisos de segurança ao detectar acesso por scripting.
Isso é controlado pelas opções e políticas do SAP GUI e não significa que o
programa tenha executado uma ação na sessão.

### Roteiro manual do recorder

Use preferencialmente um sistema de desenvolvimento/qualidade e dados de teste.

1. Confirme com a equipe Basis que `sapgui/user_scripting = TRUE` e
   `sapgui/user_scripting_disable_recording = FALSE` para o servidor da sessão.
   Em `session.Info`, o segundo ajuste aparece como
   `ScriptingModeRecordingDisabled = False`.
2. Abra uma sessão SAP autenticada e execute `python main.py record-cli`.
3. Selecione o índice exato da sessão. Confirme a mensagem “eventos Change são
   esperados”. O SAP pode mostrar seu aviso normal de segurança de scripting.
4. Clique entre dois campos. Verifique eventos `FocusChanged` com IDs coerentes.
5. Altere um campo de teste e pressione Enter. Verifique um `Change` com o
   componente e o comando normalizado, seguido por `StartRequest` e
   `EndRequest`. Não use credenciais ou outros segredos nesse ensaio.
6. Abra um menu de contexto de um `GuiShell`. Quando o controle e o cache
   permitirem, verifique `ContextMenu`.
7. Navegue para outra tela e confirme novos pares `StartRequest`/`EndRequest`.
8. Feche a sessão selecionada e confirme que `Destroy` encerra o listener; em
   clientes que não entreguem `Destroy`, três falhas consecutivas da verificação
   de sessão encerram a captura como desconectada.
9. Repita o teste e encerre com `CTRL+C`. Confirme a mensagem de encerramento e
   que o SAP continua utilizável.
10. Para testar a política de servidor, faça o ensaio em uma sessão onde
    recording esteja bloqueado. O CLI deve avisar que `Change` não é esperado,
    sem tentar alterar a política nem derrubar o processo.

`Hit` não faz parte desse roteiro porque exigi-lo implicaria habilitar o modo de
visualização, uma mudança que esta etapa deliberadamente não realiza.

## Estrutura

```text
src/
    sap_explorer/
        __init__.py
        action_recorder.py
        com_events.py
        connector.py
        events.py
        fingerprint.py
        object_tree.py
        screen_reader.py
        models.py
        transition_recorder.py
        repository.py
        analyzer.py
        sanitization.py
tests/
    __init__.py
    test_action_recorder.py
    test_connector.py
    test_events.py
    test_fingerprint.py
    test_main.py
    test_object_tree.py
    test_screen_reader.py
main.py
requirements.txt
README.md
```

## Homologação manual da exploração (etapas 6–9)

1. Use dados de teste em uma sessão SAP já autenticada. Execute `inspect --json`
   e confira os IDs, a árvore e a barra de status contra a tela visível.
2. Execute `explore --db artifacts/homologacao.sqlite3` e selecione essa sessão.
   Confirme uma linha STATE inicial e o caminho do banco.
3. Altere dois campos e envie Enter. Confira as ações em ordem e a observação
   posterior, incluindo transação, programa, tela e status. `complete` indica
   captura dos dois extremos, não sucesso funcional.
4. Abra e feche um popup. Confira a janela ativa e os novos estados. Repita com
   uma mensagem de erro de validação usando dados de teste.
5. Navegue novamente pelo mesmo caminho. Encerre com `CTRL+C`, inicie outra
   exploração no mesmo banco e repita. Em `stats`/`export`, confira contagens
   acumuladas e definições estruturais deduplicadas.
6. Altere um campo e encerre sem enviar Enter. Confira no diário se o SAP
   entregou o Change ao desligar Record. Se já havia outro recorder ativo, o
   programa não o desliga e não pode forçar essa entrega.
7. Feche manualmente a sessão observada durante a captura. Confira o encerramento
   por Destroy/desconexão e a preservação das transições incompletas.
8. Execute `stats`, `export` e `analyze` com `--db artifacts/homologacao.sqlite3`.
   Confira estados, ações, resultados diferentes, popups e mensagens no JSON e
   no Markdown. A análise agregada é gerada por `analyze`; o roteiro por execução
   é gerado automaticamente ao encerrar ou pelo comando `context`.
9. Se houver um script externo de teste, inicie a exploração antes dele e
   compare manualmente suas chamadas com o diário. Registre a cobertura real
   do cliente SAP; lacunas não equivalem a chamadas que não foram executadas.
10. Repita com uma política de sanitização apropriada e confira que os valores
    escolhidos foram redigidos tanto na árvore quanto nas cargas dos eventos.
