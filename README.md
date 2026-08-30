# English Activities & Leaderboard

Aplicação Streamlit para receber comprovações de atividades de inglês, executar
OCR, encaminhar ambiguidades para revisão e calcular o leaderboard a partir de
um ledger imutável.

## Arquitetura oficial e estado da migração

A arquitetura oficial de produção é **Streamlit Cloud + Supabase Auth + Supabase
PostgreSQL + Supabase Storage**. GitHub armazena somente código, configuração sem
segredos, documentação e assets fixos. Arquivos de alunos pertencem ao Storage; dados
estruturados e metadados pertencem ao PostgreSQL; autenticação pertence ao
Supabase Auth. O filesystem do Streamlit é sempre temporário.

Leia [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) antes de alterar persistência,
autenticação ou uploads.

> **Arquitetura oficial em vigor.** Identidade no Supabase Auth, dados no
> Supabase PostgreSQL com RLS, binários no Supabase Storage privado. A
> autenticação local, o SQLite de produção, os uploads em disco e o backup no
> GitHub foram removidos do código. O SQLite permanece apenas como banco de
> desenvolvimento e da suíte de testes, exercitando os **mesmos** modelos.

## O que está incluído

Na implementação legada atual, preservada enquanto a migração é feita:

- página pública **Entrar** em uma barra de navegação superior, sem menu lateral;
- autenticação local fechada por **nome de usuário** e senha Argon2, com senha
  temporária, troca obrigatória, sessões persistentes e revogáveis e bloqueio
  por tentativas;
- depois do login, rotas permitidas pelo papel e a página **Minha conta** com identidade e logout;
- papéis `student` e `admin`, validados também na camada de serviço;
- catálogo configurável com pontuação histórica preservada;
- caixa única de upload para PNG, JPEG, WebP, PDF, DOCX e TXT, com validação
  do conteúdo real, limites, extração seletiva e nomes UUID;
- OCR RapidOCR/ONNX local, carregado uma vez com `st.cache_resource`;
- SHA-256 para duplicata exata e ImageHash pHash para alerta visual;
- regras conservadoras para Duolingo/BeConfident e campos estruturais;
- fila administrativa, correção de unidades, aprovação e rejeição auditadas;
- uma unidade por conclusão; `combo` nunca é interpretado como número de lições;
- grupos únicos de cinco lições que geram 5 pontos, e o mesmo agrupamento
  disponível para qualquer atividade pelo campo **Unidades por premiação**;
- exclusão de atividade com confirmação explícita, avisando antes se o efeito
  será arquivar (há histórico) ou remover em definitivo;
- Reunião em inglês tratada como atividade comum configurável, sem fluxo especial;
- ledger, leaderboard geral/por período, XLSX e espelho opcional no Google Sheets;
- aba **Recursos** com links de estudo publicados pela administração, em cartões
  que viram uma coluna no celular e abrem em nova aba;
- meta de lições por semana definida pela administração, com progresso no painel
  do aluno e contagem de quantos alunos a cumpriram;
- indicador de quantos pontos faltam para alcançar o colocado à frente, com as
  atividades do catálogo que fecham essa diferença em menos envios;
- históricos visuais por aluno e administrador, gestão segura de contas/atividades
  e lembretes SMTP configuráveis em processo separado;
- importação idempotente da planilha legada e relatório JSON;
- SQLite em memória para a suíte offline, sobre os mesmos modelos que rodam no
  PostgreSQL de produção.

## Materiais analisados

A cópia `inputs/aulas ingles 7565.xlsx` foi lida sem modificação. A aba `Página1` contém 15 alunos, 81 lançamentos diários e 585 pontos. O catálogo da planilha tem uma regra antiga de 15 pontos para Impact; o seed usa os 10 pontos definidos no briefing.

Os arquivos reais usados na validação ficam fora do repositório para preservar
dados pessoais. Screenshots anonimizados ou sintéticos podem ser usados em testes
locais. Veja [docs/PRD.md](docs/PRD.md) e [docs/SPEC.md](docs/SPEC.md).

## Requisitos

