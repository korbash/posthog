import posthog from 'posthog-js'

import { loadPostHogJS } from './loadPostHogJS'

jest.mock('posthog-js', () => ({
    __esModule: true,
    default: {
        get_session_id: jest.fn(),
        init: jest.fn(),
    },
}))

describe('loadPostHogJS', () => {
    beforeEach(() => {
        delete window.JS_POSTHOG_API_KEY
        jest.clearAllMocks()
    })

    it('initializes a local no-op client when analytics are disabled', () => {
        loadPostHogJS()

        expect(posthog.init).toHaveBeenCalledWith(
            'fake_token',
            expect.objectContaining({
                api_host: window.location.origin,
                advanced_disable_decide: true,
                autocapture: false,
                disable_external_dependency_loading: true,
                opt_out_capturing_by_default: true,
            })
        )
    })
})
