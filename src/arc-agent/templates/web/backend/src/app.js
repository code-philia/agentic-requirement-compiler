const express = require('express');
const cors = require('cors');
const bodyParser = require('body-parser');
const fs = require('fs');
const path = require('path');
const app = express();

// route modules imports

// middleware imports
app.use(cors());
app.use(bodyParser.json());

// initialize database
require('./database/init_db');

// register routes
app.get('/api/health', (req, res) => {
  res.json({ code: 200, message: 'Backend Ready' });
});

const frontendDistPath = path.resolve(__dirname, '../../frontend/dist');

if (fs.existsSync(frontendDistPath)) {
  app.use(express.static(frontendDistPath));

  // Keep API routes on the backend and serve the SPA for all other GET requests.
  app.get(/^(?!\/api(?:\/|$)).*/, (req, res) => {
    res.sendFile(path.join(frontendDistPath, 'index.html'));
  });
} else {
  app.get('/', (req, res) => {
    res.json({ code: 200, message: 'Backend Ready. Frontend build not found yet.' });
  });
}

module.exports = app;
