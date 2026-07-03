const fs = require('fs');
const path = require('path');

const {
  getDbPath,
  setDbPath,
  initializeDatabase,
  closeDb,
  resetDatabaseFile,
} = require('./init_db');
const runtime = require('./db_runtime');
const seedDatabase = require('./seed_db');

const DEFAULT_TEST_DB_ROOT = '.arc-test-db';

function sanitizeLabel(label) {
  const normalized = String(label || 'test-suite').trim().toLowerCase();
  return normalized.replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'test-suite';
}

function createRuntimeFacade(dbPath) {
  return {
    dbPath,
    run: runtime.run,
    get: runtime.get,
    all: runtime.all,
    exec: runtime.exec,
    withTransaction: runtime.withTransaction,
    seedDefault: () => seedDatabase(),
  };
}

function removeDirIfEmpty(dirPath) {
  if (!fs.existsSync(dirPath)) {
    return;
  }
  if (fs.readdirSync(dirPath).length === 0) {
    fs.rmdirSync(dirPath);
  }
}

function createTestDatabaseHarness(options = {}) {
  const label = sanitizeLabel(options.label);
  const rootDir = path.resolve(process.cwd(), options.rootDir || DEFAULT_TEST_DB_ROOT);
  const uniqueSuffix = `${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const dbPath = path.join(rootDir, `${label}-${uniqueSuffix}.sqlite`);

  let previousDbPath = null;
  let active = false;

  async function ensureHarnessIsActive() {
    if (!active) {
      throw new Error(
        'Test database harness is not active. Call setup() before using seed/reset/query helpers.',
      );
    }
    await setDbPath(dbPath);
  }

  async function runSeedHook(seedHook) {
    if (typeof seedHook === 'function') {
      await seedHook(createRuntimeFacade(dbPath));
    }
  }

  async function setup() {
    previousDbPath = getDbPath();
    fs.mkdirSync(rootDir, { recursive: true });

    await setDbPath(dbPath);
    await resetDatabaseFile();
    await initializeDatabase();

    active = true;

    if (options.seedDefault === true) {
      await seedDatabase();
    }
    await runSeedHook(options.seed);

    return createRuntimeFacade(dbPath);
  }

  async function seed(seedHook) {
    await ensureHarnessIsActive();
    await runSeedHook(seedHook);
  }

  async function reset(seedHook) {
    await ensureHarnessIsActive();
    await resetDatabaseFile();
    await initializeDatabase();

    if (options.seedDefault === true) {
      await seedDatabase();
    }
    await runSeedHook(seedHook);
  }

  async function cleanup() {
    await closeDb();
    await resetDatabaseFile(dbPath);
    active = false;

    const restorePath = previousDbPath;
    previousDbPath = null;

    if (restorePath) {
      await setDbPath(restorePath);
    }

    removeDirIfEmpty(rootDir);
  }

  return {
    dbPath,
    setup,
    seed,
    reset,
    cleanup,
  };
}

module.exports = {
  DEFAULT_TEST_DB_ROOT,
  createTestDatabaseHarness,
};
