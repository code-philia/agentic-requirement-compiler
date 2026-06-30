const fs = require('fs');
const path = require('path');
const sqlite3 = require('sqlite3').verbose();

const dbPath = path.resolve(process.cwd(), 'database.db');

let db = null;
let initPromise = null;

function getDb() {
  if (!db) {
    db = new sqlite3.Database(dbPath);
  }
  return db;
}

function runStatement(database, sql) {
  return new Promise((resolve, reject) => {
    database.run(sql, (err) => {
      if (err) {
        reject(err);
        return;
      }
      resolve();
    });
  });
}

async function initializeDatabase() {
  if (initPromise) {
    return initPromise;
  }

  const database = getDb();
  initPromise = (async () => {
    await runStatement(database, 'PRAGMA foreign_keys = ON;');

    /**
     * Guide model instructions:
     * 1. Use CREATE TABLE IF NOT EXISTS to create new tables.
     * 2. When adding fields, use ALTER TABLE ... ADD COLUMN ... and guard it with existence checks or tolerant error handling.
     * 3. Keep schema evolution idempotent and centralized in this file.
     */
  })();

  try {
    await initPromise;
  } catch (error) {
    initPromise = null;
    throw error;
  }

  return database;
}

function closeDb() {
  if (!db) {
    initPromise = null;
    return Promise.resolve();
  }

  const currentDb = db;
  db = null;
  initPromise = null;
  return new Promise((resolve, reject) => {
    currentDb.close((err) => {
      if (err) {
        reject(err);
        return;
      }
      resolve();
    });
  });
}

async function resetDatabaseFile() {
  await closeDb();
  if (fs.existsSync(dbPath)) {
    fs.rmSync(dbPath, { force: true });
  }
}

initializeDatabase().catch((error) => {
  console.error('Database initialization failed:', error);
});

module.exports = {
  dbPath,
  getDb,
  initializeDatabase,
  closeDb,
  resetDatabaseFile,
};
