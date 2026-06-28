package com.example.template.unit;

import com.example.template.domain.CounterRepository;
import com.example.template.ui.MainViewModel;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

/*
 * TIER: Unit
 * RUNNER: JUnit 5 (Jupiter) — no Robolectric, no Android framework.
 *
 * WHAT THIS FILE TESTS
 * MainViewModel business logic: state transitions and repository interactions.
 * CounterRepository is mocked so tests are pure JVM and run in milliseconds.
 *
 * WHY JUNIT 5
 * MainViewModel extends ViewModel but has no Android platform dependencies
 * once its CounterRepository is injected. No Robolectric sandbox is needed,
 * so JUnit 5 is the right choice (faster, cleaner assertions, no @RunWith).
 *
 * KEY INFRASTRUCTURE
 * @ExtendWith(InstantTaskExecutorExtension.class) — required for every test
 * class that exercises LiveData. Without it, LiveData.setValue() throws
 * "Cannot invoke setValue on a background thread" in a pure JVM context.
 * See InstantTaskExecutorExtension.java for implementation details.
 *
 * HOW TO EXTEND FOR A NEW VIEWMODEL
 * 1. Create XxxViewModelTest.java in this package.
 * 2. Add @ExtendWith(InstantTaskExecutorExtension.class).
 * 3. Declare a Mockito mock for every interface the ViewModel depends on.
 * 4. For each public ViewModel method write two tests:
 *      a. State test  — assertEquals(expected, vm.getSomeState().getValue())
 *      b. Interaction test — verify(mockDep).expectedCall(args)
 * 5. Cover edge cases: boundary values, guard conditions (e.g. "don't go below 0").
 *
 * HOW TO EXTEND FOR NEW MAINVIEWMODEL METHODS
 * Follow the existing pattern:
 *   - Add a @Test that calls the new method and asserts getValue()
 *   - Add a @Test that verifies the correct repository call was made
 *   - If the method has a guard condition, add a negative-path test
 *     (see testDecrementFloorAtZeroDoesNotSave).
 */
@ExtendWith(InstantTaskExecutorExtension.class)
class MainViewModelTest {

    private CounterRepository mockRepository;
    private MainViewModel viewModel;

    @BeforeEach
    void setUp() {
        mockRepository = mock(CounterRepository.class);
        when(mockRepository.load()).thenReturn(0);
        viewModel = new MainViewModel(mockRepository);
    }

    @Test
    @DisplayName("initial count loads from repository")
    void testInitialCountLoadsFromRepository() {
        when(mockRepository.load()).thenReturn(7);
        MainViewModel vm = new MainViewModel(mockRepository);
        assertEquals(7, vm.getCount().getValue());
    }

    @Test
    @DisplayName("increment increases count by 1")
    void testIncrement() {
        viewModel.increment();
        assertEquals(1, viewModel.getCount().getValue());
    }

    @Test
    @DisplayName("multiple increments accumulate")
    void testMultipleIncrements() {
        viewModel.increment();
        viewModel.increment();
        viewModel.increment();
        assertEquals(3, viewModel.getCount().getValue());
    }

    @Test
    @DisplayName("increment saves updated value to repository")
    void testIncrementSavesToRepository() {
        viewModel.increment();
        viewModel.increment();
        verify(mockRepository).save(1);
        verify(mockRepository).save(2);
    }

    @Test
    @DisplayName("decrement decreases count by 1")
    void testDecrement() {
        viewModel.increment();
        viewModel.increment();
        viewModel.decrement();
        assertEquals(1, viewModel.getCount().getValue());
    }

    @Test
    @DisplayName("decrement does not go below 0 and does not save")
    void testDecrementFloorAtZeroDoesNotSave() {
        viewModel.decrement();
        assertEquals(0, viewModel.getCount().getValue());
        verify(mockRepository, never()).save(anyInt());
    }

    @Test
    @DisplayName("reset sets count to 0 and saves to repository")
    void testReset() {
        viewModel.increment();
        viewModel.increment();
        viewModel.reset();
        assertEquals(0, viewModel.getCount().getValue());
        verify(mockRepository).save(0);
    }
}