- Python 3.11 ou 3.12 (RapidOCR legado não suporta Python 3.13+);
- Streamlit 1.61.1 ou superior, com o extra de autenticação;
- aproximadamente 2–4 CPUs, 8 GB de RAM e SSD.

O núcleo legado, OCR e testes locais funcionam sem API externa. A arquitetura
oficial de produção exige conectividade com Supabase e Supabase Storage.

## Execução local

```bash
cp .env.example .env
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
pip install --no-build-isolation -e .
english-leaderboard init-db
streamlit run streamlit_app.py
```

Abra `http://localhost:8501`. A aplicação começa na área pública, em **Entrar**.
Toda conta vive no Supabase Auth: preencha as variáveis do Supabase e as de
bootstrap no `.env` antes de subir, porque não existe login local nem identidade
de demonstração.

`SEED_FAKE_DATA=true` popula cinco alunos marcados como **Demo** com envios
sintéticos e ranking idempotente, úteis para ver o leaderboard preenchido em
desenvolvimento. Eles são apenas perfis, sem conta no Auth e sem acesso — por
isso a opção nasce `false` no `.env.example` e não deve ser ligada em um banco
apontado para um Supabase real. Desligá-la não apaga lançamentos já criados no
ledger imutável.

## Contas e autenticação

A identidade vive no **Supabase Auth**. A aplicação não guarda senha, hash nem
sessão própria: ela troca credenciais por um token do Supabase e o mantém em
memória, com o *refresh token* no `localStorage` do navegador por um componente
bidirecional do Streamlit. Nada de nome, usuário ou papel é gravado no navegador.

Não existe cadastro público. O primeiro administrador nasce das variáveis de
bootstrap, uma única vez:

```dotenv
BOOTSTRAP_ADMIN_NAME=Nome do administrador
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=uma-senha-forte-com-10-ou-mais-caracteres
```

Na primeira subida a conta é criada no Supabase Auth e o perfil correspondente
no PostgreSQL. Se já existe um administrador ativo, essas variáveis são
ignoradas — trocar a senha de quem já usa o sistema por variável de ambiente
seria uma porta dos fundos. Depois disso o administrador cria as demais contas
pela página **Alunos**, e a senha temporária aparece uma única vez.

### Login por usuário, não por e-mail

O Supabase Auth autentica por endereço, e a equipe quis manter o login por nome
de usuário. A aplicação resolve isso traduzindo `ana.silva` para
`ana.silva@{SUPABASE_USERNAME_DOMAIN}` antes de falar com o serviço; o aluno
nunca vê esse endereço, e `profiles.username` continua sendo a identidade
visível. Um usuário aceita de 3 a 150 caracteres entre letras, números, ponto,
hífen, sublinhado e arroba, em minúsculas. Contas migradas que guardavam um
e-mail nesse campo continuam entrando com exatamente o que já usavam.

A consequência aceita é que **não há recuperação de senha por e-mail**: como
nenhum endereço é real, ninguém receberia o link. As contas nascem confirmadas
pela Admin API e a redefinição é feita pelo administrador, que gera e entrega
uma senha temporária — o mesmo fluxo que a equipe já usava.

A senha é escolhida livremente: não há exigência de tamanho, letras ou números.
Desativar ou excluir uma conta alcança o Supabase Auth, e não apenas o perfil:
marcar só o perfil deixaria a sessão já emitida válida até expirar sozinha.

### Nomes de variáveis renomeados

Os nomes antigos continuam aceitos para não derrubar ambientes já implantados:

| Nome atual | Nome antigo ainda aceito |
|---|---|
| `BOOTSTRAP_ADMIN_USERNAME` | `BOOTSTRAP_ADMIN_EMAIL` |

Quando as duas estão definidas, a atual vence.

### Streamlit Community Cloud

O Community Cloud **não garante persistência do filesystem local** — foi por
isso que dados criados em um dia sumiram no seguinte, no desenho anterior. Nada
que precise sobreviver a um restart mora lá: o banco é o Supabase PostgreSQL e
os arquivos vão para o Supabase Storage. O disco local só recebe processamento
temporário.

