package com.example.template.data.system;

import android.content.Context;
import android.os.BatteryManager;
import com.example.template.domain.BatteryProvider;

public class AndroidBatteryProvider implements BatteryProvider {
    private final Context context;

    public AndroidBatteryProvider(Context context) {
        this.context = context;
    }

    @Override
    public int getChargePercent() {
        BatteryManager bm = (BatteryManager) context.getSystemService(Context.BATTERY_SERVICE);
        return bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
    }
}
