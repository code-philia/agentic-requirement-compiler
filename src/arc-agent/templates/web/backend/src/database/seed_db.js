const { getDb, initializeDatabase, closeDb } = require('./init_db');

/**
 * Guide model instructions:
 * 1. Must use INSERT OR IGNORE or INSERT OR REPLACE to prevent errors when running the script repeatedly.
 * 2. Must define UNIQUE constraints (in init_db.js) to support OR IGNORE logic.
 * 3. Insert data according to foreign key dependency order (parent table before child table).
 */
async function seed() {
  await initializeDatabase();
  const db = getDb();
  db.serialize(() => {
    // [INSERT OR IGNORE statements added by the model as needed here]
  });
}

// Support running directly from the terminal: node src/database/seed_db.js
if (require.main === module) {
  seed()
    .then(() => closeDb())
    .catch((error) => {
      console.error('Database seed failed:', error);
      process.exitCode = 1;
    });
}

module.exports = seed;
