package com.example.template;

import android.app.Application;

/*
 * Generic test Application for Robolectric-backed test classes.
 *
 * PURPOSE
 * Provides a bare Application subclass that Robolectric can instantiate for
 * tests that need an Application context without touching a real DB or network.
 *
 * HOW TO USE
 * Add to any Robolectric test class that needs an Application context:
 *
 *   @Config(sdk = 31, application = TestCounterApp.class)
 *
 * HOW TO EXTEND FOR APP-LEVEL DEPENDENCIES
 * Create a subclass in your test package and override Application lifecycle
 * methods to supply test doubles — for example an in-memory Room database:
 *
 *   public class TestMyApp extends TestCounterApp {
 *       @Override
 *       public void onCreate() {
 *           super.onCreate();
 *           // wire up in-memory DB, MockWebServer URL, etc.
 *       }
 *   }
 *
 * IMPORTANT
 * - NEVER call ApplicationProvider.getApplicationContext() in JVM tests — use
 *   RuntimeEnvironment.getApplication() (Robolectric) instead.
 * - NEVER import androidx.test.core.app.ActivityScenario — use
 *   Robolectric.buildActivity(MyActivity.class).create().resume().get().
 */
public class TestCounterApp extends Application {
    // No counter-specific logic here. Extend this class and override
    // onCreate() to provide app-level test infrastructure for your app.
}
