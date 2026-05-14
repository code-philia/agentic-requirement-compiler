package com.example.template.data.remote;

import java.io.IOException;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

// MVP only — use async calls (coroutines / RxJava) in production.
public class HttpRemoteCounterService implements RemoteCounterService {
    private final OkHttpClient client;
    private final String url;

    public HttpRemoteCounterService(OkHttpClient client, String baseUrl) {
        this.client = client;
        this.url = baseUrl.replaceAll("/$", "") + "/counter";
    }

    @Override
    public int fetch() throws IOException {
        Request request = new Request.Builder().url(url).build();
        try (Response response = client.newCall(request).execute()) {
            if (!response.isSuccessful()) {
                throw new IOException("HTTP " + response.code());
            }
            String body = response.body().string().trim();
            try {
                return Integer.parseInt(body);
            } catch (NumberFormatException e) {
                throw new IOException("unexpected body: " + body, e);
            }
        }
    }
}
