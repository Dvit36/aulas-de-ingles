# Convergência dos dois fluxos de submissão

**Status:** análise concluída em 3 de setembro de 2026. **Etapa 2 concluída em 4
de setembro de 2026**, na branch `convergencia-pipeline-etapa2`. A Etapa 3 — a
delegação — ainda não começou. O que a Etapa 2 entregou e o que ela mudou neste
documento estão no fim.

## O problema

`english_leaderboard/submission_pipeline.py::processar_envio` e
`english_leaderboard/services.py::submit_evidence` implementam o mesmo fluxo —
validar, analisar, subir ao Storage, gravar metadados. A substituição de um pelo
outro ficou pela metade:

- **Só `submit_evidence` roda em produção** (`streamlit_app.py:1173`).
- **Só `processar_envio` tem testes como pipeline** — 11 deles, em
  `tests/test_submission_pipeline.py`.

A decisão é `submit_evidence` passar a delegar a `processar_envio`. **A direção
importa:** `processar_envio` é a versão testada, `submit_evidence` é a versão
correta. Delegar antes de igualar comportamento apaga regra antifraude sem
deixar nenhum teste vermelho.

## Tabela comparativa

| # | Etapa | `processar_envio` | `submit_evidence` | Certo | Por quê |
|---|---|---|---|---|---|
| 1 | Autorização | nenhuma; confia no `student_id` recebido | `require_active`, exige `role == STUDENT`, exige atividade ativa e não arquivada (`services.py:310-315`) | `submit_evidence` | AGENTS.md: o dono vem da sessão autenticada, nunca da interface. É camada acima do pipeline — quem delegar precisa manter. |
| 2 | Lote: lista vazia | `EnvioRejeitado("Nenhum arquivo enviado")` (`submission_pipeline.py:71-72`) | não checa | `processar_envio` | Sem a checagem, `submit_evidence` cria uma `Submission` órfã com `declared_units=0`. |
| 3 | Lote: arquivo vazio | `EnvioRejeitado` antes de qualquer persistência (`submission_pipeline.py:81`) | só em `salvar_arquivo`, já com a `Submission` criada | `processar_envio` | Falhar antes de persistir é mais barato e não deixa linha pela metade. |
| 4 | Lote: byte máximo por arquivo | checa explicitamente (`submission_pipeline.py:83`) | delegado a `analyze_image_bytes` / `process_document_bytes` | empate | Mesmo efeito. `processar_envio` falha mais cedo. |
| 5 | Roteamento por tipo de arquivo | só extensão: `.jpg/.jpeg/.png/.webp` (`submission_pipeline.py:164`) | extensão **+** `duolingo_beconfident` recusa não-imagem, `code="image_required"` (`services.py:357-361`) | `submit_evidence` | **Regra antifraude.** Sem ela aceita-se PDF como prova de lição. `processar_envio` sequer conhece a atividade. |
| 6 | Nome do arquivo | `arquivo.filename` cru (`submission_pipeline.py:131`) | `_client_filename()`: remove `\x00`, aplica `Path().name`, corta em 255 (`services.py:129`) | `submit_evidence` | Sanitização. O nome vai para `submission_files.filename` e é exibido na tela de revisão. |
| 7 | OCR de imagem | `extract_text` uma vez, e **só se `ocr_engine` foi passado** (`submission_pipeline.py:169`) | cria o engine sob demanda; tenta `original` e, se confiança < 0,60 ou texto < 10 chars, tenta `contrast` e `threshold`, ficando com o melhor (`services.py:500-521`) | `submit_evidence` | Duas coisas distintas: criação preguiçosa do engine e as variantes. Sem as variantes o OCR falha em print escuro ou borrado, que é o caso comum. |
| 8 | OCR de PDF sem texto extraível | repassa o engine que recebeu | chama **sem** engine e, se `file_kind == "pdf"` e o texto vier vazio, **cria o engine** e repete (`services.py:370-381`) | `submit_evidence` | O *fallback* em si já está em `document_processing.py:89` e `processar_envio` o alcança. O que falta é a criação preguiçosa: com `ocr_engine=None`, `processar_envio` devolve PDF escaneado sem texto nenhum, em silêncio. |
| 9 | `OCRExecutionError` | não trata — sobe e derruba o envio inteiro | captura por imagem e usa `OCRResult.empty()` (`services.py:522`) | `submit_evidence` | Falha do motor de OCR não pode invalidar prova válida. |
| 10 | Onde o texto do OCR fica | `submission_files.ocr_text` para imagem **e** documento | imagem → `submission.ocr_text`, via decisão; documento → `submission_files.ocr_text` | `submit_evidence` | É o que as telas e as regras leem hoje. |
| 11 | Metadados gravados | não passa `width`, `height` nem `page_count` | passa os três (`services.py:445-446`, `:479`) | `submit_evidence` | `width`/`height` alimentam a comparação antifraude; `page_count` aparece na revisão. |
| 12 | Duplicidade perceptual | **inexistente** | `_duplicate_candidates` (`services.py:197`) + `_record_duplicate_matches` (`services.py:215`) → linhas em `duplicate_matches` e os `exact_flags`/`similar_flags` que alimentam as regras | `submit_evidence` | O coração do antifraude, e a regra que o painel de comparação de evidências exibe. |
| 13 | Duplicidade de documento | **inexistente** | checksum contra o próprio lote e contra `submission_files`; gera `exact_document_duplicate` com `hard_reject` (`services.py:450-463`, `:547`) | `submit_evidence` | Sem ela, reenviar o mesmo PDF pontua de novo. |
| 14 | Regras, decisão, pontuação, auditoria | nenhuma | `analyze_submission_rules`, `transition_submission`, `_claim_approved_evidence`, `award_approved_submission`, `add_audit` | `submit_evidence` | Camada acima; fica fora do pipeline por desenho. |
| 15 | Idempotência por checksum | via `salvar_arquivo` | via `salvar_arquivo` | empate | Mesma função. |
| 16 | Orçamento de Storage | propaga `StorageBudgetExceeded` | propaga | empate | — |

