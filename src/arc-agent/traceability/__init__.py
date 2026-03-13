from .database import init_db, insert_requirement
from .service import store_all_requirement

# Automatically initialize database schema when the module is imported
init_db()
