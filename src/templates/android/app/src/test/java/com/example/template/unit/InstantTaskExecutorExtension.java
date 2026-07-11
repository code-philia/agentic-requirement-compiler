package com.example.template.unit;

import androidx.arch.core.executor.ArchTaskExecutor;
import androidx.arch.core.executor.TaskExecutor;
import org.junit.jupiter.api.extension.AfterEachCallback;
import org.junit.jupiter.api.extension.BeforeEachCallback;
import org.junit.jupiter.api.extension.ExtensionContext;

/*
 * JUnit 5 equivalent of AndroidX's InstantTaskExecutorRule (which is JUnit 4 only).
 *
 * PROBLEM SOLVED
 * LiveData.setValue() internally calls ArchTaskExecutor.getInstance().isMainThread()
 * before dispatching. In a pure JVM test there is no Android main looper, so this
 * check throws "Cannot invoke setValue on a background thread".
 *
 * HOW IT WORKS
 * Before each test, the real ArchTaskExecutor is replaced with a synchronous
 * delegate that always reports isMainThread() = true and runs every runnable
 * immediately on the calling thread. After each test the real executor is restored.
 *
 * USAGE
 * Annotate any JUnit 5 test class whose ViewModel or LiveData calls setValue():
 *
 *   @ExtendWith(InstantTaskExecutorExtension.class)
 *   class MyViewModelTest { ... }
 *
 * This extension is not needed for Robolectric tests (JUnit 4) because Robolectric
 * installs its own main-thread emulation.
 */
public class InstantTaskExecutorExtension implements BeforeEachCallback, AfterEachCallback {

    @Override
    public void beforeEach(ExtensionContext context) {
        ArchTaskExecutor.getInstance().setDelegate(new TaskExecutor() {
            @Override public void executeOnDiskIO(Runnable r) { r.run(); }
            @Override public void postToMainThread(Runnable r) { r.run(); }
            @Override public boolean isMainThread() { return true; }
        });
    }

    @Override
    public void afterEach(ExtensionContext context) {
        ArchTaskExecutor.getInstance().setDelegate(null);
    }
}