Preencha os Secrets conforme `.streamlit/secrets.toml.example`. Em produção a
aplicação recusa subir sem a configuração completa do Supabase, em vez de
funcionar por um tempo e perder dados depois.

Segredos de produção serão configurados no painel do Streamlit, nunca no Git.

## Navegação e telas móveis

- A interface segue a identidade visual Robonáticos #7565: cabeçalho carvão,
  amarelo e vermelho da equipe, títulos condensados, contornos fortes e sombras
  sólidas. Os logos versionados ficam em `assets/brand/`.
- Sem autenticação, o aplicativo abre diretamente em **Entrar**.
- Depois da autenticação, ela mostra as páginas autorizadas para `student` ou `admin` e acrescenta **Minha conta**.
- **Minha conta** exibe nome, usuário, papel, troca de senha e logout.
- Em viewport móvel de até `768px`, os layouts com várias colunas devem ser empilhados verticalmente.
- Botões, seletores e demais controles interativos devem ter área de toque com pelo menos `44px` de altura.
- Tabelas, imagens e formulários ocupam a largura disponível sem exigir zoom; a barra superior própria quebra seus botões em novas linhas e nunca vira uma gaveta lateral em telas estreitas.

Esses critérios devem ser conferidos tanto em `768px` quanto em uma largura menor representativa de celular antes de uma entrega de interface.

## Documentos e controle de acesso

A mesma caixa de envio aceita imagens, PDF, DOCX e TXT. Duolingo/BeConfident
continua restrito a imagens. PDF textual é extraído diretamente; somente PDF sem
texto carrega renderizador e OCR. DOCX é aberto como pacote Office, rejeitando
macros e objetos incorporados; TXT aceita UTF-8 ou CP-1252 e rejeita conteúdo
binário. `.doc`, HTML, scripts, executáveis, conteúdo com extensão divergente e
PDF acima de `MAX_PDF_PAGES` são recusados.

Além do limite individual, cada submissão respeita `MAX_UPLOAD_FILES` e
`MAX_UPLOAD_TOTAL_BYTES`. DOCX possui orçamento de expansão e taxa de compressão;
PDF digitalizado possui orçamento agregado de pixels antes da renderização. Esses
limites evitam que um usuário autenticado esgote memória ou CPU do servidor.

Cada arquivo recebe UUID, modo `0600`, SHA-256 e registro no banco. Downloads são
resolvidos por ID e passam novamente pela autorização da submissão; caminhos
internos e nomes físicos não são mostrados.

## Persistência de arquivos

GitHub **não é storage nem destino de backup de dados de alunos**, mesmo quando o
repositório é privado. O mecanismo legado `GITHUB_BACKUP_*` está descontinuado e
não deve ser habilitado em novos ambientes.

Na arquitetura oficial, o Streamlit valida e processa temporariamente o arquivo,
envia o binário privado ao Supabase Storage e registra no Supabase somente metadados
e a `storage_key`. Leituras consultam primeiro o registro sob autorização/RLS e só
então acessam o Storage. Consulte [o fluxo completo](docs/ARCHITECTURE.md).

## Lembretes por e-mail

