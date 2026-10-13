import { IncomingMessage } from 'node:http'
import { Socket } from 'node:net'
import { test } from '@japa/runner'

import { RouterFactory } from '../factories/router.ts'
import { HttpResponseFactory } from '../factories/response.ts'

function createResponse(
  options: { allowedHosts?: string[]; referer?: string; forwardQueryString?: boolean } = {}
) {
  const req = new IncomingMessage(new Socket())
  req.url = '/current'
  req.headers.host = 'app.example.test'
  if (options.referer) {
    req.headers.referer = options.referer
  }

  const router = new RouterFactory().create()
  router.get('/users/:id', () => {}).as('users.show')
  router.commit()

  return new HttpResponseFactory()
    .merge({
      req,
      router,
      config: {
        redirect: {
          allowedHosts: options.allowedHosts ?? [],
          forwardQueryString: options.forwardQueryString ?? false,
        },
      },
    })
    .create()
}

test.group('Public redirect contract', () => {
  test('explicit external destination preserves location with a request host', ({ assert }) => {
    const response = createResponse()
    response.redirect('https://accounts.example.test/login')
    assert.equal(response.getHeader('location'), 'https://accounts.example.test/login')
    assert.equal(response.getStatus(), 302)
  })

  test('fluent external destination preserves status and query', ({ assert }) => {
    const response = createResponse()
    response
      .redirect()
      .status(303)
      .withQs({ flow: 'login' })
      .toPath('https://accounts.example.test/login')
    assert.equal(response.getHeader('location'), 'https://accounts.example.test/login?flow=login')
    assert.equal(response.getStatus(), 303)
  })

  test('route prefix preserves external destination and encoded params', ({ assert }) => {
    const response = createResponse()
    const options = {
      prefixUrl: 'https://accounts.example.test',
      qs: { view: 'full' },
    }
    // @ts-expect-error "Because RoutesList is empty"
    response.redirect().toRoute('users.show', { id: 'blue/red' }, options)
    assert.equal(
      response.getHeader('location'),
      'https://accounts.example.test/users/blue%2Fred?view=full'
    )
    assert.equal(response.getStatus(), 302)
  })

  test('local and same host destinations remain supported', ({ assert }) => {
    for (const destination of ['/dashboard', 'https://app.example.test/dashboard']) {
      const response = createResponse()
      response.redirect().toPath(destination)
      assert.equal(response.getHeader('location'), destination)
      assert.equal(response.getStatus(), 302)
    }
  })

  test('referrer destination rejects an unapproved external host', ({ assert }) => {
    const response = createResponse({ referer: 'https://untrusted.example.test/account' })
    response.redirect().back('/dashboard')
    assert.equal(response.getHeader('location'), '/dashboard')
    assert.equal(response.getStatus(), 302)
  })

  test('referrer destination preserves explicitly allowed host', ({ assert }) => {
    const response = createResponse({
      referer: 'https://accounts.example.test/account',
      allowedHosts: ['accounts.example.test'],
    })
    response.redirect().back('/dashboard')
    assert.equal(response.getHeader('location'), 'https://accounts.example.test/account')
    assert.equal(response.getStatus(), 302)
  })

  test('back destination respects disabled query forwarding', ({ assert }) => {
    const response = createResponse({
      referer: 'https://app.example.test/search?term=alpha',
      forwardQueryString: false,
    })
    response.redirect().back('/fallback')
    assert.equal(response.getHeader('location'), 'https://app.example.test/search')
    assert.equal(response.getStatus(), 302)
  })

  test('back destination honors fluent query controls', ({ assert }) => {
    const referer = 'https://app.example.test/search?term=alpha'
    const disabled = createResponse({ referer, forwardQueryString: true })
    disabled.redirect().withQs(false).back('/fallback')
    assert.equal(disabled.getHeader('location'), 'https://app.example.test/search')

    const cleared = createResponse({ referer, forwardQueryString: true })
    cleared.redirect().withQs({ discarded: 'yes' }).clearQs().back('/fallback')
    assert.equal(cleared.getHeader('location'), 'https://app.example.test/search')

    const custom = createResponse({ referer })
    custom.redirect().withQs({ notice: 'saved' }).back('/fallback')
    assert.equal(custom.getHeader('location'), 'https://app.example.test/search?notice=saved')

    const enabled = createResponse({ referer })
    enabled.redirect().withQs().back('/fallback')
    assert.equal(enabled.getHeader('location'), referer)
  })

  test('encoded local previous destination remains supported', ({ assert }) => {
    for (const forwardQueryString of [false, true]) {
      const response = createResponse({ referer: '/%2Fcatalog?view=full', forwardQueryString })
      response.redirect().back('/fallback')
      const expected = forwardQueryString ? '/%2Fcatalog?view=full' : '/%2Fcatalog'
      assert.equal(response.getHeader('location'), expected)
      assert.equal(response.getStatus(), 302)
    }
  })

  test('direct location setters preserve explicit references', ({ assert }) => {
    for (const target of ['../login', '?view=full', 'https://accounts.example.test/login']) {
      for (const method of ['location', 'header']) {
        const response = createResponse({ allowedHosts: ['referrer.example.test'] })
        if (method === 'location') {
          response.location(target)
        } else {
          response.header('Location', target)
        }
        assert.equal(response.getHeader('location'), target)
        assert.equal(response.getStatus(), 200)
      }
    }
  })
})