## A divergência bloqueante: arquivo inválido no meio do lote

Esta não é "um lado tem uma regra a mais". São **semânticas opostas**, e ela
precisa de decisão antes de qualquer linha de código.

- **`processar_envio`** rejeita o arquivo ruim e **segue com os bons**
  (`submission_pipeline.py:121-124`). Só falha se nenhum sobrar.
- **`submit_evidence`** deixa o primeiro inválido **rejeitar a submissão
  inteira** (`services.py:383-417`), com `valid_file_content` FAIL,
  `hard_reject: True`, auditoria `submission_auto_rejected`, e **nada chega ao
  bucket** — os uploads só acontecem depois do bloco `try`.

O comportamento de `processar_envio` está travado em teste, em
`tests/test_submission_pipeline.py:212`:

```python
def test_valid_files_survive_a_rejected_sibling(conexao, opcoes) -> None:
    """Um arquivo ruim no lote não derruba os bons."""
    ...
    assert len(resultado.registrados) == 1
    assert len(resultado.rejeitados) == 1
```

**As duas instruções do plano colidem aqui:** "`submit_evidence` é a versão
correta" e "os testes de `processar_envio` não podem ser afrouxados". Não é
possível honrar as duas. Ou `processar_envio` passa a ser tudo-ou-nada e **esse
teste muda**, ou `submit_evidence` passa a aceitar parcialmente e **a rejeição
dura desaparece** — sem nenhum teste ficando vermelho, que é exatamente o risco
que motivou esta análise.

**Recomendação:** tudo-ou-nada está certo para prova de atividade. Aceitar
parcialmente credita o aluno pelos arquivos bons e descarta o ruim em silêncio,
sem `RuleCheck` e sem auditoria — o revisor nunca fica sabendo. Mas é decisão do
dono do projeto.

**Decidido:** tudo-ou-nada. `processar_envio` rejeita a submissão inteira, e
`test_valid_files_survive_a_rejected_sibling` foi reescrito para travar o
comportamento novo.

> **Correção.** Este parágrafo afirmava que a aceitação parcial era *o único
> ponto da tabela em que portar a regra exige alterar um teste existente*. Não
> era. A divergência **6** também exigia: `test_key_is_always_derived_from_the_`
> `session_owner` travava `filename == "../../etc/passwd.png"`, isto é,
> exatamente o nome cru que a sanitização corrige. Os dois testes foram
> alterados, cada um no commit da sua regra. Nenhum outro precisou.

## Duas correções ao enunciado original

