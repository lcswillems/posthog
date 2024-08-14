import '../Experiment.scss'

import { IconInfo } from '@posthog/icons'
import { LemonTable, LemonTableColumns, Tooltip } from '@posthog/lemon-ui'
import { useValues } from 'kea'
import { EntityFilterInfo } from 'lib/components/EntityFilterInfo'
import { LemonProgress } from 'lib/lemon-ui/LemonProgress'

import { _FunnelExperimentResults, FunnelExperimentVariant, InsightType, TrendExperimentVariant } from '~/types'

import { experimentLogic } from '../experimentLogic'
import { VariantTag } from './components'

export function SummaryTable(): JSX.Element {
    const {
        experimentResults,
        tabularExperimentResults,
        experimentInsightType,
        exposureCountDataForVariant,
        conversionRateForVariant,
        experimentMathAggregationForTrends,
        countDataForVariant,
        areTrendResultsConfusing,
        getHighestProbabilityVariant,
    } = useValues(experimentLogic)

    if (!experimentResults) {
        return <></>
    }

    const winningVariant = getHighestProbabilityVariant(experimentResults)

    const columns: LemonTableColumns<TrendExperimentVariant | FunnelExperimentVariant> = [
        {
            key: 'variants',
            title: 'Variant',
            render: function Key(_, item): JSX.Element {
                return (
                    <div className="flex items-center">
                        <VariantTag variantKey={item.key} />
                    </div>
                )
            },
        },
    ]

    if (experimentInsightType === InsightType.TRENDS) {
        columns.push({
            key: 'counts',
            title: (
                <div className="flex">
                    {experimentResults.insight?.[0] && 'action' in experimentResults.insight[0] && (
                        <EntityFilterInfo filter={experimentResults.insight[0].action} />
                    )}
                    <span className="pl-1">
                        {experimentMathAggregationForTrends(experimentResults?.filters) ? 'metric' : 'count'}
                    </span>
                </div>
            ),
            render: function Key(_, item, index): JSX.Element {
                return (
                    <div className="flex">
                        {countDataForVariant(experimentResults, item.key)}{' '}
                        {areTrendResultsConfusing && index === 0 && (
                            <Tooltip
                                placement="right"
                                title="It might seem confusing that the best variant has lower absolute count, but this can happen when fewer people are exposed to this variant, so its relative count is higher."
                            >
                                <IconInfo className="py-1 px-0.5 text-lg" />
                            </Tooltip>
                        )}
                    </div>
                )
            },
        })
        columns.push({
            key: 'exposure',
            title: 'Exposure',
            render: function Key(_, item): JSX.Element {
                return <div>{exposureCountDataForVariant(experimentResults, item.key)}</div>
            },
        })
    }

    if (experimentInsightType === InsightType.FUNNELS) {
        columns.push({
            key: 'conversionRate',
            title: 'Conversion rate',
            render: function Key(_, item): JSX.Element {
                const conversionRate = conversionRateForVariant(experimentResults, item.key)
                if (!conversionRate) {
                    return <>—</>
                }

                return <div className="font-semibold">{`${conversionRate.toFixed(2)}%`}</div>
            },
        }),
            columns.push({
                key: 'delta',
                title: (
                    <div className="inline-flex items-center space-x-1">
                        <div className="">Delta %</div>
                        <Tooltip title="Delta % indicates the percentage change in the conversion rate between the control and the test variant.">
                            <IconInfo className="text-muted-alt text-base" />
                        </Tooltip>
                    </div>
                ),
                render: function Key(_, item): JSX.Element {
                    if (item.key === 'control') {
                        return <em>Baseline</em>
                    }

                    const controlConversionRate = conversionRateForVariant(experimentResults, 'control')
                    const variantConversionRate = conversionRateForVariant(experimentResults, item.key)

                    if (!controlConversionRate || !variantConversionRate) {
                        return <>—</>
                    }

                    const delta = variantConversionRate - controlConversionRate

                    return (
                        <div
                            className={`font-semibold ${delta > 0 ? 'text-success' : delta < 0 ? 'text-danger' : ''}`}
                        >{`${delta > 0 ? '+' : ''}${delta.toFixed(2)}%`}</div>
                    )
                },
            }),
            columns.push({
                key: 'credibleInterval',
                title: (
                    <div className="inline-flex items-center space-x-1">
                        <div className="">Credible interval (95%)</div>
                        <Tooltip title="A credible interval represents a range within which we believe the true parameter value lies with a certain probability (often 95%), based on the posterior distribution derived from the observed data and our prior beliefs.">
                            <IconInfo className="text-muted-alt text-base" />
                        </Tooltip>
                    </div>
                ),
                render: function Key(_, item): JSX.Element {
                    const credibleInterval = (experimentResults as _FunnelExperimentResults)?.credible_intervals?.[
                        item.key
                    ]
                    if (!credibleInterval) {
                        return <>—</>
                    }

                    const lowerBound = (credibleInterval[0] * 100).toFixed(2)
                    const upperBound = (credibleInterval[1] * 100).toFixed(2)

                    return <div className="font-semibold">{`[${lowerBound}%, ${upperBound}%]`}</div>
                },
            })
    }

    columns.push({
        key: 'winProbability',
        title: 'Win probability',
        sorter: (a, b) => {
            const aPercentage = (experimentResults?.probability?.[a.key] || 0) * 100
            const bPercentage = (experimentResults?.probability?.[b.key] || 0) * 100
            return aPercentage - bPercentage
        },
        render: function Key(_, item): JSX.Element {
            const variantKey = item.key
            const percentage =
                experimentResults?.probability?.[variantKey] != undefined &&
                experimentResults.probability?.[variantKey] * 100
            const isWinning = variantKey === winningVariant

            return (
                <>
                    {percentage ? (
                        <span className="inline-flex items-center w-30 space-x-4">
                            <LemonProgress className="inline-flex w-3/4" percent={percentage} />
                            <span className={`w-1/4 font-semibold ${isWinning && 'text-success'}`}>
                                {percentage.toFixed(2)}%
                            </span>
                        </span>
                    ) : (
                        '—'
                    )}
                </>
            )
        },
    })

    return (
        <div className="mb-4">
            <LemonTable loading={false} columns={columns} dataSource={tabularExperimentResults} />
        </div>
    )
}
