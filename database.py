from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
import os

# DATABASE_URL_INT takes precedence, DATABASE_URL is the fallback.
DATABASE_URL = (
    os.getenv("DATABASE_URL_INT")
    or os.getenv("DATABASE_URL")
    or "postgresql://fishuser:fishpass@postgres:5432/fishguard"
)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# FastAPI dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
