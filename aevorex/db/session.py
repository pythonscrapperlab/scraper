"""
Async SQLAlchemy engine and session factory.

Uses asyncpg driver for PostgreSQL with connection pooling.
"""

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from aevorex.config import settings


def get_database_url() -> str:
    """Construct PostgreSQL async connection string."""
    return f"postgresql+asyncpg://{settings.db_user}:{settings.db_password}@{settings.db_host}:{settings.db_port}/{settings.db_name}"


# Create async engine with connection pooling
engine = create_async_engine(
    get_database_url(),
    echo=settings.db_echo,  # SQL logging
    future=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,  # Verify connections before use
    connect_args={
        "timeout": settings.db_timeout,
        "command_timeout": settings.db_command_timeout,
    },
)

# Session factory for async context manager usage
async_session_maker = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,  # Keep loaded objects in memory after commit
    autoflush=False,  # Manual flush control
    autocommit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency injection for async sessions.
    Usage in FastAPI:
        @app.get("/")
        async def my_route(session: AsyncSession = Depends(get_session)):
            ...
    """
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


async def close_engine() -> None:
    """Close all connections in the pool. Call on app shutdown."""
    await engine.dispose()
