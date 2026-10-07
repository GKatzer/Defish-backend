# prestart.py
import logging
import sys
import time
from sqlalchemy import text, inspect
from database import engine
import models.models  # noqa: F401  (registers the tables on Base.metadata, otherwise create_all creates nothing)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def create_tables():
    """Create tables (not recreate!)"""
    try:
        from database import Base
        logger.info("Creating tables...")
        Base.metadata.create_all(bind=engine)
        logger.info("Tables created!")
    except Exception as e:
        logger.error(f"Error with creating tables: {e}")
        raise

def wait_for_db(max_retries=30, retry_interval=2):
    """Wait for database availability"""
    for i in range(max_retries):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("Database is available!")
            return True
        except Exception as e:
            if i == 0:
                logger.info(f"Waiting for DB...")
            elif i % 5 == 0:
                logger.info(f"Attempt {i+1}/{max_retries}...")
            time.sleep(retry_interval)
    
    logger.error("Cannot connect to DB")
    return False

def check_and_create_tables():
    """Check tables and create if changed"""
    try:
        # Inspector for checking DB structure
        inspector = inspect(engine)
        
        # Check if "users" exists
        if 'users' not in inspector.get_table_names():
            logger.info("'users' not found. Creating all tables...")
            from database import Base
            Base.metadata.create_all(bind=engine)
            logger.info("Database created successfully!")
            return
        
        logger.info("'users' already created. Checking cols...")
        
        # Check cols in 'fish_analyses'
        columns = [col['name'] for col in inspector.get_columns('fish_analyses')]
        
        # Essential 'fish_analyses' cols list
        required_columns = ['image_path', 'processed_image_path', 'total_objects']
        
        # Check and add absent cols
        with engine.begin() as conn:
            for column in required_columns:
                if column not in columns:
                    try:
                        if column == 'total_objects':
                            conn.execute(text(f"ALTER TABLE fish_analyses ADD COLUMN {column} INTEGER DEFAULT 0"))
                        else:
                            conn.execute(text(f"ALTER TABLE fish_analyses ADD COLUMN {column} VARCHAR"))
                        logger.info(f"Added col '{column}'")
                    except Exception as e:
                        logger.warning(f"Cannot add col '{column}': {e}")
        
        logger.info("Structure check complete!")
        
    except Exception as e:
        logger.error(f"Error with checking/adding tables: {e}")
        raise

def main():
    logger.info("Checks before starting FastAPI...")
    
    if not wait_for_db():
        sys.exit(1)

    create_tables()
    
    check_and_create_tables()
    logger.info("Checks completed!")

if __name__ == "__main__":
    main()