const sqlite3 = require('sqlite3').verbose();
const path = require('path');

// Database file stored in the root directory
const dbPath = path.resolve(process.cwd(), 'database.db');
const db = new sqlite3.Database(dbPath);

/**
 * Guide model instructions:
 * 1. Use CREATE TABLE IF NOT EXISTS to create new tables.
 * 2. When adding fields, use ALTER TABLE ... ADD COLUMN ... and wrap it in try/catch logic, or check if the field exists via PRAGMA table_info.
 * 3. Always execute within db.serialize to ensure DDL order.
 */
db.serialize(() => {
  // [CREATE TABLE statements added by the model as needed here]
  
  // [ALTER TABLE statements for incremental evolution added by the model as needed here]
});

module.exports = db;