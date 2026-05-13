package com.example.template.unit;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.AfterEach;
import org.robolectric.annotation.Config;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit test template.
 * Place all unit tests in the {pkg}.unit sub-package.
 * Gradle filtering: --tests "com.example.template.unit.*"
 *
 * Key annotations:
 *   @Config(sdk = 31) — activates Robolectric for this test class
 *   @Test — JUnit5 test method
 *
 * NOTE: Do NOT use @RunWith(RobolectricTestRunner.class) — conflicts with JUnit5.
 * NOTE: Do NOT use @ExtendWith(RobolectricExtension.class) — it does not exist.
 *       The android-junit5 Gradle plugin handles the Robolectric bridge automatically.
 */
@Config(sdk = 31)
class ExampleUnitTest {

    @BeforeEach
    void setUp() {
        // Setup before each test
    }

    @AfterEach
    void tearDown() {
        // Cleanup after each test
    }

    @Test
    @DisplayName("addition works correctly")
    void testAddition() {
        assertEquals(4, 2 + 2, "2 + 2 should equal 4");
    }

    @Test
    @DisplayName("string operations work correctly")
    void testStringOperations() {
        String result = "hello" + " " + "world";
        assertTrue(result.contains("hello"));
        assertEquals("hello world", result);
    }
}
