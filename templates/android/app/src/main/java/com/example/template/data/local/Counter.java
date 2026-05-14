package com.example.template.data.local;

import androidx.room.Entity;
import androidx.room.PrimaryKey;

@Entity(tableName = "counter")
public class Counter {
    @PrimaryKey
    public int id = 1; // singleton row
    public int value;

    public Counter(int value) {
        this.value = value;
    }
}
