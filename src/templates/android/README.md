# Android Template

This template is a Gradle-based Android application with one `app` module. It uses Java, AppCompat, Material Components, Room, OkHttp, Lifecycle ViewModel/LiveData, JUnit 5, Robolectric, Mockito, MockWebServer, and Room test helpers.

## Project Layout

- `settings.gradle`: Includes the `:app` module and configures repositories.
- `build.gradle`: Root Gradle plugin configuration.
- `app/build.gradle`: Android app module, SDK levels, dependencies, and test setup.
- `app/src/main`: Application code, resources, manifest, and launcher activity.
- `app/src/test`: JVM test suites for unit, integration, and E2E-style Robolectric tests.

## Prerequisites

- JDK compatible with Android Gradle Plugin 8.1.4.
- Android SDK with compile SDK 34 installed.
- A shell that can run the included Gradle wrapper.

## Build

From the template root:

```bash
./gradlew clean
./gradlew assembleDebug
```

The debug APK is generated under:

```text
app/build/outputs/apk/debug/
```

## Run on a Device or Emulator

Start an Android emulator or connect a device, then install the debug build:

```bash
./gradlew installDebug
```

Launch the installed app from the device launcher. The template main activity is:

```text
com.example.template.ui.MainActivity
```

## Tests

Run all JVM-backed tests:

```bash
./gradlew testDebugUnitTest
```

The template places test layers under:

- `app/src/test/java/com/example/template/unit`
- `app/src/test/java/com/example/template/integration`
- `app/src/test/java/com/example/template/e2e`

These tests run on the JVM with Robolectric and related test helpers. The template does not include an `androidTest` instrumentation suite by default.

## Customization Notes

- Package name: `com.example.template`
- Application class: `CounterApp`
- Main activity: `MainActivity`
- Main layout: `app/src/main/res/layout/activity_main.xml`
- App label and resource strings: `app/src/main/res/values/strings.xml`

Keep app wiring centralized through the existing application, repository, ViewModel, and database boundaries so generated features remain testable.
