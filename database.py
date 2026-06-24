from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from config import settings


_async_url = settings.async_database_url

# asyncpg (Postgres/Neon) needs SSL supplied here rather than via the URL's
# libpq `sslmode` param (which it rejects). `statement_cache_size=0` keeps the
# app compatible with Neon's pooled (`-pooler`) endpoint, where server-side
# prepared statements collide under PgBouncer. Harmless on the direct endpoint.
_connect_args: dict = {}
if _async_url.startswith("postgresql+asyncpg"):
    _connect_args = {"ssl": "require", "statement_cache_size": 0}

engine = create_async_engine(
    _async_url,
    echo=(settings.ENVIRONMENT == "development"),
    pool_pre_ping=True,
    pool_recycle=300,   # Neon closes idle connections; recycle well before that
    pool_size=5,
    max_overflow=5,
    connect_args=_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    """FastAPI dependency — yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
