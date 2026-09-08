# Instruções de arquitetura do projeto

Estas regras valem para todo o repositório e substituem decisões anteriores de
persistência e autenticação. Toda alteração futura deve preservar esta separação
de responsabilidades.

## Arquitetura oficial

| Tipo de responsabilidade | Serviço oficial |
|---|---|
| Código, configuração, documentação e assets fixos da interface | GitHub |
| Interface, Python, OCR e processamento temporário | Streamlit Cloud |
| Cadastro, login, logout, recuperação e sessões | Supabase Auth |
| Dados estruturados, progresso, resultados e metadados | Supabase PostgreSQL |
| Imagens, PDFs, documentos e demais binários persistentes | Supabase Storage |

Consulte `docs/ARCHITECTURE.md` antes de alterar autenticação, persistência,
uploads, downloads, OCR ou infraestrutura.

## Regras obrigatórias

- Nunca persistir dados ou arquivos de alunos no GitHub.
- Nunca considerar o filesystem do Streamlit como persistente. Arquivos locais
  só podem existir durante o processamento e devem ser removidos ao final,
  inclusive em caso de erro.
- Nunca criar ou validar senhas em tabelas da aplicação. Supabase Auth é a única
  autoridade de autenticação e sessão.
- Usar o UUID de `auth.users.id` como identidade estável. Quando existir um
  perfil de aluno, `profiles.id`/`students.id` deve referenciar esse UUID.
- Guardar no PostgreSQL apenas dados estruturados e metadados. Não usar `bytea`,
  base64 ou blobs para arquivos que pertencem ao Storage.
- Manter o bucket `student-files` privado. Upload e download de aluno usam o
  JWT dele, para as políticas de `storage.objects` valerem; a chave secreta do
  Supabase ignora RLS e não entra nessas operações.
- Não alterar registros do schema `storage` por SQL. Upload, download,
  movimentação e exclusão passam pelas APIs do Supabase Storage.
- Construir chaves do Storage com IDs estáveis, por exemplo
  `students/{user_id}/activities/{file_id}.{ext}`. Não usar nome completo,
  e-mail ou nome de usuário no path.
- Toda consulta ou mutação pertencente a um aluno deve derivar o proprietário da
  sessão autenticada. Nunca confiar em um `student_id` recebido da interface.
- Aplicar autorização na aplicação e Row Level Security no Supabase. Antes de
  ler, alterar, excluir ou assinar uma URL, confirmar a propriedade do registro.
- URLs de arquivos privados devem ser temporárias/presigned. Não gravar URLs
  assinadas no banco; gravar apenas a `storage_key`.
- Segredos nunca entram no Git. Versionar somente exemplos sem valores reais.
- Apagar uma conta é operação de banco, não de interface: a exclusão pela
  tela não funciona em produção, por causa desconhecida. Remova pelo
  Supabase Auth, que leva o perfil por cascade. **Nunca apague só de
  `public.profiles`**: sobra uma conta que ainda autentica e que o detector
  da aba Alunos não consegue ver. Ver `docs/EXCLUSAO_DE_CONTA.md`, que
  reúne os procedimentos manuais de conta.
- Conta presa na tela de troca de senha destrava por SQL:
  `update public.profiles set must_change_password = false where id = '...';`
  Criar conta e redefinir senha marcam essa coluna; a tela a limpa depois
  da troca confirmada pelo Auth.

## Linha de base de qualidade

Hoje: **`ruff check .` com 79 achados** e **287 passed, 4 skipped**.

O número não é meta de zero — são padrões que o projeto aceita, sobretudo
`BLE001` (o `except Exception` que faz `rollback` e chama
`show_operation_error`, repetido em toda tela) e `DTZ011`. O que ele serve para
detectar é **crescimento silencioso**: se subir sem que alguém saiba por quê,
entrou coisa nova junto.

Ele muda com motivo declarado, não por acidente. Foi 78 até a troca obrigatória
de senha, que acrescentou a décima quinta ocorrência do mesmo `except Exception`
das outras catorze telas — silenciar só essa com `noqa` a tornaria a única
marcada, e por isso não foi feito.

O 78 que aparece em `docs/CONVERGENCIA_PIPELINE.md` é registro histórico
daquele trabalho e continua correto para a época. Não atualize aquele número:
atualize este.

## Verificar contra o banco de produção

A aplicação conecta ao PostgreSQL como `postgres` — dono, que **ignora RLS** —,
mas `english_leaderboard/rls_session.py` troca a transação para
`authenticated` com as claims do usuário. Toda consulta disparada por uma tela
roda sob esse papel, não sob o dono.

**Uma consulta verificada como `postgres` não prova nada sobre o que a
aplicação consegue fazer.** Quem for conferir uma consulta contra o banco real
tem de rodá-la sob `rls_session.aplicar_identidade`, com o UUID de um usuário
de verdade. Conectar com `SUPABASE_DB_URL` e executar direto responde outra
pergunta.

Isso já custou uma queda: um diagnóstico que lia `auth.users` foi conferido
como `postgres`, passou, e em produção morreu com `permission denied for table
users` — porque `authenticated` não enxerga o schema `auth`, nem deve.

Duas consequências práticas:

- O papel `authenticated` não alcança o schema `auth`. Precisou de dado de lá?
  Função `SECURITY DEFINER` que confira `is_admin()` por dentro e devolva
  colunas nomeadas, como `public.contas_fora_de_sincronia()`. **Nunca**
  `GRANT SELECT ON auth.users TO authenticated`, que o PostgreSQL sugere no
  `HINT` do erro: a tabela guarda hash de senha e token de recuperação, e não
  tem RLS separando uma linha da outra.
- No PostgreSQL, um comando que falha **aborta a transação inteira**: todo
  comando seguinte morre com `current transaction is aborted`, e capturar a
  exceção em Python não desfaz isso. Consulta acessória — diagnóstico,
  telemetria, qualquer coisa que a tela não precise para funcionar — vai dentro
  de `session.begin_nested()` (SAVEPOINT), para a falha não levar junto quem
  chamou. O SQLite da suíte não reproduz esse comportamento: nenhum teste
  daqui pega essa classe de erro.

## Fluxo obrigatório de upload

1. Identificar o usuário pela sessão do Supabase Auth.
2. Validar formato, MIME, tamanho e limites antes de persistir.
3. Processar/OCR em memória ou arquivo temporário com limpeza garantida.
4. Enviar o binário ao Supabase Storage com chave baseada no UUID do usuário e do
   arquivo.
5. Registrar no Supabase PostgreSQL os metadados e a `storage_key`.
6. Se o registro no banco falhar, remover o objeto recém-enviado ao Storage ou
   registrá-lo para reconciliação. A operação não pode deixar órfãos em silêncio.

## Fluxo obrigatório de leitura

1. Identificar o usuário pela sessão do Supabase Auth.
2. Consultar o registro no Supabase sob RLS/autorização.
3. Obter a `storage_key` somente após confirmar acesso.
4. Baixar no servidor para processamento ou gerar URL presigned curta para
   entrega ao cliente.

## Estado de transição

O repositório contém implementação legada de autenticação local, SQLite,
uploads locais e backup de dados no GitHub. Ela não representa mais a arquitetura
oficial e não deve ser ampliada nem usada como base para novas funcionalidades.
A migração está registrada em `docs/TASKS.md`. Até sua conclusão, não declarar o
deploy de produção como aderente à arquitetura oficial.

