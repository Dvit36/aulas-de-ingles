-- Troca obrigatória de senha enquanto a senha em uso for temporária.
--
-- `create_user_account` e `reset_user_password` geram uma senha de 16
-- caracteres que o administrador entrega em mãos. Até ontem nada obrigava a
-- substituí-la: o docstring de `gerar_senha_temporaria` dizia "trocada no
-- primeiro acesso" e não existia estado nenhum sustentando isso.
--
-- O estado mora aqui, e não em `user_metadata` do Supabase Auth, porque a
-- aplicação monta o ator a partir de `public.profiles` — do Auth ela usa só o
-- `sub` do token. Ler `user_metadata` custaria uma chamada à Admin API, com a
-- chave privilegiada, a cada rerun do Streamlit, só para decidir qual tela
-- mostrar. E `user_metadata` hoje é escrito e nunca lido: estado que tranca
-- acesso não estreia num campo sem leitor.
--
-- `default false` é deliberado: todo perfil que já existe — inclusive o do
-- administrador, criado à mão antes desta coluna — nasce sem obrigação e
-- continua navegando. Não há backfill, e não deve haver. A obrigação passa a
-- ser criada só nos dois pontos que geram senha temporária.
--
-- O nome diz "a senha atual é temporária", e não "é o primeiro acesso", de
-- propósito: um reset feito pelo administrador devolve alguém à mesma
-- situação, e um sinal de primeiro acesso deixaria esse caso de fora.
--
-- ---------------------------------------------------------------------------
-- Isto é uma porteira de fluxo, não uma fronteira de segurança.
--
-- A política `perfil_proprio_edicao` permite que o aluno atualize a própria
-- linha, então em tese ele poderia limpar esta marca por fora da aplicação.
-- Não se ganha nada com isso: o acesso obtido seria o mesmo que a senha
-- temporária já dá, a RLS continua valendo e o papel continua protegido pelo
-- trigger `profiles_protege_papel`.
--
-- Por isso a coluna **não** entra na lista do trigger. O aluno limpa a própria
-- marca de forma legítima, logo depois de trocar a senha — e proibir isso ali
-- o trancaria fora para sempre, que é exatamente o que esta coluna existe para
-- evitar.
-- ---------------------------------------------------------------------------
alter table public.profiles
    add column must_change_password boolean not null default false;

comment on column public.profiles.must_change_password is
    'A senha em uso é temporária e precisa ser trocada antes de navegar. '
    'Marcada ao criar conta e ao redefinir senha; limpa pelo próprio aluno '
    'depois da troca. Para destravar alguém à mão: '
    'update public.profiles set must_change_password = false where id = ''...'';';
