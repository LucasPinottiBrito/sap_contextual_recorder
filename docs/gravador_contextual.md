# Gravador contextual

## Uso

Execute `python main.py` ou `python main.py record`. A janela enumera as sessões
SAP existentes e exige seleção, mesmo se houver apenas uma sessão. **Atualizar
sessões** refaz a lista. A gravação começa apenas ao clicar em **Iniciar gravação**.

O painel acompanha eventos, comandos, transições e janelas ativas; selecionar uma
ocorrência mostra o JSON integral recebido. **Etapa** e **Resultado esperado**
permitem anotar a observação atualmente exibida. Essa associação permanece a mesma
se o SAP mudar de tela antes de a fila de anotações ser processada.

**Finalizar e gerar arquivos** desliga o modo Record se foi ativado pelo gravador,
drena os eventos finais, finaliza transições e exporta JSON e Markdown. Fechar a
janela realiza o mesmo processo. Uma gravação interrompida por erro pode gerar
um arquivo parcial, com o motivo e os dados já persistidos. O banco confirma cada
evento/observação em transação SQLite; falhas de exportação podem ser recuperadas:

```powershell
python main.py export --db artifacts/exploration.sqlite3
# Consulte runs[].id no exploration.json:
python main.py context --db artifacts/exploration.sqlite3 --run-id ID --artifacts artifacts
```

Uma execução que sofreu término abrupto do processo continua com `ended_at=null`
no roteiro recuperado. O exportador não inventa uma finalização.

## Contrato do JSON

`schema=sap_contextual_recording`, `schema_version=1`. Nomes de arquivos incluem
o ID da execução para impedir sobrescrita entre gravações diferentes.

| Seção | Conteúdo |
| --- | --- |
| `run`, `case`, `capture_metadata` | Sessão, início/fim, capacidades, limites, política e caso |
| `general_steps` | Resumo numerado das etapas observadas |
| `steps` | Tela anterior/posterior, ações ordenadas, controles, lote e resultado observado |
| `steps[].actions[].script_preview` | Prévia Python de comandos íntegros M/SP, sem execução |
| `observations` | Todas as telas capturadas, com árvore, valores, seleções, amostras de tabelas e qualidade |
| `observation_metadata` | Relação com última janela principal observada e disponibilidade da árvore |
| `events` | Diário integral, inclusive foco, requisições, erros e comandos brutos sanitizados |
| `timeline` | Ordem global de persistência, definida por `sequence` |
| `annotations` | Etapa (`label`) e resultado esperado informado pelo operador (`note`), ligados à observação |
| `unassigned_actions`, `warnings` | Ações sem correlação e limitações encontradas |
| `generation_guidance`, `references` | Orientações e fontes para gerar o script contextual |

`expected_result` preserva a barra de status (texto, tipo, classe, número,
parâmetros 0–7, indicação de popup e texto longo) e, quando a janela ativa é um
popup SAP, seu título e textos. `message_has_long_text` indica disponibilidade;
o gravador não abre a ajuda para coletar esse texto. Parâmetros não acessíveis
são `null`, sem substituir essa ausência por um valor presumido.

Um resultado obtido após o lote usa `basis=observed_after_batch` e
`review_required=true`. A barra pode manter uma mensagem anterior. O resultado
funcional permanece não confirmado; a mensagem capturada deve ser revisada para
virar uma asserção no script final. As expectativas digitadas ficam em `annotations`.

Os IDs relativos em `locator` removem somente o prefixo volátil da sessão. Os
IDs originais continuam nos eventos. `control_context` se refere à última
observação verificada da tela de origem, não ao instante exato de cada comando.

Prévia de uma cadeia emitida pelo SAP:

```python
session.findById('wnd[0]/usr/tblEXAMPLE').getAbsoluteRow(1).selected = True
```

As linhas de um mesmo `Change` permanecem encadeadas. Tipos desconhecidos,
parâmetros redigidos/transformados ou fidelidade não comprovada geram
`python=null` com motivo. O arquivo não é um script de automação pronto para
execução: a próxima etapa deve introduzir entradas, condições, esperas com timeout,
tratamento de popups e critérios de resultado revisados.

## Arquitetura e boas práticas

