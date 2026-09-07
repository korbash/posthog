import { act, renderHook } from '@testing-library/react'
import posthog from 'posthog-js'

import { useAdblockDetection } from './useAdblockDetection'

const originalFetch = global.fetch

jest.mock('posthog-js', () => ({
    __esModule: true,
    default: { capture: jest.fn() },
}))

describe('useAdblockDetection', () => {
    beforeEach(() => {
        jest.useFakeTimers()
        delete window.JS_POSTHOG_HOST
        delete (window as any).posthog
        global.fetch = jest.fn()
    })

    afterEach(() => {
        jest.useRealTimers()
        jest.clearAllMocks()
    })

    afterAll(() => {
        global.fetch = originalFetch
    })

    it('returns ok without issuing a probe when internal analytics are disabled', async () => {
        const { result } = renderHook(() => useAdblockDetection(0))

        await act(async () => {
            await jest.runAllTimersAsync()
        })

        expect(result.current).toBe('ok')
        expect(global.fetch).not.toHaveBeenCalled()
        expect(posthog.capture).toHaveBeenCalledWith('onboarding adblock detection completed', { status: 'ok' })
    })

    it('probes the configured analytics host', async () => {
        window.JS_POSTHOG_HOST = 'https://analytics.example.com'
        ;(window as any).posthog = { __loaded: true }
        ;(global.fetch as jest.Mock).mockResolvedValue({})
        const { result } = renderHook(() => useAdblockDetection(0))

        await act(async () => {
            await jest.runAllTimersAsync()
        })

        expect(result.current).toBe('ok')
        expect(global.fetch).toHaveBeenCalledWith('https://analytics.example.com/decide/?v=3', {
            method: 'POST',
            mode: 'no-cors',
            body: JSON.stringify({}),
        })
    })
})
