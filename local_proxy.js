const http = require('node:http');

const host = '127.0.0.1';
const publicPort = 3000;
const authority = `${host}:${publicPort}`;
const webuiPort = 3001;
const importPort = 3012;

function target(request) {
  if (request.headers.host !== authority || !request.url.startsWith('/') || request.url.startsWith('//')) return null;
  const path = request.url.split('?', 1)[0];
  return path === '/chatgpt-import' || path.startsWith('/chatgpt-import/') ? importPort : webuiPort;
}

function endToEnd(headers) {
  const excluded = new Set(['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'proxy-connection', 'transfer-encoding', 'upgrade', 'te', 'trailer']);
  for (const name of (headers.connection || '').split(',')) excluded.add(name.trim().toLowerCase());
  return Object.fromEntries(Object.entries(headers).filter(([name]) => !excluded.has(name)));
}

function upstream(request, port, upgrade = false) {
  const headers = endToEnd(request.headers);
  delete headers.forwarded;
  headers['x-forwarded-for'] = host;
  headers['x-forwarded-host'] = authority;
  headers['x-forwarded-port'] = String(publicPort);
  headers['x-forwarded-proto'] = 'http';
  if (upgrade) {
    headers.connection = 'Upgrade';
    headers.upgrade = 'websocket';
  }
  return http.request({ hostname: host, port, path: request.url, method: request.method, headers });
}

const server = http.createServer((request, response) => {
  const port = target(request);
  if (!port) { response.writeHead(400).end(); return; }
  if (request.url === '/chatgpt-import') {
    response.writeHead(308, { location: '/chatgpt-import/' }).end();
    return;
  }
  const outgoing = upstream(request, port);
  outgoing.on('response', (incoming) => {
    response.writeHead(incoming.statusCode, endToEnd(incoming.headers));
    incoming.on('error', () => response.destroy());
    incoming.pipe(response);
  });
  outgoing.on('error', () => {
    if (response.headersSent) response.destroy();
    else response.writeHead(502).end('Local upstream unavailable');
  });
  request.on('aborted', () => outgoing.destroy());
  response.on('close', () => { if (!response.writableFinished) outgoing.destroy(); });
  request.pipe(outgoing);
});

server.on('upgrade', (request, client, head) => {
  if (target(request) !== webuiPort || request.headers.upgrade?.toLowerCase() !== 'websocket') {
    client.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n');
    return;
  }
  const outgoing = upstream(request, webuiPort, true);
  client.on('error', () => outgoing.destroy());
  client.on('close', () => outgoing.destroy());
  outgoing.on('error', () => client.destroy());
  outgoing.on('response', (response) => {
    response.resume();
    client.end(`HTTP/1.1 ${response.statusCode} ${response.statusMessage}\r\nConnection: close\r\n\r\n`);
  });
  outgoing.on('upgrade', (response, socket, tail) => {
    socket.on('error', () => client.destroy());
    client.on('close', () => socket.destroy());
    const fields = [];
    for (let i = 0; i < response.rawHeaders.length; i += 2) {
      fields.push(`${response.rawHeaders[i]}: ${response.rawHeaders[i + 1]}`);
    }
    client.write(`HTTP/1.1 101 Switching Protocols\r\n${fields.join('\r\n')}\r\n\r\n`);
    if (tail.length) client.write(tail);
    if (head.length) socket.write(head);
    client.pipe(socket).pipe(client);
  });
  outgoing.end();
});

server.requestTimeout = 30 * 60 * 1000;
server.listen(publicPort, host);
