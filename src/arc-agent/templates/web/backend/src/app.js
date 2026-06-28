const express = require('express');
const cors = require('cors');
const bodyParser = require('body-parser');
const app = express();

// route modules imports

// middleware imports
app.use(cors());
app.use(bodyParser.json());

// initialize database
require('./database/init_db');

// register routes
app.get('/', (req, res) => {
  res.json({ code: 200, message: 'Backend Ready' });
});

module.exports = app;