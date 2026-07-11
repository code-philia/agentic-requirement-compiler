package com.example.template.integration;

import com.example.template.data.local.AppDatabase;
import com.example.template.data.local.Counter;
import com.example.template.data.local.CounterDao;
import com.example.template.data.local.RoomCounterRepository;
import com.example.template.domain.CounterRepository;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.annotation.Config;

import android.content.Context;
import androidx.room.Room;

import static org.junit.Assert.*;

/*
 * TIER: Integration
 * RUNNER: JUnit 4 + RobolectricTestRunner — runs on the JVM, no device required.
 *
 * WHAT THIS FILE TESTS
 * CounterDao SQL queries and RoomCounterRepository against a real (in-memory)
 * Room database. Tests verify that the data layer behaves correctly end-to-end:
 * schema, insert/replace semantics, and repository load/save round-trips.
 *
 * WHY JUNIT 4 (NOT JUNIT 5)
 * Robolectric requires Android Context to initialise Room. The only reliable
 * way to obtain a Robolectric-managed Context on the JVM is via
 * RobolectricTestRunner (@RunWith). Robolectric has no JUnit 5 @ExtendWith
 * equivalent as of v4.16 — the JUnit 4 vintage engine (already on the
 * classpath) is the standard path for all Robolectric-backed tests.
 *
 * DATABASE ISOLATION
 * Room.inMemoryDatabaseBuilder() creates a fresh, empty database per test.
 * No file-system paths, no state leakage between tests.
 * Always close the database in @After so the SQLite connection is released.
 *
 * HOW TO EXTEND FOR NEW ENTITIES
 * 1. Add the new @Entity class to AppDatabase.entities.
 * 2. Add its @Dao interface.
 * 3. Add DAO tests here following the pattern:
 *      - insert → get: verify round-trip
 *      - duplicate insert: verify replace/conflict strategy
 *      - empty table: verify null/default handling
 * 4. If you wrap the DAO in a repository, add repository-level tests too.
 *
 * HOW TO EXTEND FOR NEW DAO QUERIES
 * Each new @Query method needs at least:
 *   - a test with data that matches the query
 *   - a test with an empty table (boundary condition)
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 31)
public class CounterRepositoryTest {

    private AppDatabase db;
    private CounterDao dao;
    private CounterRepository repository;

    @Before
    public void setUp() {
        Context context = RuntimeEnvironment.getApplication();
        db = Room.inMemoryDatabaseBuilder(context, AppDatabase.class)
                .allowMainThreadQueries()
                .build();
        dao = db.counterDao();
        repository = new RoomCounterRepository(dao);
    }

    @After
    public void tearDown() {
        db.close();
    }

    // --- DAO-level tests --------------------------------------------------

    @Test
    public void daoSavesAndRetrievesCounter() {
        dao.save(new Counter(42));
        Counter loaded = dao.get();
        assertNotNull(loaded);
        assertEquals(42, loaded.value);
    }

    @Test
    public void daoReplacesExistingRow() {
        dao.save(new Counter(5));
        dao.save(new Counter(99));
        assertEquals(99, dao.get().value);
    }

    // --- Repository-level tests -------------------------------------------

    @Test
    public void repositoryLoadReturnsZeroWhenEmpty() {
        assertEquals(0, repository.load());
    }

    @Test
    public void repositoryPersistsAndLoadsValue() {
        repository.save(7);
        assertEquals(7, repository.load());
    }

    @Test
    public void repositoryOverwritesOnRepeatedSave() {
        repository.save(3);
        repository.save(15);
        assertEquals(15, repository.load());
    }
}
