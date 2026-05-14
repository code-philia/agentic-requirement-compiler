package com.example.template.ui;

import com.example.template.domain.CounterRepository;
import androidx.lifecycle.LiveData;
import androidx.lifecycle.MutableLiveData;
import androidx.lifecycle.ViewModel;

public class MainViewModel extends ViewModel {
    private final MutableLiveData<Integer> count = new MutableLiveData<>(0);
    private final CounterRepository repository;

    public MainViewModel(CounterRepository repository) {
        this.repository = repository;
        count.setValue(repository.load());
    }

    public LiveData<Integer> getCount() {
        return count;
    }

    public void increment() {
        int next = value() + 1;
        count.setValue(next);
        repository.save(next);
    }

    public void decrement() {
        int current = value();
        if (current > 0) {
            count.setValue(current - 1);
            repository.save(current - 1);
        }
    }

    public void reset() {
        count.setValue(0);
        repository.save(0);
    }

    private int value() {
        return count.getValue() != null ? count.getValue() : 0;
    }
}
