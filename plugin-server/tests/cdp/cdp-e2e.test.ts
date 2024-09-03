import { KafkaConsumer, LibrdKafkaError, Message } from 'node-rdkafka'

import { CdpFunctionCallbackConsumer, CdpProcessedEventsConsumer } from '../../src/cdp/cdp-consumers'
import { HogFunctionInvocationGlobals, HogFunctionType } from '../../src/cdp/types'
import { KAFKA_APP_METRICS_2 } from '../../src/config/kafka-topics'
import { BatchConsumer, startBatchConsumer } from '../../src/kafka/batch-consumer'
import { createRdConnectionConfigFromEnvVars } from '../../src/kafka/config'
import { createKafkaConsumer } from '../../src/kafka/consumer'
import { Hub, Team } from '../../src/types'
import { createHub } from '../../src/utils/db/hub'
import { delay } from '../../src/utils/utils'
import { getFirstTeam, resetTestDatabase } from '../helpers/sql'
import { HOG_EXAMPLES, HOG_FILTERS_EXAMPLES, HOG_INPUTS_EXAMPLES } from './examples'
import { createHogExecutionGlobals, insertHogFunction as _insertHogFunction } from './fixtures'

jest.mock('../../src/utils/fetch', () => {
    return {
        trackedFetch: jest.fn(() =>
            Promise.resolve({
                status: 200,
                text: () => Promise.resolve(JSON.stringify({ success: true })),
                json: () => Promise.resolve({ success: true }),
            })
        ),
    }
})

const mockFetch: jest.Mock = require('../../src/utils/fetch').trackedFetch

jest.setTimeout(1000)

type KafkaObserver = {
    messages: Message[]
    consumer: KafkaConsumer
    stop: () => Promise<void>
}
const createKafkaObserver = async (hub: Hub): Promise<KafkaObserver> => {
    const consumer = await createKafkaConsumer({
        ...createRdConnectionConfigFromEnvVars(hub),
        'group.id': 'test-group',
    })

    consumer.connect()
    consumer.subscribe([KAFKA_APP_METRICS_2])
    const messages: Message[] = []

    const poll = async () => {
        await delay(50)
        if (!consumer.isConnected()) {
            return
        }
        const newMessages = await new Promise<Message[]>((res, rej) =>
            consumer.consume(1000, (err, messages) => (err ? rej(err) : res(messages)))
        )
        messages.push(...newMessages)
        poll()
    }

    poll()

    return {
        messages,
        consumer,
        stop: () => new Promise((res) => consumer.disconnect(res)),
    }
}

describe('CDP E2E', () => {
    let processedEventsConsumer: CdpProcessedEventsConsumer
    let functionProcessor: CdpFunctionCallbackConsumer
    let hub: Hub
    let closeHub: () => Promise<void>
    let team: Team
    let kafkaObserver: KafkaObserver

    const insertHogFunction = async (hogFunction: Partial<HogFunctionType>) => {
        const item = await _insertHogFunction(hub.postgres, team.id, hogFunction)
        // Trigger the reload that django would do
        await processedEventsConsumer.hogFunctionManager.reloadAllHogFunctions()
        await functionProcessor.hogFunctionManager.reloadAllHogFunctions()
        return item
    }

    beforeEach(async () => {
        await resetTestDatabase()
        ;[hub, closeHub] = await createHub()
        team = await getFirstTeam(hub)

        kafkaObserver = await createKafkaObserver(hub)

        processedEventsConsumer = new CdpProcessedEventsConsumer(hub)
        await processedEventsConsumer.start()
        functionProcessor = new CdpFunctionCallbackConsumer(hub)
        await functionProcessor.start()

        mockFetch.mockClear()
    })

    afterEach(async () => {
        jest.setTimeout(10000)
        await processedEventsConsumer.stop()
        await functionProcessor.stop()
        await kafkaObserver.stop()
        await closeHub()
    })

    afterAll(() => {
        jest.useRealTimers()
    })

    describe('full fetch function', () => {
        /**
         * Tests here are somewhat expensive so should mostly simulate happy paths and the more e2e scenarios
         */

        let fnFetchNoFilters: HogFunctionType
        let globals: HogFunctionInvocationGlobals

        beforeEach(async () => {
            fnFetchNoFilters = await insertHogFunction({
                ...HOG_EXAMPLES.simple_fetch,
                ...HOG_INPUTS_EXAMPLES.simple_fetch,
                ...HOG_FILTERS_EXAMPLES.no_filters,
            })

            globals = createHogExecutionGlobals({
                project: {
                    id: team.id,
                } as any,
                event: {
                    uuid: 'b3a1fe86-b10c-43cc-acaf-d208977608d0',
                    name: '$pageview',
                    properties: {
                        $current_url: 'https://posthog.com',
                        $lib_version: '1.0.0',
                    },
                } as any,
            })
        })

        // const gatherProducedMessages = () => {
        //     const allMessages = decodeAllKafkaMessages()

        //     allMessages.forEach((message) => {
        //         if (message.topic === 'clickhouse_app_metrics2_test') {
        //             kafkaMessages.metrics.push(message)
        //         } else if (message.topic === 'log_entries_test') {
        //             kafkaMessages.logs.push(message)
        //         } else if (message.topic === 'cdp_function_callbacks_test') {
        //             kafkaMessages.invocations.push(message)
        //         } else {
        //             throw new Error(`Unknown topic: ${message.topic}`)
        //         }
        //     })

        //     mockProducer.produce.mockClear()
        // }

        it('should invoke a function via kafka transportation until completed', async () => {
            // NOTE: We can skip kafka as the entry point
            const invocations = await processedEventsConsumer.processBatch([globals])
            expect(invocations).toHaveLength(1)
            // gatherProducedMessages()

            // expect(kafkaMessages.invocations).toHaveLength(1)
            // expect(kafkaMessages.invocations[0].topic).toEqual('cdp_function_callbacks_test')
            // // mockProducer.produce.mockClear()

            // while (kafkaMessages.invocations.length) {
            //     await functionProcessor._handleKafkaBatch([convertToKafkaMessage(kafkaMessages.invocations[0])])
            //     kafkaMessages.invocations = []
            //     gatherProducedMessages()
            // }

            // expect(kafkaMessages.metrics).toMatchObject([
            //     {
            //         key: fnFetchNoFilters.id.toString(),
            //         value: {
            //             app_source: 'hog_function',
            //             app_source_id: fnFetchNoFilters.id.toString(),
            //             count: 1,
            //             metric_kind: 'success',
            //             metric_name: 'succeeded',
            //             team_id: 2,
            //         },
            //     },
            // ])
            // expect(kafkaMessages.logs.map((x) => x.value.message)).toEqual([
            //     'Executing function',
            //     "Suspending function due to async function call 'fetch'. Payload: 1902 bytes",
            //     'Resuming function',
            //     'Fetch response:, {"status":200,"body":{"success":true}}',
            //     expect.stringContaining('Function completed'),
            // ])
        })
    })
})
