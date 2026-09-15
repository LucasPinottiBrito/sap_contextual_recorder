# Gravar casos com fidelidade e contexto

As correções desta versão preservam parâmetros, acrescentam estado de controles,
registram lotes e permitem identificar casos. A ferramenta continua sendo um
gravador passivo: não altera campos, seleciona abas nem executa a titularidade.

## Primeira gravação

Ative o ambiente Python do projeto. Copie `config/case.example.json` para um
arquivo local, por exemplo `config/case.local.json`, e preencha os dados do caso.
Os valores são metadados da gravação; não são enviados para campos no SAP.

```json
{
  "name": "Titularidade - caso 001",
  "process": "alteracao_titularidade",
  "inputs": {
    "instalacao": "00000000",
    "novo_titular": "IDENTIFICADOR_DO_NOVO_TITULAR",
    "data_vigencia": "AAAA-MM-DD"
  }
}
```

```powershell
python main.py explore --case-file config/case.local.json
```

Selecione a sessão e realize um caso completo. O terminal informa o ID do caso
e o ID da execução. Sem `--case-file`, `explore` cria um caso genérico, sem entradas.
Nesta versão cada execução pertence a um caso; para trocar de caso, encerre com
CTRL+C e inicie outra gravação. Um mesmo caso pode ter várias execuções.

Para continuar um caso ainda aberto em outra execução:

```powershell
python main.py explore --case-id ID_DO_CASO
```

A política de sanitização do caso é mantida na retomada. Por isso `--case-id`
não pode ser combinado com `--sanitize-config`. Casos finalizados ou com outra
execução ainda aberta não aceitam uma nova gravação.

## Marcos e resultado

Durante a gravação, um segundo terminal pode registrar uma observação de negócio:

```powershell
python main.py case-mark --case-id ID_DO_CASO --label "Dados conferidos" --note "Instalação e novo titular conferidos"
```

O marco aponta para a última observação **já persistida** do caso. O horário do
marco é o momento da anotação; ele não representa uma captura simultânea do SAP.

Após encerrar a gravação com CTRL+C, informe o resultado:

```powershell
python main.py case-finish --case-id ID_DO_CASO --outcome confirmed --evidence "Titular e vigência conferidos no resultado do SAP; referência do atendimento ..."
```

Resultados disponíveis:

| Valor | Significado |
| --- | --- |
| `confirmed` | Operador verificou o resultado de negócio |
| `failed` | Caso falhou |
| `cancelled` | Operação cancelada |
| `inconclusive` | Não há evidência suficiente do resultado |

É obrigatório informar `--evidence`, inclusive para explicar falha ou resultado
inconclusivo. CTRL+C apenas encerra a captura; não confirma o processo. A evidência
é uma declaração do operador, não uma validação automática da titularidade.

```powershell
python main.py cases
python main.py stats
python main.py export
python main.py analyze
```

Esses comandos funcionam sem SAP. Use `--db CAMINHO` em todos eles quando estiver
gravando em um banco diferente do padrão.

## Limites de captura

```powershell
python main.py explore --case-file config/case.local.json --max-depth 24 --max-nodes 2000 --grid-rows 20 --grid-columns 20 --capture-seconds 0.5
```

`--capture-seconds` limita cooperativamente o enriquecimento dos controles: o
prazo é consultado entre chamadas COM. Não interrompe uma chamada já iniciada nem
limita a chamada inicial de GetObjectTree. A árvore básica continua limitada
por profundidade e quantidade de nós.

Para concentrar o enriquecimento em uma área técnica:

```powershell
python main.py explore --control-scope "wnd[0]/usr" --control-scope "wnd[1]/usr"
```

Os escopos são prefixos de IDs relativos. Eles limitam as leituras adicionais,
não a árvore básica. O orçamento padrão adicional também limita leituras a 2.000
operações e entradas de combo/árvore a 100, configuráveis pela API `CaptureOptions`.

## Informações adicionais gravadas

| Controle | Estado adicional |
| --- | --- |
| Campo de texto, código da transação, TextEdit | Texto integral sujeito à política de redação |
| Checkbox/radio | Seleção |
| Combo | Chave selecionada e amostra das opções |
| TabStrip | ID relativo da aba selecionada |
| GridView | Colunas técnicas, seleção, célula atual e amostra a partir da primeira linha visível |
| GuiTableControl | Colunas, células visíveis, índice absoluto e seleção das linhas amostradas |
| Tree | Nó selecionado, chaves e textos de uma amostra de nós |
| HTMLViewer | Conteúdo interno explicitamente marcado como não suportado |

