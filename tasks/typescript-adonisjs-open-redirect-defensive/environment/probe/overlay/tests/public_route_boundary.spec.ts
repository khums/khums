import { test } from '@japa/runner'
import { AppFactory } from '@adonisjs/application/factories'
import { EncryptionFactory } from '@boringnode/encryption/factories'

import { RouterFactory } from '../factories/router.ts'
import { URLBuilderFactory } from '../factories/url_builder_factory.ts'
import { HttpResponseFactory } from '../factories/response.ts'

test.group('Public route boundary', () => {
  test('generated navigation preserves path and query structure', ({ assert }) => {
    const router = new RouterFactory().create()
    const { urlFor } = new URLBuilderFactory<{
      ALL: {
        'members.show': {
          params: { team: string; member: string }
          paramsTuple: [string, string]
        }
      }
    }>()
      .merge({ router })
      .create()

    router.route('/teams/:team/members/:member', ['GET'], () => {}).as('members.show')
    router.commit()

    assert.equal(
      urlFor('members.show', { team: 'blue/red', member: 'café' }, { qs: { view: 'full' } }),
      '/teams/blue%2Fred/members/caf%C3%A9?view=full'
    )
  })

  test('redirect preserves query with a route value', ({ assert }) => {
    const app = new AppFactory().create(new URL('./app/', import.meta.url), () => {})
    const encryption = new EncryptionFactory().create()
    const router = new RouterFactory().merge({ app, encryption }).create()
    router.get('/teams/:id', 'TeamsController.show').as('teams.show')
    router.commit()

    const response = new HttpResponseFactory().merge({ encryption, router }).create()
    // @ts-expect-error "Because RoutesList is empty"
    response.redirect().toRoute('teams.show', { id: 'blue/red' }, { qs: { view: 'full' } })
    assert.equal(response.getHeader('location'), '/teams/blue%2Fred?view=full')
  })
})
