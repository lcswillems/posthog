import { useActions, useValues } from 'kea'
import { dataNodeLogic } from '~/queries/nodes/DataNode/dataNodeLogic'
import { LemonSwitch } from 'lib/lemon-ui/LemonSwitch/LemonSwitch'
import { useEffect } from 'react'

export function AutoLoad(): JSX.Element {
    const { autoLoadToggled } = useValues(dataNodeLogic)
    const { startAutoLoad, stopAutoLoad, toggleAutoLoad } = useActions(dataNodeLogic)

    // Reload data only when this AutoLoad component is mounted.
    // This avoids needless reloading in the background, as logics might be kept
    // around, even if not visually present.
    useEffect(() => {
        startAutoLoad()
        return () => stopAutoLoad()
        // FIXME
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    return (
        <div className="flex items-center gap-2">
            <LemonSwitch
                bordered
                data-attr="live-events-refresh-toggle"
                id="autoload-switch"
                label="Automatically load new events"
                checked={autoLoadToggled}
                onChange={toggleAutoLoad}
            />
        </div>
    )
}