Lembretes começam desativados e `REMINDER_DRY_RUN=true`. A infraestrutura e os
registros existentes permanecem preservados, mas a página **Lembretes** não é
exposta na navegação administrativa. Configurações já existentes continuam sendo
respeitadas pelo processo independente.
Defina `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
`SMTP_FROM_EMAIL`, `SMTP_FROM_NAME` e `SMTP_USE_TLS`. Para um ciclo manual:

```bash
english-leaderboard run-reminders --force
```

Para manter o processo independente do Streamlit:

```bash
english-leaderboard scheduler
```

Cada destinatário/período tem chave única, impedindo duplicidade; falhas transitórias recebem no máximo três
tentativas. Testes e configuração inicial nunca enviam e-mail real.

## Migrações

O schema do PostgreSQL pertence a `supabase/migrations`, aplicado por
`tools/apply_migrations.py`: uma transação por arquivo, o que já foi aplicado é
registrado em `schema_migrations` e pulado na próxima execução.

```bash
python tools/apply_migrations.py --dry-run   # mostra o que falta aplicar
python tools/apply_migrations.py
```

`initialize_database` **não cria tabelas no PostgreSQL** — apenas confere que as
migrações rodaram e recusa subir se faltar alguma. Um `create_all` do ORM
produziria tabelas sem RLS, sem os gatilhos de imutabilidade do ledger e sem as
restrições declaradas no SQL: pareceria certo e deixaria os dados desprotegidos.

Depois de mexer nos modelos, confira que eles ainda batem com o banco:

```bash
python tools/verify_schema_mapping.py
```

## Sincronização automática com Google Sheets

Os dados continuam tendo uma única fonte de verdade: o PostgreSQL e o ledger imutável. Quando habilitado, o Google Sheets recebe um espelho completo das abas `Leaderboard` e `Ledger` depois de cada alteração confirmada. Uma falha do Google gera um aviso, mas não desfaz submissões, aprovações, ajustes ou importações.

Foi usado Google **Sheets**, e não um documento de texto do Google Docs, porque leaderboard e ledger são dados tabulares.

1. No Google Cloud, habilite a Google Sheets API.
2. Crie uma planilha vazia e copie da URL apenas o ID entre `/d/` e `/edit`.
3. Crie uma conta de serviço dedicada. Se usar uma chave JSON local, salve-a em `secrets/google-service-account.json`, nunca no repositório, e restrinja o arquivo (`chmod 600`). Em infraestrutura Google, prefira Application Default Credentials/identidade da carga, sem chave persistente.
4. Compartilhe somente essa planilha, como **Editor**, com o `client_email` da conta de serviço.
5. Configure no `.env`:

```dotenv
GOOGLE_SHEETS_AUTO_SYNC=true
GOOGLE_SHEETS_SPREADSHEET_ID=ID_DA_PLANILHA
GOOGLE_SHEETS_LEADERBOARD_TAB=Leaderboard
GOOGLE_SHEETS_LEDGER_TAB=Ledger
GOOGLE_APPLICATION_CREDENTIALS=./secrets/google-service-account.json
```

6. Reinicie a aplicação. Na página administrativa **Ledger e exportações**, use **Sincronizar agora** para o primeiro espelho e **Abrir planilha** para conferir o resultado. Depois disso, alterações feitas pela aplicação são sincronizadas automaticamente.

A autenticação local e a conta de serviço do Sheets são coisas diferentes. O cliente solicita somente o escopo `spreadsheets`; não precisa de acesso geral ao Drive. Como permissões do Google são concedidas à planilha inteira e a aba `Ledger` contém histórico administrativo, mantenha essa planilha restrita aos administradores.

Para reconciliar manualmente ou por um agendador periódico:

```bash
english-leaderboard sync-google-sheets
```

O comando substitui o snapshot, cria as duas abas se necessário e não duplica linhas. É seguro executá-lo, por exemplo, a cada cinco minutos.

## Importar a planilha legada

Inicialize o banco e execute:

```bash
english-leaderboard import-xlsx "inputs/aulas ingles 7565.xlsx" \
  --namespace aulas_ingles_7565
```

Cada célula numérica com data vira `imported_daily_score`. `x` e vazio são ignorados. Totais por aluno servem apenas para reconciliação e não são somados outra vez. Uma segunda execução com o mesmo namespace deve importar zero e marcar os 81 registros como ignorados; conflitos são relatados sem alterar o ledger.

Se uma planilha possuir apenas colunas nome + total, o importador cria uma transação `initial_balance` por aluno. Cada execução grava um relatório em `import-reports/`, salvo se `--report` indicar outro caminho.

## Testes

```bash
source .venv/bin/activate
pytest
```

A suite padrão não usa rede. Testes com os screenshots do diretório `inputs/` são executados quando os arquivos estão presentes. Para rodar a inferência OCR real explicitamente:

```bash
RUN_OCR_TESTS=1 pytest -m ocr -vv
```

Também é possível inspecionar uma imagem manualmente:

```bash
english-leaderboard analyze-image \
  "inputs/WhatsApp Image 2026-08-13 at 23.21.52.jpeg"
