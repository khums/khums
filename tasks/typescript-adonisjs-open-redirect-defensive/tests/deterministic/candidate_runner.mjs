// Candidate-side observation only. Private cases and expected results live in pytest.
import { readFileSync } from 'node:fs'
import { IncomingMessage } from 'node:http'
import { Socket } from 'node:net'
import { pathToFileURL } from 'node:url'

const input = JSON.parse(readFileSync(0, 'utf8'))
const moduleUrl = (relative) => pathToFileURL(`${process.cwd()}/build/${relative}`).href

async function observeRedirect() {
  // Use the submitted, freshly built response and router implementations. No
  // destination checks or route interpolation are reimplemented in this runner.
  const { ResponseFactory, RouterFactory } = await import(moduleUrl('factories/main.js'))
  const req = new IncomingMessage(new Socket())
  req.url = input.requestUrl ?? '/'
  req.headers = { ...(input.headers ?? {}) }

  const router = new RouterFactory().create()
  if (input.method === 'route') {
    router.get(input.pattern, () => {}).as(input.identifier)
  }
  if (input.previousRoute) {
    router.get(input.previousRoute.pattern, () => {}).as(input.previousRoute.identifier)
  }
  router.commit()
  if (input.previousRoute) {
    const { identifier, params, options } = input.previousRoute
    req.headers.referer = router.urlBuilder.urlFor(identifier, params, options)
  }

  const response = new ResponseFactory()
    .merge({
      req,
      router,
      config: {
        redirect: {
          allowedHosts: input.allowedHosts ?? [],
          forwardQueryString: input.forwardQueryString ?? false,
        },
      },
    })
    .create()

  if (input.method === 'location') {
    response.location(input.target)
  } else if (input.method === 'header') {
    response.header('Location', input.target)
  } else if (input.method === 'shortcut') {
    response.redirect(input.target, input.forwardQueryString ?? false, input.status ?? 302)
  } else {
    const redirect = response.redirect()
    if (input.status !== undefined) redirect.status(input.status)
    if (input.query !== undefined) redirect.withQs(input.query)
    for (const action of input.queryActions ?? []) {
      if (action.operation === 'withQs') {
        redirect.withQs(...(action.args ?? []))
      } else if (action.operation === 'clearQs') {
        redirect.clearQs()
      } else {
        throw new Error('Unsupported query control operation')
      }
    }
    switch (input.method) {
      case 'path':
        redirect.toPath(input.target)
        break
      case 'route':
        redirect.toRoute(input.identifier, input.params, input.options)
        break
      case 'back':
        redirect.back(input.fallback)
        break
      default:
        throw new Error('Unsupported redirect observation method')
    }
  }

  return { location: response.getHeader('location') ?? null, status: response.getStatus() }
}

try {
  if (input.operation === 'redirect') {
    process.stdout.write(JSON.stringify(await observeRedirect()))
  } else {
    const { createURL } = await import(moduleUrl('src/client/url_builder.js'))
    const value = createURL(
      input.pattern,
      input.tokens,
      () => input.query ?? '',
      input.params,
      input.options
    )
    process.stdout.write(JSON.stringify({ value }))
  }
} catch (error) {
  process.stdout.write(JSON.stringify({ error: error?.message ?? String(error) }))
}
