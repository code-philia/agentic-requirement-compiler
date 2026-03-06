from .database import init_db, insert_requirement
from .traceability_service import store_all_requirement

# Automatically initialize database schema when the module is imported
init_db()
