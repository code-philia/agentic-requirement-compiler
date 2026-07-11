package com.example.template.domain;

// Business logic that depends on BatteryProvider through an interface,
// making it testable without Android framework or BatteryManager.
public class LowBatteryDetector {
    public static final int LOW_THRESHOLD = 20;
    private final BatteryProvider provider;

    public LowBatteryDetector(BatteryProvider provider) {
        this.provider = provider;
    }

    public boolean isLow() {
        return provider.getChargePercent() < LOW_THRESHOLD;
    }
}
