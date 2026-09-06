-- Reconciliação entre o perfil e a conta de acesso.
--
-- A tela de gestão lê `public.profiles`; o login lê `auth.users`. Quando os
-- dois discordam nada estoura: o administrador vê o valor do perfil e o aluno
-- é recusado no login, sem ninguém ligar uma coisa à outra. Esta função existe
-- para o desencontro ser visível na tela, e não numa mensagem de "usuário ou
-- senha incorretos".
--
-- Por que SECURITY DEFINER, como `is_admin()`: a aplicação fala com o banco
-- por conexão direta, mas troca a transação para `authenticated` (ver
-- `english_leaderboard/rls_session.py`), e esse papel não enxerga o schema
-- `auth` — nem deve. A função roda como dono e devolve só o recorte abaixo.
--
-- ============================ ATENÇÃO ============================
-- Ao receber `permission denied for table users`, o PostgreSQL sugere:
--
--     GRANT SELECT ON auth.users TO authenticated;
--
-- NÃO faça isso. `auth.users` guarda `encrypted_password`,
-- `confirmation_token`, `recovery_token` e `email_change_token_*`. Aquele
-- grant entregaria todos eles a qualquer aluno logado, porque `auth.users`
-- não tem RLS que separe uma linha da outra. O HINT do banco está errado para
-- este caso.
--
-- Esta função é a alternativa: expõe CINCO colunas e nenhuma outra —
-- id, username, active, email, banned_until. Nenhuma delas é segredo: o
-- endereço é derivado do username, e `banned_until` é estado de acesso. Se um
-- dia alguém precisar de mais uma coluna daqui, ela entra por nome, uma a
-- uma, e nunca por `select *` ou `returns setof auth.users`.
-- =================================================================
create or replace function public.contas_fora_de_sincronia()
returns table (
    id            uuid,
    username      text,
    active        boolean,
    email         text,
    banned_until  timestamptz
)
language sql
stable
security definer
set search_path = public, auth, pg_temp
as $$
    -- `is_admin()` por dentro: a função é executável por `authenticated`, e
    -- sem esta linha qualquer aluno logado leria o endereço de todo mundo.
    -- A autorização da aplicação não basta sozinha — o AGENTS.md pede as duas
    -- camadas, e esta é a do banco.
    select p.id,
           p.username,
           p.active,
           u.email::text,
           u.banned_until
    from public.profiles p
    left join auth.users u on u.id = p.id
    where public.is_admin()
    order by p.username;
$$;

revoke execute on function public.contas_fora_de_sincronia() from public;
grant execute on function public.contas_fora_de_sincronia() to authenticated;
