package com.example.template.domain;

public class CounterManager {
    private int count;

    public CounterManager() {
        this.count = 0;
    }

    public CounterManager(int initialCount) {
        this.count = initialCount;
    }

    public void increment() {
        count++;
    }

    public void decrement() {
        if (count > 0) count--;
    }

    public void reset() {
        count = 0;
    }

    public int getCount() {
        return count;
    }
}
