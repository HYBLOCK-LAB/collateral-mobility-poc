import logging
import os

from fastapi import FastAPI, HTTPException
from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

platform = os.environ["PLATFORM"]
database = os.environ["DB_NAME"]
username = os.environ["DB_USER"]
engine = create_engine(
    URL.create(
        "postgresql+psycopg",
        username=username,
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=5432,
        database=database,
    ),
    pool_pre_ping=True,
    connect_args={"connect_timeout": 3},
)

logger = logging.getLogger(__name__)
app = FastAPI(title=f"Model A / {platform}")


@app.get("/health/db")
def database_health() -> dict[str, str]:
    try:
        with engine.connect() as connection:
            actual_database, actual_user, version = connection.execute(
                text("SELECT current_database(), current_user, current_setting('server_version')")
            ).one()
    except SQLAlchemyError:
        logger.warning("Platform %s could not query its PostgreSQL database", platform)
        raise HTTPException(status_code=503, detail="Database unavailable") from None

    if (actual_database, actual_user) != (database, username):
        logger.error("Unexpected database identity for platform %s", platform)
        raise HTTPException(status_code=503, detail="Unexpected database identity")

    return {
        "status": "ok",
        "platform": platform,
        "database": actual_database,
        "postgresql_version": version,
    }
