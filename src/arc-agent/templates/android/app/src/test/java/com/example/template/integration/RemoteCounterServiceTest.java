package com.example.template.integration;

import com.example.template.data.remote.HttpRemoteCounterService;
import com.example.template.data.remote.RemoteCounterService;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import okhttp3.OkHttpClient;
import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;

import static org.junit.jupiter.api.Assertions.*;

/*
 * TIER: Integration
 * RUNNER: JUnit 5 (Jupiter) — pure JVM, no Robolectric, no Android framework.
 *
 * WHAT THIS FILE TESTS
 * HttpRemoteCounterService request construction, response parsing, and error
 * handling against a real local HTTP server (MockWebServer). Every test gets
 * a fresh server bound to a random port; the server is shut down after each test.
 *
 * WHY JUNIT 5 (NOT JUNIT 4)
 * OkHttp and MockWebServer are pure JVM libraries. No Android Context is
 * needed, so Robolectric is unnecessary and JUnit 5 is the right choice.
 *
 * MOCKWEBSERVER PATTERN
 *   server.enqueue(new MockResponse().setBody("42"))   — enqueue a response
 *   service.fetch()                                     — trigger the HTTP call
 *   RecordedRequest req = server.takeRequest()          — inspect what was sent
 *
 * HOW TO EXTEND FOR NEW API ENDPOINTS
 * For each new method on RemoteCounterService / HttpRemoteCounterService:
 * 1. Success case   — enqueue 200 with a valid body, assert return value.
 * 2. Empty/default  — enqueue 200 with a boundary body (e.g. "0"), assert.
 * 3. Server error   — enqueue 500, assertThrows(IOException.class, ...).
 * 4. Malformed body — enqueue a body that cannot be parsed, assertThrows.
 * 5. (Optional) Timeout — set server.setBodyDelay(...) and configure a
 *    short OkHttpClient timeout to verify the client handles it.
 *
 * HOW TO VERIFY REQUEST DETAILS
 * Use server.takeRequest() after the call to inspect the path, headers, body:
 *   RecordedRequest req = server.takeRequest();
 *   assertEquals("/counter", req.getPath());
 *   assertEquals("Bearer token", req.getHeader("Authorization"));
 */
class RemoteCounterServiceTest {

    private MockWebServer server;
    private RemoteCounterService service;

    @BeforeEach
    void setUp() throws IOException {
        server = new MockWebServer();
        server.start();
        service = new HttpRemoteCounterService(
                new OkHttpClient(),
                "http://localhost:" + server.getPort());
    }

    @AfterEach
    void tearDown() throws IOException {
        server.shutdown();
    }

    @Test
    void fetchReturnsCountFromServer() throws IOException {
        server.enqueue(new MockResponse().setBody("42"));
        assertEquals(42, service.fetch());
    }

    @Test
    void fetchReturnsZeroWhenServerSendsZero() throws IOException {
        server.enqueue(new MockResponse().setBody("0"));
        assertEquals(0, service.fetch());
    }

    @Test
    void serverErrorThrowsIOException() {
        server.enqueue(new MockResponse().setResponseCode(500));
        assertThrows(IOException.class, () -> service.fetch());
    }

    @Test
    void malformedBodyThrowsIOException() {
        server.enqueue(new MockResponse().setBody("not-a-number"));
        assertThrows(IOException.class, () -> service.fetch());
    }
}
