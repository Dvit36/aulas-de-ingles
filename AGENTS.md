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

