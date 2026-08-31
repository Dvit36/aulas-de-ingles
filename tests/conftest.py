from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, PngImagePlugin
from sqlalchemy import select

from english_leaderboard.catalog import seed_catalog
from english_leaderboard.config import Settings
from english_leaderboard.database import (
    create_database_engine,
    create_session_factory,
    initialize_database,
)
from english_leaderboard.schema import Role, User, new_id
from english_leaderboard.storage import StorageError


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        seed_fake_data=False,
        database_url="sqlite+pysqlite:///:memory:",
        max_upload_bytes=5 * 1024 * 1024,
        min_image_width=320,
        min_image_height=320,
        min_laplacian_variance=10.0,
        phash_distance_threshold=4,
        auto_approve_confidence=0.85,
        summary_min_chars=120,
    )


@pytest.fixture
def session(settings: Settings):
    engine = create_database_engine(settings.database_url)
    initialize_database(engine)
    factory = create_session_factory(engine)
    db = factory()
    seed_catalog(db)
    # O id do perfil é o UUID de auth.users: não há default, ele vem de fora.
    admin = User(
        id=new_id(),
        username="admin",
        display_name="Admin",
        role=Role.ADMIN,
        active=True,
    )
    student = User(
        id=new_id(),
        username="student",
        display_name="Student",
        role=Role.STUDENT,
        active=True,
    )
    db.add_all([admin, student])
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def users(session):
    return {
        user.role: user
        for user in session.scalars(
            select(User).where(User.username.in_(["admin", "student"]))
        ).all()
    }


class FakeDuolingoOCR:
    def __call__(self, _source):
        return (
            [
                [None, "Lição concluída!", 0.99],
                [None, "Receber XP", 0.98],
                [None, "Combo x51", 0.97],
            ],
            [0.01, 0.01, 0.01],
        )


class FakeBeConfidentOCR:
    def __call__(self, _source):
        return (
            [
                [None, "Atividade concluída", 0.99],
                [None, "Calculando pontuação geral", 0.96],
            ],
            [0.01, 0.01, 0.01],
        )


def make_png(seed: int = 1, *, metadata: str | None = None) -> bytes:
    image = Image.new("RGB", (480, 800), "white")
    draw = ImageDraw.Draw(image)
    for index in range(0, 800, 24):
        color = ((index + seed * 17) % 255, (index * 3 + 70) % 255, (index * 7 + 20) % 255)
        draw.line((0, index, 480, 800 - index // 2), fill=color, width=3)
    draw.rectangle((35, 240, 445, 560), fill=(25, 170, 80), outline="black", width=4)
    draw.text((80, 360), f"Licao concluida {seed}", fill="white")
    output = BytesIO()
    pnginfo = None
    if metadata is not None:
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_text("variant", metadata)
    image.save(output, format="PNG", pnginfo=pnginfo)
    return output.getvalue()


class GatewayFalso:
    """Bucket em memória: exercita o fluxo real de Storage sem rede.

    Os testes precisam do caminho completo — enviar, registrar metadados,
    compensar em caso de falha —, e um duplo é o que permite isso sem uma
    conta do Supabase.
    """

    def __init__(self) -> None:
        self.objetos: dict[str, bytes] = {}
        self.falhar_em: set[str] = set()

    def upload(self, key: str, data: bytes, content_type: str) -> None:
        if "upload" in self.falhar_em:
            raise StorageError("falha simulada no envio")
        self.objetos[key] = data

    def download(self, key: str) -> bytes:
        if key not in self.objetos:
            raise StorageError(f"objeto ausente: {key}")
        return self.objetos[key]

    def remove(self, key: str) -> None:
        if "remove" in self.falhar_em:
            raise StorageError("falha simulada na remoção")
        self.objetos.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.objetos

    def signed_url(self, key: str, expires_in: int) -> str:
        return f"https://exemplo.invalid/{key}?expira={expires_in}"


@pytest.fixture
def gateway() -> GatewayFalso:
    return GatewayFalso()