```

## Streamlit Community Cloud

Ao criar a aplicação no Community Cloud, selecione **Python 3.12** em
**Advanced settings**. O arquivo `packages.txt` instala as bibliotecas nativas
de OpenCV/ONNX (`libgl1`, `libglib2.0-0t64` e `libgomp1`) exigidas pelo
RapidOCR no ambiente Linux.

## Durabilidade e cópias

A durabilidade é responsabilidade dos serviços Supabase: o PostgreSQL tem
backup automático gerenciado e o bucket `student-files` guarda os binários. A
aplicação não mantém cópia própria, e o GitHub **não é storage nem destino de
backup de dados de alunos** — o repositório guarda código, documentação e
configuração sem credenciais.

O que a aplicação garante do seu lado:

- **o ledger é imutável por gatilho no banco.** Correção de pontuação é
  lançamento compensatório, nunca edição do histórico. O papel `authenticated`
  não tem sequer o privilégio de `update` ou `delete` nessa tabela;
- **binário e metadado não podem divergir em silêncio.** Se a gravação dos
  metadados falhar depois do upload, o objeto é removido; se nem a remoção
  funcionar, o caso é registrado em `storage_orphans` para reconciliação;
- **um checksum só pontua uma vez.** A chave primária de `approved_evidence`
  fecha a corrida entre duas aprovações simultâneas do mesmo arquivo.

Antes de qualquer migração destrutiva, use o *dry run* de
`tools/migrate_to_supabase.py` e confira o relatório gerado.

## Estrutura principal

```text
streamlit_app.py                 interface aluno/admin
english_leaderboard/schema.py   modelo de dados, espelhando o PostgreSQL
english_leaderboard/supabase_auth.py login, renovação e contas no Supabase Auth
english_leaderboard/contas.py   conta do Auth e perfil mantidos em passo
english_leaderboard/rls_session.py identidade aplicada por transação, para a RLS valer
english_leaderboard/migrations.py migrações aditivas e repetíveis
english_leaderboard/services.py casos de uso e auditoria
english_leaderboard/rules.py    motor configurável/conservador
english_leaderboard/ocr.py      adaptador RapidOCR local
english_leaderboard/image_processing.py validação, OpenCV e hashes
english_leaderboard/document_processing.py PDF/DOCX/TXT seguros
english_leaderboard/scoring.py  ledger e grupos de cinco
english_leaderboard/reminders.py SMTP, deduplicação e dry-run
english_leaderboard/scheduler.py processo independente de lembretes
english_leaderboard/importer.py importação idempotente
english_leaderboard/exporter.py downloads XLSX
english_leaderboard/google_sheets.py espelho idempotente via Sheets API
docs/ARCHITECTURE.md            arquitetura oficial de produção
docs/                           PRD, SPEC e plano verificável
tests/                          suite offline
```

## Limitações conhecidas

- O login identifica o remetente, não prova a autoria da atividade mostrada no print.
- pHash encontra semelhança visual, mas telas legítimas do mesmo aplicativo são parecidas; por isso nunca rejeita sozinho.
- Regras simples não avaliam qualidade/veracidade de resumos; essas entregas vão para revisão.
- OCR pode falhar em imagens comprimidas ou textos pequenos; baixa confiança vai para revisão.
- Estados aprovados são terminais no MVP. Correções de pontos usam uma nova transação compensatória auditada na página de relatórios; a submissão original não é reescrita.
- Não há recuperação de senha por e-mail: os endereços das contas são internos
  (`usuario@dominio-interno`), então ninguém receberia o link. A redefinição é
  feita pelo administrador.
- O SQLite serve ao desenvolvimento e à suíte de testes, sobre os mesmos
  modelos; produção é sempre Supabase PostgreSQL e Supabase Storage.
- Google Sheets é um espelho eventualmente consistente e administrativo; indisponibilidade externa não altera o ledger local, e o comando de reconciliação refaz o snapshot completo.
