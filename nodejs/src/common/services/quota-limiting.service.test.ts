import { TeamManager } from '~/common/utils/team-manager'
import { RedisPool } from '~/types'

import { QuotaLimiting } from './quota-limiting.service'

describe('QuotaLimiting', () => {
    it('allows ingestion without looking up teams or billing quotas', async () => {
        const acquire = jest.fn(() => {
            throw new Error('Unexpected Redis access')
        })
        const getTeam = jest.fn(() => {
            throw new Error('Unexpected team lookup')
        })
        const service = new QuotaLimiting({ acquire } as unknown as RedisPool, { getTeam } as unknown as TeamManager)

        service.clearCache('events')
        service.clearAllCaches()
        expect(await service.isTeamQuotaLimited(1, 'events')).toBe(false)
        expect(await service.isTeamTokenQuotaLimited('example-token', 'events')).toBe(false)
        expect(acquire).not.toHaveBeenCalled()
        expect(getTeam).not.toHaveBeenCalled()
    })
})
