package com.example.template.data.local;

import com.example.template.domain.CounterRepository;

public class RoomCounterRepository implements CounterRepository {
    private final CounterDao dao;

    public RoomCounterRepository(CounterDao dao) {
        this.dao = dao;
    }

    @Override
    public int load() {
        Counter c = dao.get();
        return c != null ? c.value : 0;
    }

    @Override
    public void save(int value) {
        dao.save(new Counter(value));
    }
}
