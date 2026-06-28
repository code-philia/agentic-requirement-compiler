package com.example.template.data.local;

import androidx.room.Database;
import androidx.room.RoomDatabase;

@Database(entities = {Counter.class}, version = 1)
public abstract class AppDatabase extends RoomDatabase {
    public abstract CounterDao counterDao();
}
