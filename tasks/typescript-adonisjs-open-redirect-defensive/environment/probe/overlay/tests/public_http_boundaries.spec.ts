import { IncomingMessage } from 'node:http'
import { Socket } from 'node:net'
import { test } from '@japa/runner'
import { HttpRequestFactory } from '../factories/request.ts'
import { HttpResponseFactory } from '../factories/response.ts'
import { RouterFactory } from '../factories/router.ts'

function requestWithHost(value: string, trusted: boolean) {
  const req = new IncomingMessage(new Socket())
  req.url = '/account'
  req.headers.host = 'internal.example.test'
  req.headers['x-forwarded-host'] = value
  return new HttpRequestFactory()
    .merge({
      req,
      config: { trustProxy: () => trusted },
    })
    .create()
}

function redirectResponse(forward: boolean, url = '/current?token=secret') {
  const req = new IncomingMessage(new Socket())
  req.url = url
  req.headers.host = 'app.example.test'
  const router = new RouterFactory().create()
  return new HttpResponseFactory()
    .merge({
      req,
      router,
      config: { redirect: { allowedHosts: [], forwardQueryString: forward } },
    })
    .create()
}

test.group('Public HTTP boundaries', () => {
  test('trusted proxy selects first forwarded host', ({ assert }) => {
    const request = requestWithHost('primary.example.test, edge.example.test', true)
    assert.equal(request.host(), 'primary.example.test')
    assert.equal(request.hostname(), 'primary.example.test')
  })

  test('untrusted proxy does not override original host', ({ assert }) => {
    const request = requestWithHost('attacker.example.test, edge.example.test', false)
    assert.equal(request.host(), 'internal.example.test')
  })

  test('native response preserves multiple cookies', ({ assert }) => {
    const response = redirectResponse(false)
    response.send(
      new Response('ok', {
        headers: [
          ['Set-Cookie', 'first=1; Path=/'],
          ['Set-Cookie', 'second=2; Path=/'],
        ],
      })
    )
    assert.deepEqual(response.getHeader('set-cookie'), ['first=1; Path=/', 'second=2; Path=/'])
  })

  test('native response retains pre-existing cookies', ({ assert }) => {
    const response = redirectResponse(false)
    response.append('Set-Cookie', 'framework=1; Path=/')
    response.send(new Response('ok', { headers: [['Set-Cookie', 'native=2; Path=/']] }))
    assert.deepEqual(response.getHeader('set-cookie'), ['framework=1; Path=/', 'native=2; Path=/'])
  })

  test('redirect inherits global query-forwarding default', ({ assert }) => {
    const response = redirectResponse(true)
    response.redirect('/landing')
    assert.equal(response.getHeader('location'), '/landing?token=secret')
  })

  test('redirect explicitly disables inherited forwarding', ({ assert }) => {
    const response = redirectResponse(true)
    response.redirect('/landing', false)
    assert.equal(response.getHeader('location'), '/landing')
  })

  test('redirect explicitly enables forwarding', ({ assert }) => {
    const response = redirectResponse(false)
    response.redirect('/landing', true)
    assert.equal(response.getHeader('location'), '/landing?token=secret')
  })
})
