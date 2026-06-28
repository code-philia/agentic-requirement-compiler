package com.example.template.data.local;

import androidx.room.Dao;
import androidx.room.Insert;
import androidx.room.OnConflictStrategy;
import androidx.room.Query;

@Dao
public interface CounterDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    void save(Counter counter);

    @Query("SELECT * FROM counter WHERE id = 1")
    Counter get();
}
