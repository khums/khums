import { IncomingMessage } from 'node:http'
import { Socket } from 'node:net'
import { test } from '@japa/runner'
import { HttpRequestFactory } from '../factories/request.ts'
import { HttpResponseFactory } from '../factories/response.ts'
import { RouterFactory } from '../factories/router.ts'

function host(header: string, trusted: boolean) {
  const req = new IncomingMessage(new Socket())
  req.url = '/auth/continue'
  req.headers.host = 'origin.internal.test:8843'
  req.headers['x-forwarded-host'] = header
  return new HttpRequestFactory().merge({ req, config: { trustProxy: () => trusted } }).create()
}
function response(forward: boolean, url = '/origin?session=keep&flag=1') {
  const req = new IncomingMessage(new Socket())
  req.url = url
  req.headers.host = 'service.internal.test'
  return new HttpResponseFactory()
    .merge({
      req,
      router: new RouterFactory().create(),
      config: { redirect: { allowedHosts: [], forwardQueryString: forward } },
    })
    .create()
}
test.group('Private HTTP contract', () => {
  test('proxy_multi_hop', ({ assert }) => {
    const req = host('first.example.test:8443, second.example.test, third.example.test', true)
    assert.equal(req.host(), 'first.example.test:8443')
    assert.equal(req.hostname(), 'first.example.test')
  })
  test('proxy_untrusted', ({ assert }) => {
    assert.equal(
      host('spoof.example.test, legitimate.example.test', false).host(),
      'origin.internal.test:8843'
    )
  })
  test('proxy_single_host', ({ assert }) => {
    assert.equal(host('first.example.test:8080', true).host(), 'first.example.test:8080')
  })
  test('cookies_multiple_attributes', ({ assert }) => {
    const res = response(false)
    res.send(
      new Response('ok', {
        headers: [
          ['Set-Cookie', 'sid=123; Secure; HttpOnly; Path=/'],
          ['Set-Cookie', 'theme=dark; SameSite=Lax; Path=/account'],
        ],
      })
    )
    assert.deepEqual(res.getHeader('set-cookie'), [
      'sid=123; Secure; HttpOnly; Path=/',
      'theme=dark; SameSite=Lax; Path=/account',
    ])
  })
  test('cookies_append_existing', ({ assert }) => {
    const res = response(false)
    res.append('Set-Cookie', 'framework=live; Path=/')
    res.send(new Response('ok', { headers: [['Set-Cookie', 'native=live; Secure; Path=/']] }))
    assert.deepEqual(res.getHeader('set-cookie'), [
      'framework=live; Path=/',
      'native=live; Secure; Path=/',
    ])
  })
  test('cookies_no_native', ({ assert }) => {
    const res = response(false)
    res.append('Set-Cookie', 'framework=untouched; Path=/')
    res.send(new Response('ok'))
    assert.deepEqual([res.getHeader('set-cookie')].flat(), ['framework=untouched; Path=/'])
  })
  test('query_inherit', ({ assert }) => {
    const res = response(true)
    res.redirect('/target')
    assert.equal(res.getHeader('location'), '/target?session=keep&flag=1')
  })
  test('query_disable', ({ assert }) => {
    const res = response(true)
    res.redirect('/target', false)
    assert.equal(res.getHeader('location'), '/target')
  })
  test('query_enable', ({ assert }) => {
    const res = response(false)
    res.redirect('/target', true)
    assert.equal(res.getHeader('location'), '/target?session=keep&flag=1')
  })
  test('query_override_explicit', ({ assert }) => {
    const res = response(true)
    res.redirect('/target?explicit=yes', false)
    assert.equal(res.getHeader('location'), '/target?explicit=yes')
  })
})
