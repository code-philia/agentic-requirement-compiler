package com.example.template;

import androidx.room.Room;
import com.example.template.data.local.AppDatabase;

/*
 * Test Application used by Robolectric-backed test classes.
 *
 * PURPOSE
 * Overrides CounterApp.buildDatabase() to return an in-memory Room database
 * instead of a file-backed one. This eliminates file-system path issues that
 * arise when Robolectric shadows Context.getDatabasePath() across test methods,
 * and prevents persistent state from leaking between test runs.
 *
 * HOW IT PROVIDES ISOLATION
 * Robolectric runs each test method in its own ClassLoader sandbox. A fresh
 * TestCounterApp instance is created per sandbox, so each test starts with an
 * empty in-memory database — no manual reset or @Before teardown required.
 *
 * HOW TO USE
 * Add to any Robolectric test class that exercises code going through the
 * Application (ViewModel → CounterApp.getRepository() → Room):
 *
 *   @Config(sdk = 31, application = TestCounterApp.class)
 *
 * Test classes that do NOT go through the Application (e.g. CounterRepositoryTest,
 * which builds its own in-memory DB directly) do not need this annotation.
 *
 * HOW TO EXTEND FOR NEW APP-LEVEL DEPENDENCIES
 * If CounterApp gains additional services (e.g. a network client, a cache),
 * override the corresponding factory method here and return a deterministic
 * test double — an in-memory store, a MockWebServer URL, etc.
 */
public class TestCounterApp extends CounterApp {

    @Override
    protected AppDatabase buildDatabase() {
        return Room.inMemoryDatabaseBuilder(this, AppDatabase.class)
                .allowMainThreadQueries()
                .build();
    }
}
