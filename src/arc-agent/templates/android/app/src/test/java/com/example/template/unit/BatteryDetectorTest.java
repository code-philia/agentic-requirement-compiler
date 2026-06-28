package com.example.template.unit;

import com.example.template.domain.BatteryProvider;
import com.example.template.domain.LowBatteryDetector;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

/*
 * TIER: Unit
 * RUNNER: JUnit 5 (Jupiter) — no Robolectric, no Android framework.
 *
 * WHAT THIS FILE TESTS
 * LowBatteryDetector business logic, using a Mockito mock of BatteryProvider.
 * BatteryProvider is the interface that wraps Android's BatteryManager.
 *
 * STRATEGY: Mock the Android system API wrapper (system.md Strategy A)
 * The production stack is:
 *   LowBatteryDetector  →  BatteryProvider  →  AndroidBatteryProvider  →  BatteryManager
 *
 * Tests mock BatteryProvider directly. AndroidBatteryProvider is a thin
 * delegation layer with no logic of its own, so it does not need its own
 * test suite — the interface contract is verified here, and the wrapper is
 * verified at the OS level by device/emulator tests if ever needed.
 *
 * WHY THIS STRATEGY (not Robolectric shadows)
 * Robolectric shadow support is inconsistent across Android API versions and
 * not available for every system service. Wrapping the API behind an interface
 * and mocking that interface is universally applicable: it works for Camera,
 * Sensors, Bluetooth, NFC, Telephony, and anything else in the framework.
 *
 * HOW TO APPLY THIS PATTERN TO A NEW ANDROID SYSTEM API
 * 1. Create an interface in domain/: XxxProvider { int/String/Whatever getX(); }
 * 2. Create data/system/AndroidXxxProvider implements XxxProvider (thin wrapper).
 * 3. Inject XxxProvider into the class that needs the data.
 * 4. Add a XxxTest here using mock(XxxProvider.class) + when/verify.
 * No Context, no BatteryManager, no Robolectric required.
 *
 * HOW TO EXTEND FOR NEW BATTERY BEHAVIOUR
 * Add the new method to LowBatteryDetector, then add:
 *   - a test for the true branch
 *   - a test for the false branch
 *   - a boundary test if applicable
 */
class BatteryDetectorTest {

    private BatteryProvider mockProvider;

    @BeforeEach
    void setUp() {
        mockProvider = mock(BatteryProvider.class);
    }

    @Test
    @DisplayName("isLow returns true when charge is below threshold")
    void isLowReturnsTrueWhenBelowThreshold() {
        when(mockProvider.getChargePercent()).thenReturn(15);
        assertTrue(new LowBatteryDetector(mockProvider).isLow());
    }

    @Test
    @DisplayName("isLow returns false when charge is above threshold")
    void isLowReturnsFalseWhenAboveThreshold() {
        when(mockProvider.getChargePercent()).thenReturn(50);
        assertFalse(new LowBatteryDetector(mockProvider).isLow());
    }

    @Test
    @DisplayName("isLow returns false at exact threshold (boundary)")
    void isLowReturnsFalseAtExactThreshold() {
        when(mockProvider.getChargePercent()).thenReturn(LowBatteryDetector.LOW_THRESHOLD);
        assertFalse(new LowBatteryDetector(mockProvider).isLow());
    }

    @Test
    @DisplayName("provider is queried exactly once per isLow call")
    void providerCalledOnce() {
        when(mockProvider.getChargePercent()).thenReturn(10);
        new LowBatteryDetector(mockProvider).isLow();
        verify(mockProvider, times(1)).getChargePercent();
    }
}
