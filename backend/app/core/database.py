import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.engine.url import URL
from fastapi import HTTPException, status

import core.config as core_config
import core.logger as core_logger

# Define the database connection URL using environment variables
db_url = URL.create(
    drivername="postgresql+psycopg",
    username=os.environ.get("DB_USER", "endurain"),
    password=core_config.read_secret("DB_PASSWORD"),
    host=os.environ.get("DB_HOST", "postgres"),
    port=os.environ.get("DB_PORT", "5432"),
    database=os.environ.get("DB_DATABASE", "endurain"),
)

# Create the SQLAlchemy engine
engine = create_engine(
    db_url,
    pool_size=20,
    max_overflow=40,
    pool_timeout=180,
    pool_recycle=3600,
    pool_pre_ping=True,
)

# Create a session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create a base class for declarative models
Base = declarative_base()


def get_db():
    """
    Yields a new SQLAlchemy database session.

    This generator function creates a new database session using SessionLocal,
    yields it for use in database operations, and ensures the session is properly
    closed after use. Intended for use as a dependency in FastAPI routes or other
    contexts where session management is required.

    Yields:
        Session: An active SQLAlchemy database session.

    Raises:
        HTTPException: If database connection fails.
    """
    try:
        # Create a new database session and return it
        db = SessionLocal()
    except Exception as err:
        core_logger.print_to_log(
            f"Database connection failed: {err}",
            "error",
            exc=err,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection failed",
        ) from err

    try:
        # Yield the database session
        yield db
    finally:
        # Close the database session
        db.close()