- Tkinter manipula widgets apenas na thread principal. Um worker mantém COM,
  descoberta, seleção, listener, leitura e SQLite. Filas carregam dados Python;
  `threading.Event` solicita parada sem manipular COM de outra thread.
- Callbacks COM copiam cargas para uma fila; leitura estrutural, persistência e
  atualização da interface acontecem fora desses callbacks.
- O worker mantém o apartment COM até concluir a captura final. O listener é
  removido e `Record` restaurado antes de liberar as referências COM.
- O arquivo JSON e o Markdown usam substituição atômica individual. O banco
  permanece como fonte para regenerar os arquivos, inclusive se só um exportar.
- O formato de conteúdo do fingerprint passa a versão 4 para incluir os novos
  atributos da barra de status; a identidade estrutural não muda. Históricos
  anteriores continuam intactos e têm suas versões registradas.
- O modo gráfico preserva textos completos; o resumo visual é limitado. Senhas
  e campos configurados continuam redigidos antes de entrar na fila persistente.

## Cobertura e limites

A captura complementa `Change` com observações estáveis e valores pré-preenchidos.
Nenhum gravador baseado nessa API garante cada clique físico: há eventos em lote,
controles não suportados, popups curtos e diálogos nativos do Windows fora da
árvore SAP. Menus em cache e chamadas de scripts externos podem não emitir todos
os eventos. Nessas situações, o roteiro preserva a incerteza.

`--interval`, `--max-depth`, `--max-nodes`, `--grid-rows`, `--grid-columns`,
`--capture-seconds` e `--control-scope` controlam a captura. A árvore é da janela
ativa; a janela principal anterior é uma observação histórica. Limites de tempo,
nós, linhas e colunas geram indicadores de qualidade. Aumentar limites aumenta
o trabalho de leitura e pode prejudicar a captura de telas muito rápidas.

O SAP pode entregar edições apenas antes de uma requisição ou ao desligar Record.
Consequentemente, o painel apresenta ações quando elas chegam à API. Quando outro
gravador já ativou Record, seu estado é preservado e não se força o flush final.
O SAP também pode mudar o comportamento de diálogos F4 em modo de gravação.

## Referências oficiais consultadas

- [SAP GUI Scripting API 7.70](https://help.sap.com/doc/9215986e54174174854b0af6bb14305a/770.06/en-US/sap_gui_scripting_api.pdf):
  `GuiSession.Record`, eventos `Change/StartRequest/EndRequest`, *Change Event —
  Additional Remarks* (pp. 283–285) e `GuiStatusbar` (pp. 216–218).
- [Eventos de GuiSession](https://help.sap.com/docs/sap_gui_for_windows/b47d018c3b9b45e897faf66a6c0885a8/3075476b6f834449a090411ee3bbbfb4.html).
- [Threading model do Tkinter](https://docs.python.org/3/library/tkinter.html#threading-model).
- [Filas sincronizadas do Python](https://docs.python.org/3/library/queue.html).

## Homologação com SAP real

1. Execute sem SAP aberto; confira a mensagem de erro e a possibilidade de atualizar.
2. Abra duas sessões e selecione a segunda. Confira os metadados da gravação.
3. Inicie e altere campos (inclusive valores longos), checkbox, combo e seleção de
   uma linha de tabela/ALV. Envie Enter; confira a ordem e os parâmetros capturados.
4. Abra um popup SAP, registre uma anotação e feche o popup. Confira telas,
   ações e associação da anotação no JSON.
5. Provoque uma validação de teste e confira mensagem, tipo, classe, número e
   parâmetros. Verifique que a mensagem não foi classificada como sucesso.
6. Altere um campo e finalize sem enviar Enter. Confira o último Change entregue
   ao desligar Record e os arquivos gerados.
7. Repita fechando a janela do gravador e depois fechando a sessão SAP selecionada.
   Confira a preservação do histórico e o motivo da parada.
8. Compare a gravação com o recorder nativo do SAP em um processo de teste.
   Revise campos pré-preenchidos, linhas selecionadas e eventuais lacunas.
9. Gere novamente com `context` sem SAP aberto e confira a execução selecionada.

Os testes automatizados usam objetos simulados para SAP/COM e widgets reais
Tkinter quando disponíveis. A cobertura específica dos controles de cada sistema
SAP precisa desta homologação manual.
