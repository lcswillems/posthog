import { UUIDT } from '../../src/utils/utils'
import { capture, createOrganization, createTeam, fetchEvents } from '../api'
import { waitForExpect } from '../expectations'

let organizationId: string

beforeAll(async () => {
    organizationId = await createOrganization()
})

test.concurrent(`event ingestion: can ingest into the historical topic`, async () => {
    const teamId = await createTeam(organizationId)
    const distinctId = new UUIDT().toString()

    const groupIdentityUuid = new UUIDT().toString()
    await capture({
        teamId,
        distinctId,
        uuid: groupIdentityUuid,
        event: '$groupidentify',
        properties: {
            distinct_id: distinctId,
            $group_type: 'organization',
            $group_key: 'posthog',
            $group_set: {
                prop: 'value',
            },
        },
    })

    const firstEventUuid = new UUIDT().toString()
    await capture({
        teamId,
        distinctId,
        uuid: firstEventUuid,
        event: 'custom event',
        properties: {
            name: 'haha',
            $group_0: 'posthog',
        },
        topic: 'events_plugin_ingestion_historical',
    })

    await waitForExpect(async () => {
        const [event] = await fetchEvents(teamId, firstEventUuid)
        expect(event).toEqual(
            expect.objectContaining({
                $group_0: 'posthog',
                group0_properties: {
                    prop: 'value',
                },
            })
        )
    })

    const secondGroupIdentityUuid = new UUIDT().toString()
    await capture({
        teamId,
        distinctId,
        uuid: secondGroupIdentityUuid,
        event: '$groupidentify',
        properties: {
            distinct_id: distinctId,
            $group_type: 'organization',
            $group_key: 'posthog',
            $group_set: {
                prop: 'updated value',
            },
        },
    })

    const secondEventUuid = new UUIDT().toString()
    await capture({
        teamId,
        distinctId,
        uuid: secondEventUuid,
        event: 'custom event',
        properties: {
            name: 'haha',
            $group_0: 'posthog',
        },
    })
    await waitForExpect(async () => {
        const [event] = await fetchEvents(teamId, secondEventUuid)
        expect(event).toEqual(
            expect.objectContaining({
                $group_0: 'posthog',
                group0_properties: {
                    prop: 'updated value',
                },
            })
        )
    })
})
