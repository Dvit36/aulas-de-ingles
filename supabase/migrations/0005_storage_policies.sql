-- Políticas de acesso ao bucket privado student-files.
--
-- Estrutura das chaves:
--     students/{user_id}/uploads/{file_id}.{ext}
--     students/{user_id}/activities/{file_id}.{ext}
--     students/{user_id}/documents/{file_id}.{ext}
--     students/{user_id}/processed/{file_id}.{ext}
--
-- storage.foldername(name) devolve os segmentos de pasta como array, com
-- índice começando em 1. Para este layout:
--     [1] = 'students'   (literal)
--     [2] = UUID do dono <- é este que precisa bater com auth.uid()
--     [3] = categoria
--
-- Errar o índice deixaria qualquer aluno gravar na pasta de qualquer outro,
-- por isso os dois primeiros segmentos são verificados explicitamente.
--
-- As operações passam pela API do Storage com o JWT do aluno; estas políticas
-- é que são avaliadas. A chave de serviço ignora RLS e por isso não é usada
-- em upload nem download de aluno.

create or replace function public.storage_dono_do_path(caminho text)
returns uuid
language sql
immutable
as $$
    select case
        when (storage.foldername(caminho))[1] = 'students'
             and array_length(storage.foldername(caminho), 1) >= 3
             and (storage.foldername(caminho))[2] ~
                 '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        then ((storage.foldername(caminho))[2])::uuid
        else null
    end;
$$;

grant execute on function public.storage_dono_do_path(text) to authenticated;

-- Categoria precisa ser uma das quatro previstas: impede criar pastas soltas.
create or replace function public.storage_categoria_valida(caminho text)
returns boolean
language sql
immutable
as $$
    select (storage.foldername(caminho))[3] in
        ('uploads', 'activities', 'documents', 'processed');
$$;

drop policy if exists "aluno le os proprios arquivos" on storage.objects;
drop policy if exists "aluno envia para a propria pasta" on storage.objects;
drop policy if exists "aluno atualiza os proprios arquivos" on storage.objects;
drop policy if exists "aluno remove os proprios arquivos" on storage.objects;
drop policy if exists "administrador acessa student-files" on storage.objects;

create policy "aluno le os proprios arquivos" on storage.objects
    for select to authenticated
    using (
        bucket_id = 'student-files'
        and public.storage_dono_do_path(name) = auth.uid()
    );

create policy "aluno envia para a propria pasta" on storage.objects
    for insert to authenticated
    with check (
        bucket_id = 'student-files'
        and public.storage_dono_do_path(name) = auth.uid()
        and public.storage_categoria_valida(name)
    );

create policy "aluno atualiza os proprios arquivos" on storage.objects
    for update to authenticated
    using (
        bucket_id = 'student-files'
        and public.storage_dono_do_path(name) = auth.uid()
    )
    with check (
        bucket_id = 'student-files'
        and public.storage_dono_do_path(name) = auth.uid()
        and public.storage_categoria_valida(name)
    );

create policy "aluno remove os proprios arquivos" on storage.objects
    for delete to authenticated
    using (
        bucket_id = 'student-files'
        and public.storage_dono_do_path(name) = auth.uid()
    );

-- O administrador revisa comprovante de qualquer aluno. Política separada e
-- restrita ao bucket: is_admin() lê a tabela protegida de perfis, não um
-- campo editável pelo cliente.
create policy "administrador acessa student-files" on storage.objects
    for all to authenticated
    using (bucket_id = 'student-files' and public.is_admin())
    with check (bucket_id = 'student-files' and public.is_admin());
