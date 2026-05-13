package com.example.template.integration;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.AfterEach;
import org.robolectric.annotation.Config;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Integration test template.
 * Place all integration tests in the {pkg}.integration sub-package.
 * Gradle filtering: --tests "com.example.template.integration.*"
 *
 * Uses Robolectric + Room in-memory DB + MockWebServer for testing component interactions.
 * Key annotations: same as unit tests — @Config(sdk = 31) for Robolectric.
 */
@Config(sdk = 31)
class ExampleIntegrationTest {

    @BeforeEach
    void setUp() {
        // Setup: create in-memory DB, start MockWebServer if needed
    }

    @AfterEach
    void tearDown() {
        // Cleanup: close DB, shutdown MockWebServer
    }

    @Test
    @DisplayName("components interact correctly")
    void testComponentInteraction() {
        // Example: test that repository correctly calls DAO and returns data
        // Room.inMemoryDatabaseBuilder(context, AppDatabase.class)
        //     .allowMainThreadQueries().build();
        assertTrue(true, "placeholder — replace with real integration test");
    }
}
