package com.example.template;

import android.app.Application;
import androidx.room.Room;
import com.example.template.data.local.AppDatabase;
import com.example.template.data.local.RoomCounterRepository;
import com.example.template.domain.CounterRepository;

public class CounterApp extends Application {
    private AppDatabase database;
    private CounterRepository repository;

    @Override
    public void onCreate() {
        super.onCreate();
        database = buildDatabase();
        repository = new RoomCounterRepository(database.counterDao());
    }

    // Overrideable in tests to swap in an in-memory database.
    protected AppDatabase buildDatabase() {
        return Room.databaseBuilder(this, AppDatabase.class, "counter-db")
                .allowMainThreadQueries()
                .build();
    }

    public CounterRepository getRepository() {
        return repository;
    }
}