O gravador não rola tabelas, não muda a seleção e não abre abas para coletar dados.
Conteúdo não exposto pelo controle permanece desconhecido. GetAllNodeKeys pode
retornar a coleção completa; só os textos da amostra são lidos. O suporte das
propriedades depende do controle e da versão instalada do SAP GUI.

## Fidelidade e qualidade

Os parâmetros de comandos preservam strings vazias, espaços, quebras de linha,
textos longos, números, booleanos e null. O terminal resume parâmetros longos;
isso não corta o que é persistido. A forma e a ordem de `raw_commands` são
preservadas, exceto por redação, tipos não serializáveis ou limite de profundidade.

Cada comando possui `quality.parameters_preserved` e uma lista `quality.issues`
com os caminhos afetados. Isso indica fidelidade dos parâmetros, não garantia
de que o comando seja executável ou adequado a outro caso. `parameter_layout`
informa se a normalização interpretou um array de argumentos; `raw_commands`
continua sendo a referência para a forma recebida.

Cada nó pode conter `control_state` e `quality`. A qualidade distingue:

- `captured`: propriedade lida;
- `sampled`: somente parte dos registros/opções foi incluída;
- `partial`: há perdas ou propriedades específicas indisponíveis;
- `unavailable` / `unsupported`: leitura indisponível ou conteúdo não suportado;
- `omitted_budget` / `omitted_scope`: leitura não tentada devido ao limite/escopo;
- listas de perdas por caminho: `redacted`, `truncated`, `transformed`, `omitted`
  ou `unsupported_type`.

`preview_text` e `preview_tooltip` descrevem perdas nos resumos antigos. A qualidade
de `text` descreve o texto integral em `control_state`, quando capturado. As
propriedades `enabled` e `visible` continuam nulas quando o SAP não as expõe;
nulo não significa habilitado, desabilitado, visível ou invisível.

Falhas de normalização nos callbacks geram eventos `CaptureError`, sem guardar
os parâmetros ou mensagens de exceção que poderiam expor conteúdo redigido.

## Ordem, lotes e contexto de popup

`capture_timeline` registra uma sequência crescente por execução para eventos,
observações e transições. Essa é a ordem de persistência/recebimento, não o horário
real de cada clique. O timestamp original de recebimento do evento é preservado.

`request_batches` reúne IDs de eventos, limites StartRequest/EndRequest quando
recebidos e o intervalo de observações locais anterior ao lote. Uma mudança de
aba pode estar nesse intervalo antes da entrega do comando de seleção. Por isso
`exact_before_known` é falso: não se inventa uma tela anterior exata por comando.
Os extremos da transição continuam sendo os estados observados pelo gravador.

Quando há popup, `observation_metadata.previous_main_window` aponta para a última
observação da janela principal, com seu timestamp. Ela é contexto histórico, não
uma leitura atual garantida da janela coberta pelo popup.

## Compatibilidade e backup

Na primeira abertura de um banco legado, o repositório cria um backup SQLite
consistente com o nome `BANCO.pre-v1.ID.sqlite3`, indicado no log, e realiza uma
migração aditiva. As seis tabelas anteriores e seus registros são preservados.
As novas tabelas armazenam metadados, linha do tempo, lotes, casos e marcos.

Não são reconstruídos retroativamente horários, qualidade ou parâmetros que o
gravador antigo perdeu. O export identifica esses limites; a análise lista
comandos antigos sem qualidade conhecida. Os hashes estruturais mantêm a versão
2; o conteúdo passa à versão 3 para incluir seleções e valores adicionais.

O export agora é versão 2. Consumidores próprios devem aceitar as novas chaves
`timeline`, `request_batches`, `cases`, `case_runs`, `case_marks`, `run_metadata`
e `observation_metadata`.

## Verificação realizada

Os testes automatizados cobrem fidelidade dos parâmetros, redação, coleções SAP,
ambas as estratégias da árvore, seleções, amostras, orçamentos, associação de
lotes, sequência persistente, casos e migração com backup. A validação no cliente
SAP real ainda deve conferir se os controles utilizados no processo expõem os
estados esperados e se os orçamentos são suficientes.

Antes de gravar muitos casos, faça uma alteração completa e examine o export:
os campos que determinam decisões devem estar capturados; amostras não devem ser
interpretadas como listas completas. Registre e explique o resultado do caso.

Referência dos adaptadores: [SAP GUI Scripting API](https://help.sap.com/doc/9215986e54174174854b0af6bb14305a/800.04/en-US/sap_gui_scripting_api.pdf).
