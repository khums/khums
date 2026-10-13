'use strict';

// Fixture-free observation worker. It receives one behavioral input as JSON on
// stdin, exercises the submitted Koa source, and prints only what it observed.
// Case definitions, expected values and pass/fail decisions stay in the trusted
// caller; nothing in this file encodes an expectation.
const http = require('http');
const path = require('path');
const Stream = require('stream');
const { performance } = require('perf_hooks');

function readInput() {
  return new Promise((resolve, reject) => {
    let raw = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => { raw += chunk; });
    process.stdin.on('end', () => {
      try { resolve(JSON.parse(raw)); } catch (err) { reject(err); }
    });
  });
}

function directContext(Koa, input) {
  const app = new Koa();
  app.proxy = Boolean(input.proxy);
  const socket = new Stream.Duplex({ read() {}, write(_c, _e, done) { done(); } });
  socket.remoteAddress = '127.0.0.1';
  const req = Object.assign(Object.create(Stream.Readable.prototype), {
    headers: input.headers || {}, socket, url: '/', method: 'GET',
    httpVersionMajor: input.httpVersionMajor || 1
  });
  const store = {};
  const res = Object.assign(Object.create(Stream.Writable.prototype), {
    socket, statusCode: 404, _headers: store,
    getHeaders: () => store,
    getHeader: k => store[String(k).toLowerCase()],
    setHeader: (k, v) => { store[String(k).toLowerCase()] = v; },
    removeHeader: k => { delete store[String(k).toLowerCase()]; }
  });
  return app.createContext(req, res);
}

function observeDirect(Koa, input) {
  const ctx = directContext(Koa, input);
  if (input.mode === 'request') {
    const started = performance.now();
    const value = ctx.request[input.field];
    return { value: value === undefined ? null : value, defined: value !== undefined,
             elapsed: performance.now() - started };
  }
  ctx.redirect(input.mode === 'back' ? 'back' : input.target, input.alt);
  return { location: ctx.response.get('Location'), body: String(ctx.body),
           type: ctx.response.type, status: ctx.status };
}

function observeHttp(Koa, input) {
  return new Promise((resolve, reject) => {
    const app = new Koa();
    app.proxy = Boolean(input.proxy);
    app.use(ctx => {
      if (input.mode === 'request') {
        const started = performance.now();
        const value = ctx.request[input.field];
        ctx.body = JSON.stringify({ value: value === undefined ? null : value,
                                    elapsed: performance.now() - started });
      } else {
        ctx.redirect(input.mode === 'back' ? 'back' : input.target, input.alt);
      }
    });
    const server = app.listen(0, '127.0.0.1', () => {
      const options = { hostname: '127.0.0.1', port: server.address().port, path: '/',
                        headers: input.headers || {} };
      http.get(options, res => {
        let body = '';
        res.setEncoding('utf8');
        res.on('data', part => { body += part; });
        res.on('end', () => {
          server.close();
          if (input.mode === 'request') {
            try { resolve(JSON.parse(body)); } catch (err) { reject(err); }
          } else {
            resolve({ status: res.statusCode, location: res.headers.location || null,
                      contentType: res.headers['content-type'] || null, body });
          }
        });
      }).on('error', err => { server.close(); reject(err); });
    });
  });
}

readInput()
  .then(async input => {
    const root = input.root || '/testbed';
    const Koa = require(path.join(root, 'lib', 'application.js'));
    const observed = input.transport === 'http' ? await observeHttp(Koa, input) : observeDirect(Koa, input);
    process.stdout.write(JSON.stringify({ observed }));
    process.exit(0);
  })
  .catch(err => {
    process.stdout.write(JSON.stringify({ error: String(err && err.message || err).slice(0, 300) }));
    process.exit(0);
  });
