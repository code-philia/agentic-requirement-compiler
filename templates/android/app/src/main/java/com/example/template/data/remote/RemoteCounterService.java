package com.example.template.data.remote;

import java.io.IOException;

public interface RemoteCounterService {
    int fetch() throws IOException;
}
