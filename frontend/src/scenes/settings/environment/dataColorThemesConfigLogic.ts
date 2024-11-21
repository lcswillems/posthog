import { actions, afterMount, kea, path, reducers, selectors } from 'kea'
import { loaders } from 'kea-loaders'
import api from 'lib/api'

import type { dataColorThemesConfigLogicType } from './dataColorThemesConfigLogicType'

export const dataColorThemesConfigLogic = kea<dataColorThemesConfigLogicType>([
    path(['scenes', 'settings', 'environment', 'dataColorThemesConfigLogic']),
    loaders({
        themes: {
            loadThemes: async () => await api.dataColorThemes.list(),
        },
    }),
    reducers({
        selectedThemeId: [
            null as 'new' | number | null,
            {
                selectTheme: (_, { id }) => id,
            },
        ],
    }),
    selectors({
        selectedTheme: [
            (s) => [s.themes, s.selectedThemeId],
            (themes, selectedThemeId) => {
                if (themes == null || selectedThemeId == null) {
                    return null
                }

                if (selectedThemeId === 'new') {
                    // TODO: better way to detect the posthog default theme - likely is_global and trait
                    const defaultTheme = themes.find((theme) => theme.name.includes('Default'))
                    const { id, ...newTheme } = defaultTheme
                    return newTheme
                }

                return themes.find((theme) => theme.id === selectedThemeId)
            },
        ],
    }),
    actions({
        selectTheme: (id: number | null) => ({ id }),
    }),
    // forms(() => ({
    //     theme: {
    //         defaults: {},
    //         submit: async () => {},
    //     },
    // })),
    afterMount(({ actions }) => {
        actions.loadThemes()
    }),
])
