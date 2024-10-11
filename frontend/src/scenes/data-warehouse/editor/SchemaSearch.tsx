import { LemonInput } from "@posthog/lemon-ui"


export const SchemaSearch = (): JSX.Element => {
    return (
        <div className="flex items-center">
            <LemonInput
                type="search"
                placeholder="Search for schema"
                data-attr="schema-search"
                fullWidth
            />
        </div>
    )
}