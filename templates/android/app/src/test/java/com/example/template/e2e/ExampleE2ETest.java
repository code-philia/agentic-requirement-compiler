package com.example.template.e2e;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.DisplayName;
import org.robolectric.annotation.Config;

import static org.junit.jupiter.api.Assertions.*;

/**
 * E2E test template.
 * Place all E2E tests in the {pkg}.e2e sub-package.
 * Gradle filtering: --tests "com.example.template.e2e.*"
 *
 * Uses Robolectric + ActivityScenario to test full UI flows.
 * Key annotations: same as unit tests — @Config(sdk = 31) for Robolectric.
 * Use ActivityScenario.launch(MyActivity.class) to simulate Activity lifecycle.
 */
@Config(sdk = 31)
class ExampleE2ETest {

    @Test
    @DisplayName("UI scenario runs correctly end-to-end")
    void testEndToEndScenario() {
        // Example: launch Activity, interact with UI, verify result
        // ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class);
        // scenario.onActivity(activity -> {
        //     // find views, perform clicks, assert state
        // });
        assertTrue(true, "placeholder — replace with real E2E test");
    }
}
