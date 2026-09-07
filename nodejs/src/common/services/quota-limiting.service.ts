import { TeamManager } from '~/common/utils/team-manager'

import { RedisPool } from '../../types'

// subset of resources that we care about in this service
export type QuotaResource =
    | 'events'
    | 'cdp_trigger_events'
    | 'workflow_emails'
    | 'workflow_push'
    | 'workflow_destinations_dispatched'
    | 'logs_mb_ingested'
    | 'metrics_mb_ingested'
    | 'traces_mb_ingested'

export const QUOTA_LIMITER_CACHE_KEY = '@posthog/quota-limits/'

export interface QuotaLimitedToken {
    token: string
    limitedUntil: number
}

export interface QuotaLimitingResult {
    isLimited: boolean
    limitedUntil?: number
}

export class QuotaLimiting {
    constructor(_redisPool: RedisPool, _teamManager: TeamManager) {}

    public isTeamQuotaLimited(_teamId: number, _resource: QuotaResource): Promise<boolean> {
        return Promise.resolve(false)
    }

    public isTeamTokenQuotaLimited(_teamToken: string, _resource: QuotaResource): Promise<boolean> {
        return Promise.resolve(false)
    }

    public clearCache(_resource: QuotaResource): void {}

    public clearAllCaches(): void {}
}
