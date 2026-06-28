package com.example.template.ui;

import android.os.Bundle;
import android.widget.Button;
import android.widget.TextView;
import androidx.appcompat.app.AppCompatActivity;
import androidx.lifecycle.ViewModelProvider;
import com.example.template.CounterApp;
import com.example.template.R;
import com.example.template.domain.CounterRepository;

public class MainActivity extends AppCompatActivity {
    private MainViewModel viewModel;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        CounterRepository repository = ((CounterApp) getApplication()).getRepository();
        viewModel = new ViewModelProvider(this, new MainViewModelFactory(repository))
                .get(MainViewModel.class);

        TextView countDisplay = findViewById(R.id.count_display);
        Button btnIncrement = findViewById(R.id.btn_increment);
        Button btnDecrement = findViewById(R.id.btn_decrement);
        Button btnReset = findViewById(R.id.btn_reset);

        viewModel.getCount().observe(this, count ->
                countDisplay.setText(String.valueOf(count)));
        btnIncrement.setOnClickListener(v -> viewModel.increment());
        btnDecrement.setOnClickListener(v -> viewModel.decrement());
        btnReset.setOnClickListener(v -> viewModel.reset());
    }
}