**São 11 testes de pipeline, não 12.** `tests/test_submission_pipeline.py` tem
15 funções `test_`, mas 4 exercitam `tempfiles` e não chamam `processar_envio`.
Os que travam comportamento do pipeline são 11.

**O item "OCR de fallback em PDF sem texto extraível" não é bem o que parecia.**
O fallback já existe dentro de `process_document_bytes`
(`document_processing.py:89`) e `processar_envio` o alcança, desde que receba um
engine. O que falta é a **criação preguiçosa do engine**. Em produção
`submit_evidence` sempre recebe `ocr_engine=engine` (`streamlit_app.py:1180`),
então hoje a diferença não aparece; ela aparece em qualquer outro chamador — a
CLI, um utilitário, um teste.

## Ordem proposta para a Etapa 2

Para cada regra: **escrever o teste antes de portar**. Um commit por regra.

1. **(5)** regra de plataforma — `duolingo_beconfident` só aceita imagem
2. **(13)** duplicidade de documento por checksum
3. **(12)** duplicidade perceptual e os `exact_flags` / `similar_flags`
4. **(7+8)** OCR com variantes e criação preguiçosa do engine
5. **(9)** `OCRExecutionError` tratado por imagem
6. **(6)** sanitização do nome do arquivo
7. **(11)** `width`, `height`, `page_count`
8. **(10)** destino do texto do OCR

As divergências de lote (**2** e **3**) já estão corretas em `processar_envio` e
chegam de graça na delegação. As de camada acima (**1** e **14**) permanecem em
`submit_evidence` e não descem para o pipeline.

## Etapa 3 — a delegação

Só depois de `processar_envio` ter todas as regras. `submit_evidence` mantém a
assinatura pública intacta e passa a delegar. Os 11 testes de `processar_envio`
continuam valendo e não podem ser afastados, marcados como `skip` nem afrouxados
— com a única exceção, se assim for decidido, de
`test_valid_files_survive_a_rejected_sibling`, pelo motivo registrado acima.

## Linha de base

`255 passed, 4 skipped` · `ruff check .` com 78 achados.

Depois da Etapa 2: `274 passed, 4 skipped` · `ruff check .` com **os mesmos 78
achados**. Os 19 testes novos são todos de `tests/test_submission_pipeline.py`.

## O que a Etapa 2 entregou

Um commit por regra, na ordem proposta, com o teste escrito antes do porte.

| Commit | Regra |
|---|---|
| Rejeitar o envio inteiro… | lote parcial → tudo-ou-nada |
| Registrar na docstring… | divergências 1 e 14, documentadas |
| Recusar não-imagem… | 5 |
| Sinalizar documento idêntico… | 13 |
| Comparar imagens por pHash… | 12 |
| Ler a imagem em variantes… | 7 e 8 |
| Tratar falha do motor… | 9 |
| Sanitizar o nome… | 6 |
| Gravar largura, altura…| 11 |
| Guardar na linha do arquivo… | 10 |

### Mudanças de API que a Etapa 3 vai usar

- `processar_envio` recebe `activity_code` (regra 5). Sem ele o roteamento é o
  de antes, por extensão.
- `EnvioRejeitado` carrega `code`, `message`, `details` e `arquivo` do erro de
  validação original. É com eles que `submit_evidence` monta o `RuleCheck` de
  `valid_file_content` sem interpretar o texto da mensagem.
- `ResultadoProcessamento` ganhou `documento_duplicado`, `duplicatas_exatas` e
  `duplicatas_similares`; perdeu `rejeitados`, que sob tudo-ou-nada nunca teria
  conteúdo.
- `_client_filename` saiu de `services.py` e virou `sanitizar_nome` no pipeline.
  `submit_evidence` já a importa de lá: uma cópia só.
- A análise do lote inteiro acontece antes de qualquer upload. A recusa não
  deixa objeto no bucket nem linha no banco.

### Um ponto a resolver na Etapa 3

`ResultadoProcessamento.textos_ocr` guarda `str`, mas
`analyze_submission_rules` recebe `ocr_results` como `OCRResult` — precisa da
confiança, não só do texto. Delegar exige o pipeline devolver os `OCRResult`
das imagens, ou a camada acima perde o dado que hoje usa. As listas de
duplicidade são paralelas **às imagens** do lote na ordem de envio, enquanto
`submit_evidence` hoje reordena imagens antes de documentos: o alinhamento
precisa ser conferido na delegação.
