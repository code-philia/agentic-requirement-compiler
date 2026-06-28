package com.example.template.domain;

public interface CounterRepository {
    int load();
    void save(int value);
}
