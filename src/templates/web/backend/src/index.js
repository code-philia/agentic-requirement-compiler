const app = require('./app');
const defaultPort = __ARC_WEB_PORT__;
const port = Number(process.env.PORT || defaultPort);

app.listen(port, () => {
  console.log(`Backend listening at http://127.0.0.1:${port}`);
});
