package com.example.template.e2e;

import com.example.template.ui.MainActivity;
import com.example.template.R;
import com.example.template.TestCounterApp;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.android.controller.ActivityController;
import org.robolectric.annotation.Config;

import android.widget.Button;
import android.widget.TextView;

import static org.junit.Assert.*;

/*
 * TIER: End-to-End
 * RUNNER: JUnit 4 + RobolectricTestRunner — full Activity lifecycle on the JVM.
 *
 * WHAT THIS FILE TESTS
 * MainActivity from the user's perspective: launch the screen, interact with
 * buttons, assert the display updates correctly. No mocking — the full stack
 * (ViewModel → Repository → Room) runs with an in-memory database.
 *
 * WHY JUNIT 4 (NOT JUNIT 5)
 * Robolectric's ActivityController and shadow system require RobolectricTestRunner
 * (@RunWith). Robolectric has no JUnit 5 @ExtendWith equivalent as of v4.16.
 *
 * DATABASE ISOLATION
 * @Config(application = TestCounterApp.class) swaps the Room database for an
 * in-memory instance. Each Robolectric test method runs in its own ClassLoader
 * sandbox, so each test gets a fresh TestCounterApp with a fresh empty database.
 * No manual reset or teardown of DB state is needed.
 *
 * LIFECYCLE MANAGEMENT
 * ActivityController drives the full lifecycle: create → start → resume → (test)
 * → pause → stop → destroy. Always destroy in @After to release resources and
 * trigger ViewModel.onCleared().
 *
 * HOW TO EXTEND FOR NEW SCREENS
 * 1. Add a new test class XxxActivityTest in this package.
 * 2. Annotate @RunWith(RobolectricTestRunner.class) @Config(sdk=31, application=TestCounterApp.class).
 * 3. In @Before: controller = Robolectric.buildActivity(XxxActivity.class)
 * 4. In @After:  controller.pause().stop().destroy()
 * 5. Call controller.create().start().resume().get() to get the Activity instance.
 * 6. Find views with activity.findViewById(R.id.xxx).
 * 7. Simulate user actions with view.performClick() or EditText.setText(...).
 * 8. Assert display state with assertEquals("expected", textView.getText().toString()).
 *
 * HOW TO EXTEND FOR NEW MAINACTIVITY FEATURES
 * Add a @Test that:
 *   - calls launch() to get the activity
 *   - performs the user action (performClick, setText, etc.)
 *   - asserts the resulting UI state
 * Keep each test focused on a single user interaction sequence.
 */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = 31, application = TestCounterApp.class)
public class MainActivityTest {

    private ActivityController<MainActivity> controller;

    @Before
    public void setUp() {
        controller = Robolectric.buildActivity(MainActivity.class);
    }

    @After
    public void tearDown() {
        if (controller != null) {
            controller.pause().stop().destroy();
            controller = null;
        }
    }

    private MainActivity launch() {
        return controller.create().start().resume().get();
    }

    @Test
    public void counterDisplayStartsAtZero() {
        TextView display = launch().findViewById(R.id.count_display);
        assertEquals("0", display.getText().toString());
    }

    @Test
    public void tappingPlusThreeTimesShowsThree() {
        MainActivity activity = launch();
        Button increment = activity.findViewById(R.id.btn_increment);
        TextView display = activity.findViewById(R.id.count_display);

        increment.performClick();
        increment.performClick();
        increment.performClick();

        assertEquals("3", display.getText().toString());
    }

    @Test
    public void tappingMinusAfterPlusPlusShowsOne() {
        MainActivity activity = launch();
        Button increment = activity.findViewById(R.id.btn_increment);
        Button decrement = activity.findViewById(R.id.btn_decrement);
        TextView display = activity.findViewById(R.id.count_display);

        increment.performClick();
        increment.performClick();
        decrement.performClick();

        assertEquals("1", display.getText().toString());
    }

    @Test
    public void decrementAtZeroStaysAtZero() {
        MainActivity activity = launch();
        Button decrement = activity.findViewById(R.id.btn_decrement);
        TextView display = activity.findViewById(R.id.count_display);

        decrement.performClick();

        assertEquals("0", display.getText().toString());
    }

    @Test
    public void resetAfterIncrementsShowsZero() {
        MainActivity activity = launch();
        Button increment = activity.findViewById(R.id.btn_increment);
        Button reset = activity.findViewById(R.id.btn_reset);
        TextView display = activity.findViewById(R.id.count_display);

        increment.performClick();
        increment.performClick();
        increment.performClick();
        reset.performClick();

        assertEquals("0", display.getText().toString());
    }
}
