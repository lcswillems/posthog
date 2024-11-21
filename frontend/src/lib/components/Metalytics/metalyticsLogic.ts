import { connect, kea, path, selectors } from 'kea'
import { loaders } from 'kea-loaders'
import { subscriptions } from 'kea-subscriptions'
import api from 'lib/api'

import { activityForSceneLogic } from '~/layout/navigation-3000/sidepanel/panels/activity/activityForSceneLogic'
import { HogQLQuery, NodeKind } from '~/queries/schema'
import { hogql } from '~/queries/utils'

import type { metalyticsLogicType } from './metalyticsLogicType'
import { membersLogic } from 'scenes/organization/membersLogic'

export const metalyticsLogic = kea<metalyticsLogicType>([
    path(['lib', 'components', 'metalytics', 'metalyticsLogic']),
    connect({
        values: [activityForSceneLogic, ['sceneActivityFilters'], membersLogic, ['members']],
    }),

    selectors({
        instanceId: [
            (s) => [s.sceneActivityFilters],
            (sceneActivityFilters) =>
                sceneActivityFilters
                    ? sceneActivityFilters.item_id
                        ? `${sceneActivityFilters.scope}:${sceneActivityFilters.item_id}`
                        : sceneActivityFilters.scope
                    : null,
        ],

        viewingMember: [
            (s) => [s.instanceId, s.members],
            (instanceId, members) => {
                console.log('members', members)
                console.log('instanceId', instanceId)
                return {}
            },
        ],

        recentUserMembers: [
            (s) => [s.recentUsers, s.members],
            (recentUsers, members) => {
                if (!members || !recentUsers) {
                    return []
                }
                // Filter members whose IDs match the recentUsers array
                return members.filter((member) => recentUsers.includes(member.user.id))
            },
        ],

        recentUsersCount: [
            (s) => [s.recentUserMembers],
            (recentUserMembers) => recentUserMembers.length,
        ]
    }),
    
    loaders(({ values }) => ({
        viewCount: [
            null as number | null,
            {
                loadViewCount: async () => {
                    const query: HogQLQuery = {
                        kind: NodeKind.HogQLQuery,
                        query: hogql`SELECT sum(count) as count
                            FROM app_metrics
                            WHERE app_source = 'metalytics'
                            AND instance_id = ${values.instanceId}`,
                    }

                    const response = await api.query(query)
                    const result = response.results as number[]
                    return result[0]
                },
            },
        ],
        recentUsers: [
            [] as number[],
            {
                loadUsersLast30days: async () => {
                    const query: HogQLQuery = {
                        kind: NodeKind.HogQLQuery,
                        query: hogql`SELECT app_source_id
                            FROM app_metrics
                            WHERE app_source = 'metalytics'
                            AND instance_id = ${values.instanceId}
                            AND timestamp >= NOW() - INTERVAL 30 DAY`,
                    }
        
                    const response = await api.query(query)
                    return response.results as number[]
                },
            },
        ],
    })),

    subscriptions(({ actions }) => ({
        instanceId: async (instanceId) => {
            if (instanceId) {
                actions.loadViewCount()
                actions.loadUsersLast30days()

                await api.create('/api/projects/@current/metalytics/', {
                    metric_name: 'viewed',
                    // metric_kind: 'misc',
                    instance_id: instanceId,

                    // API sets these
                    // app_source: 'internal_metrics',
                    // app_source_id: user.id,
                })
            }
        },
    })),
])
