"""Arquivos temporários com limpeza garantida, para o disco efêmero do Cloud.

O pipeline processa tudo em memória: OCR aceita bytes, PDF e DOCX são abertos
com ``BytesIO``, imagens idem. Este módulo existe para o caso em que uma
biblioteca exige um caminho no disco — e garante que nada sobreviva à
requisição, inclusive quando ela termina em exceção.

Nenhum código pode contar com a existência de um arquivo local depois do fim
do bloco: o filesystem do Streamlit não é persistente.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["arquivo_temporario", "diretorio_temporario"]


@contextmanager
def diretorio_temporario(prefixo: str = "englead-") -> Iterator[Path]:
    """Diretório apagado ao sair do bloco, com sucesso ou com exceção."""

    with tempfile.TemporaryDirectory(prefix=prefixo) as caminho:
        diretorio = Path(caminho)
        os.chmod(diretorio, stat.S_IRWXU)  # 0700
        yield diretorio


@contextmanager
def arquivo_temporario(
    dados: bytes, *, sufixo: str = "", prefixo: str = "englead-"
) -> Iterator[Path]:
    """Grava os bytes em um arquivo 0600 e o remove ao sair do bloco.

    O arquivo nasce dentro de um diretório temporário próprio: assim a
    remoção não depende de o caminho ainda existir nem de ninguém lembrar de
    apagar, e um nome colidido não sobrescreve nada.
    """

    if not dados:
        raise ValueError("Não há conteúdo para gravar no temporário")
    with diretorio_temporario(prefixo) as diretorio:
        destino = diretorio / f"conteudo{sufixo}"
        descritor = os.open(
            destino, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR
        )
        with os.fdopen(descritor, "wb") as arquivo:
            arquivo.write(dados)
            arquivo.flush()
            os.fsync(arquivo.fileno())
        yield destino
