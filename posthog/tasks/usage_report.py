import dataclasses
import os
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Literal, Optional, TypedDict, Union, cast

import requests
import structlog
from celery import shared_task
from dateutil import parser
from django.conf import settings
from django.db import connection
from django.db.models import Count, Q, QuerySet, Sum
from posthoganalytics.client import Client
from psycopg import sql
from retry import retry
from sentry_sdk import capture_exception

from posthog import version_requirement
from posthog.clickhouse.client.connection import Workload
from posthog.clickhouse.materialized_columns import get_enabled_materialized_columns
from posthog.client import sync_execute
from posthog.cloud_utils import get_cached_instance_license, is_cloud
from posthog.constants import FlagRequestType
from posthog.logging.timing import timed_log
from posthog.models import GroupTypeMapping, OrganizationMembership, User
from posthog.models.dashboard import Dashboard
from posthog.models.event_definition import EventDefinition
from posthog.models.experiment import Experiment
from posthog.models.feature_flag import FeatureFlag
from posthog.models.feedback.survey import Survey
from posthog.models.organization import Organization
from posthog.models.plugin import PluginConfig
from posthog.models.team.team import Team
from posthog.models.utils import namedtuplefetchall
from posthog.session_recordings.models.session_recording_playlist import (
    SessionRecordingPlaylist,
)
from posthog.settings import CLICKHOUSE_CLUSTER, INSTANCE_TAG
from posthog.tasks.utils import CeleryQueue
from posthog.utils import (
    get_helm_info_env,
    get_instance_realm,
    get_instance_region,
    get_machine_id,
    get_previous_day,
)
from posthog.warehouse.models import ExternalDataJob
from posthog.warehouse.models.external_data_source import ExternalDataSource

logger = structlog.get_logger(__name__)


class Period(TypedDict):
    start_inclusive: str
    end_inclusive: str


class TableSizes(TypedDict):
    posthog_event: int
    posthog_sessionrecordingevent: int


CH_BILLING_SETTINGS = {
    "max_execution_time": 5 * 60,  # 5 minutes
}

QUERY_RETRIES = 3
QUERY_RETRY_DELAY = 1
QUERY_RETRY_BACKOFF = 2

USAGE_REPORT_TASK_KWARGS = {
    "queue": CeleryQueue.USAGE_REPORTS.value,
    "ignore_result": True,
    "autoretry_for": (Exception,),
    "retry_backoff": True,
}


@dataclasses.dataclass
class UsageReportCounters:
    event_count_in_period: int
    enhanced_persons_event_count_in_period: int
    event_count_with_groups_in_period: int
    event_count_from_langfuse_in_period: int
    event_count_from_helicone_in_period: int
    event_count_from_keywords_ai_in_period: int
    event_count_from_traceloop_in_period: int

    # Recordings
    recording_count_in_period: int
    mobile_recording_count_in_period: int
    # Persons and Groups
    group_types_total: int
    # Dashboards
    dashboard_count: int
    dashboard_template_count: int
    dashboard_shared_count: int
    dashboard_tagged_count: int
    # Feature flags
    ff_count: int
    ff_active_count: int
    decide_requests_count_in_period: int
    local_evaluation_requests_count_in_period: int
    billable_feature_flag_requests_count_in_period: int
    # Queries
    query_app_bytes_read: int
    query_app_rows_read: int
    query_app_duration_ms: int
    query_api_bytes_read: int
    query_api_rows_read: int
    query_api_duration_ms: int
    # Event Explorer
    event_explorer_app_bytes_read: int
    event_explorer_app_rows_read: int
    event_explorer_app_duration_ms: int
    event_explorer_api_bytes_read: int
    event_explorer_api_rows_read: int
    event_explorer_api_duration_ms: int
    # Surveys
    survey_responses_count_in_period: int
    # Data Warehouse
    rows_synced_in_period: int
    # CDP Delivery
    hog_function_calls_in_period: int
    hog_function_fetch_calls_in_period: int
    # SDK usage
    web_events_count_in_period: int
    web_lite_events_count_in_period: int
    node_events_count_in_period: int
    android_events_count_in_period: int
    flutter_events_count_in_period: int
    ios_events_count_in_period: int
    go_events_count_in_period: int
    java_events_count_in_period: int
    react_native_events_count_in_period: int
    ruby_events_count_in_period: int
    python_events_count_in_period: int
    php_events_count_in_period: int


@dataclasses.dataclass
class WeeklyDigestReport:
    new_dashboards_in_last_7_days: list[dict[str, str]]
    new_event_definitions_in_last_7_days: list[dict[str, str]]
    new_playlists_created_in_last_7_days: list[dict[str, str]]
    new_experiments_launched_in_last_7_days: list[dict[str, str]]
    new_experiments_completed_in_last_7_days: list[dict[str, str]]
    new_external_data_sources_connected_in_last_7_days: list[dict[str, str]]
    new_surveys_launched_in_last_7_days: list[dict[str, str]]
    new_feature_flags_created_in_last_7_days: list[dict[str, str]]


# Instance metadata to be included in overall report
@dataclasses.dataclass
class InstanceMetadata:
    deployment_infrastructure: str
    realm: str
    period: Period
    site_url: str
    product: str
    helm: Optional[dict]
    clickhouse_version: Optional[str]
    users_who_logged_in: Optional[list[dict[str, Union[str, int]]]]
    users_who_logged_in_count: Optional[int]
    users_who_signed_up: Optional[list[dict[str, Union[str, int]]]]
    users_who_signed_up_count: Optional[int]
    table_sizes: Optional[TableSizes]
    plugins_installed: Optional[dict]
    plugins_enabled: Optional[dict]
    instance_tag: str


@dataclasses.dataclass
class OrgReport(UsageReportCounters):
    date: str
    organization_id: str
    organization_name: str
    organization_created_at: str
    organization_user_count: int
    team_count: int
    teams: dict[str, UsageReportCounters]


@dataclasses.dataclass
class FullUsageReport(OrgReport, InstanceMetadata):
    pass


def fetch_table_size(table_name: str) -> int:
    return fetch_sql("SELECT pg_total_relation_size(%s) as size", (table_name,))[0].size


def fetch_sql(sql_: str, params: tuple[Any, ...]) -> list[Any]:
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL(sql_), params)
        return namedtuplefetchall(cursor)


def get_product_name(realm: str, has_license: bool) -> str:
    if realm == "cloud":
        return "cloud"
    elif realm in {"hosted", "hosted-clickhouse"}:
        return "scale" if has_license else "open source"
    else:
        return "unknown"


def get_instance_metadata(period: tuple[datetime, datetime]) -> InstanceMetadata:
    has_license = False

    if settings.EE_AVAILABLE:
        license = get_cached_instance_license()
        has_license = license is not None

    period_start, period_end = period

    realm = get_instance_realm()
    metadata = InstanceMetadata(
        deployment_infrastructure=os.getenv("DEPLOYMENT", "unknown"),
        realm=realm,
        period={
            "start_inclusive": period_start.isoformat(),
            "end_inclusive": period_end.isoformat(),
        },
        site_url=settings.SITE_URL,
        product=get_product_name(realm, has_license),
        # Non-cloud vars
        helm=None,
        clickhouse_version=None,
        users_who_logged_in=None,
        users_who_logged_in_count=None,
        users_who_signed_up=None,
        users_who_signed_up_count=None,
        table_sizes=None,
        plugins_installed=None,
        plugins_enabled=None,
        instance_tag=INSTANCE_TAG,
    )

    if realm != "cloud":
        metadata.helm = get_helm_info_env()
        metadata.clickhouse_version = str(version_requirement.get_clickhouse_version())

        metadata.users_who_logged_in = [
            (
                {"id": user.id, "distinct_id": user.distinct_id}
                if user.anonymize_data
                else {
                    "id": user.id,
                    "distinct_id": user.distinct_id,
                    "first_name": user.first_name,
                    "email": user.email,
                }
            )
            for user in User.objects.filter(is_active=True, last_login__gte=period_start, last_login__lte=period_end)
        ]
        metadata.users_who_logged_in_count = len(metadata.users_who_logged_in)

        metadata.users_who_signed_up = [
            (
                {"id": user.id, "distinct_id": user.distinct_id}
                if user.anonymize_data
                else {
                    "id": user.id,
                    "distinct_id": user.distinct_id,
                    "first_name": user.first_name,
                    "email": user.email,
                }
            )
            for user in User.objects.filter(
                is_active=True,
                date_joined__gte=period_start,
                date_joined__lte=period_end,
            )
        ]
        metadata.users_who_signed_up_count = len(metadata.users_who_signed_up)

        metadata.table_sizes = {
            "posthog_event": fetch_table_size("posthog_event"),
            "posthog_sessionrecordingevent": fetch_table_size("posthog_sessionrecordingevent"),
        }

        plugin_configs = PluginConfig.objects.select_related("plugin").all()

        metadata.plugins_installed = dict(Counter(plugin_config.plugin.name for plugin_config in plugin_configs))
        metadata.plugins_enabled = dict(
            Counter(plugin_config.plugin.name for plugin_config in plugin_configs if plugin_config.enabled)
        )

    return metadata


def get_org_user_count(organization_id: str) -> int:
    return OrganizationMembership.objects.filter(organization_id=organization_id).count()


def get_org_owner_or_first_user(organization_id: str) -> Optional[User]:
    # Find the membership object for the org owner
    user = None
    membership = OrganizationMembership.objects.filter(
        organization_id=organization_id, level=OrganizationMembership.Level.OWNER
    ).first()
    if not membership:
        # If no owner membership is present, pick the first membership association we can find
        membership = OrganizationMembership.objects.filter(organization_id=organization_id).first()
    if hasattr(membership, "user"):
        membership = cast(OrganizationMembership, membership)
        user = membership.user
    else:
        capture_exception(
            Exception("No user found for org while generating report"),
            {"org": {"organization_id": organization_id}},
        )
    return user


@shared_task(**USAGE_REPORT_TASK_KWARGS, max_retries=3, rate_limit="10/s")
def send_report_to_billing_service(org_id: str, report: dict[str, Any]) -> None:
    if not settings.EE_AVAILABLE:
        return

    from ee.billing.billing_manager import BillingManager, build_billing_token
    from ee.billing.billing_types import BillingStatus
    from ee.settings import BILLING_SERVICE_URL

    try:
        license = get_cached_instance_license()
        if not license or not license.is_v2_license:
            return

        organization = Organization.objects.get(id=org_id)
        if not organization:
            return

        token = build_billing_token(license, organization)
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        response = requests.post(f"{BILLING_SERVICE_URL}/api/usage", json=report, headers=headers)
        if response.status_code != 200:
            raise Exception(
                f"Failed to send usage report to billing service code:{response.status_code} response:{response.text}"
            )

        logger.info(f"UsageReport sent to Billing for organization: {organization.id}")

        response_data: BillingStatus = response.json()
        BillingManager(license).update_org_details(organization, response_data)

    except Exception as err:
        logger.exception(f"UsageReport failed sending to Billing for organization: {organization.id}: {err}")
        capture_exception(err)
        pha_client = Client("sTMFPsFhdP1Ssg")
        capture_event(
            pha_client=pha_client,
            name=f"organization usage report to billing service failure",
            organization_id=org_id,
            properties={"err": str(err)},
        )
        raise


def capture_event(
    *,
    pha_client: Client,
    name: str,
    organization_id: Optional[str] = None,
    team_id: Optional[int] = None,
    properties: dict[str, Any],
    timestamp: Optional[Union[datetime, str]] = None,
    send_for_all_members: bool = False,
) -> None:
    if timestamp and isinstance(timestamp, str):
        try:
            timestamp = parser.isoparse(timestamp)
        except ValueError:
            timestamp = None

    if not organization_id and not team_id:
        raise ValueError("Either organization_id or team_id must be provided")

    if is_cloud():
        distinct_ids = []
        if send_for_all_members:
            if organization_id:
                distinct_ids = list(
                    OrganizationMembership.objects.filter(organization_id=organization_id).values_list(
                        "user__distinct_id", flat=True
                    )
                )
            elif team_id:
                team = Team.objects.get(id=team_id)
                distinct_ids = [user.distinct_id for user in team.all_users_with_access()]
        else:
            if not organization_id:
                team = Team.objects.get(id=team_id)
                organization_id = team.organization_id
            org_owner = get_org_owner_or_first_user(organization_id) if organization_id else None
            distinct_ids.append(
                org_owner.distinct_id if org_owner and org_owner.distinct_id else f"org-{organization_id}"
            )

        for distinct_id in distinct_ids:
            pha_client.capture(
                distinct_id,
                name,
                {**properties, "scope": "user"},
                groups={"organization": organization_id, "instance": settings.SITE_URL},
                timestamp=timestamp,
            )
        pha_client.group_identify("organization", organization_id, properties)
    else:
        pha_client.capture(
            get_machine_id(),
            name,
            {**properties, "scope": "machine"},
            groups={"instance": settings.SITE_URL},
            timestamp=timestamp,
        )
        pha_client.group_identify("instance", settings.SITE_URL, properties)


@timed_log()
@retry(tries=QUERY_RETRIES, delay=QUERY_RETRY_DELAY, backoff=QUERY_RETRY_BACKOFF)
def get_teams_with_billable_event_count_in_period(
    begin: datetime, end: datetime, count_distinct: bool = False
) -> list[tuple[int, int]]:
    # count only unique events
    # Duplicate events will be eventually removed by ClickHouse and likely came from our library or pipeline.
    # We shouldn't bill for these. However counting unique events is more expensive, and likely to fail on longer time ranges.
    # So, we count uniques in small time periods only, controlled by the count_distinct parameter.
    if count_distinct:
        # Uses the same expression as the one used to de-duplicate events on the merge tree:
        # https://github.com/PostHog/posthog/blob/master/posthog/models/event/sql.py#L92
        distinct_expression = "distinct toDate(timestamp), event, cityHash64(distinct_id), cityHash64(uuid)"
    else:
        distinct_expression = "1"

    result = sync_execute(
        f"""
        SELECT team_id, count({distinct_expression}) as count
        FROM events
        WHERE timestamp between %(begin)s AND %(end)s AND event != '$feature_flag_called' AND event NOT IN ('survey sent', 'survey shown', 'survey dismissed')
        GROUP BY team_id
    """,
        {"begin": begin, "end": end},
        workload=Workload.OFFLINE,
        settings=CH_BILLING_SETTINGS,
    )
    return result


@timed_log()
@retry(tries=QUERY_RETRIES, delay=QUERY_RETRY_DELAY, backoff=QUERY_RETRY_BACKOFF)
def get_teams_with_billable_enhanced_persons_event_count_in_period(
    begin: datetime, end: datetime, count_distinct: bool = False
) -> list[tuple[int, int]]:
    # count only unique events
    # Duplicate events will be eventually removed by ClickHouse and likely came from our library or pipeline.
    # We shouldn't bill for these. However counting unique events is more expensive, and likely to fail on longer time ranges.
    # So, we count uniques in small time periods only, controlled by the count_distinct parameter.
    if count_distinct:
        # Uses the same expression as the one used to de-duplicate events on the merge tree:
        # https://github.com/PostHog/posthog/blob/master/posthog/models/event/sql.py#L92
        distinct_expression = "distinct toDate(timestamp), event, cityHash64(distinct_id), cityHash64(uuid)"
    else:
        distinct_expression = "1"

    result = sync_execute(
        f"""
        SELECT team_id, count({distinct_expression}) as count
        FROM events
        WHERE timestamp between %(begin)s AND %(end)s AND event != '$feature_flag_called' AND event NOT IN ('survey sent', 'survey shown', 'survey dismissed') AND person_mode IN ('full', 'force_upgrade')
        GROUP BY team_id
    """,
        {"begin": begin, "end": end},
        workload=Workload.OFFLINE,
        settings=CH_BILLING_SETTINGS,
    )
    return result


@timed_log()
@retry(tries=QUERY_RETRIES, delay=QUERY_RETRY_DELAY, backoff=QUERY_RETRY_BACKOFF)
def get_teams_with_event_count_with_groups_in_period(begin: datetime, end: datetime) -> list[tuple[int, int]]:
    result = sync_execute(
        """
        SELECT team_id, count(1) as count
        FROM events
        WHERE timestamp between %(begin)s AND %(end)s
        AND ($group_0 != '' OR $group_1 != '' OR $group_2 != '' OR $group_3 != '' OR $group_4 != '')
        GROUP BY team_id
    """,
        {"begin": begin, "end": end},
        workload=Workload.OFFLINE,
        settings=CH_BILLING_SETTINGS,
    )
    return result


@timed_log()
@retry(tries=QUERY_RETRIES, delay=QUERY_RETRY_DELAY, backoff=QUERY_RETRY_BACKOFF)
def get_all_event_metrics_in_period(begin: datetime, end: datetime) -> dict[str, list[tuple[int, int]]]:
    materialized_columns = get_enabled_materialized_columns("events")

    # Check if $lib is materialized
    lib_expression = materialized_columns.get(("$lib", "properties"), "JSONExtractString(properties, '$lib')")

    results = sync_execute(
        f"""
        SELECT
            team_id,
            multiIf(
                event LIKE 'helicone%%', 'helicone_events',
                event LIKE 'langfuse%%', 'langfuse_events',
                event LIKE 'keywords_ai%%', 'keywords_ai_events',
                event LIKE 'traceloop%%', 'traceloop_events',
                {lib_expression} = 'web', 'web_events',
                {lib_expression} = 'posthog-js-lite', 'web_lite_events',
                {lib_expression} = 'posthog-node', 'node_events',
                {lib_expression} = 'posthog-android', 'android_events',
                {lib_expression} = 'posthog-flutter', 'flutter_events',
                {lib_expression} = 'posthog-ios', 'ios_events',
                {lib_expression} = 'posthog-go', 'go_events',
                {lib_expression} = 'posthog-java', 'java_events',
                {lib_expression} = 'posthog-react-native', 'react_native_events',
                {lib_expression} = 'posthog-ruby', 'ruby_events',
                {lib_expression} = 'posthog-python', 'python_events',
                {lib_expression} = 'posthog-php', 'php_events',
                'other'
            ) AS metric,
            count(1) as count
        FROM events
        WHERE timestamp BETWEEN %(begin)s AND %(end)s
        GROUP BY team_id, metric
        HAVING metric != 'other'
    """,
        {"begin": begin, "end": end},
        workload=Workload.OFFLINE,
        settings=CH_BILLING_SETTINGS,
    )

    metrics: dict[str, list[tuple[int, int]]] = {
        "helicone_events": [],
        "langfuse_events": [],
        "keywords_ai_events": [],
        "traceloop_events": [],
        "web_events": [],
        "web_lite_events": [],
        "node_events": [],
        "android_events": [],
        "flutter_events": [],
        "ios_events": [],
        "go_events": [],
        "java_events": [],
        "react_native_events": [],
        "ruby_events": [],
        "python_events": [],
        "php_events": [],
    }

    for team_id, metric, count in results:
        metrics[metric].append((team_id, count))

    return metrics


@timed_log()
@retry(tries=QUERY_RETRIES, delay=QUERY_RETRY_DELAY, backoff=QUERY_RETRY_BACKOFF)
def get_teams_with_recording_count_in_period(
    begin: datetime, end: datetime, snapshot_source: Literal["mobile", "web"] = "web"
) -> list[tuple[int, int]]:
    previous_begin = begin - (end - begin)

    result = sync_execute(
        """
        SELECT team_id, count(distinct session_id) as count
        FROM (
            SELECT any(team_id) as team_id, session_id
            FROM session_replay_events
            WHERE min_first_timestamp BETWEEN %(begin)s AND %(end)s
            GROUP BY session_id
            HAVING ifNull(argMinMerge(snapshot_source), 'web') == %(snapshot_source)s
        )
        WHERE session_id NOT IN (
            -- we want to exclude sessions that might have events with timestamps
            -- before the period we are interested in
            SELECT DISTINCT session_id
            FROM session_replay_events
            -- begin is the very first instant of the period we are interested in
            -- we assume it is also the very first instant of a day
            -- so we can to subtract 1 second to get the day before
            WHERE min_first_timestamp BETWEEN %(previous_begin)s AND %(begin)s
            GROUP BY session_id
        )
        GROUP BY team_id
    """,
        {"previous_begin": previous_begin, "begin": begin, "end": end, "snapshot_source": snapshot_source},
        workload=Workload.OFFLINE,
        settings=CH_BILLING_SETTINGS,
    )

    return result


@timed_log()
@retry(tries=QUERY_RETRIES, delay=QUERY_RETRY_DELAY, backoff=QUERY_RETRY_BACKOFF)
def get_teams_with_query_metric(
    begin: datetime,
    end: datetime,
    query_types: Optional[list[str]] = None,
    access_method: str = "",
    metric: Literal["read_bytes", "read_rows", "query_duration_ms"] = "read_bytes",
) -> list[tuple[int, int]]:
    if metric not in ["read_bytes", "read_rows", "query_duration_ms"]:
        # :TRICKY: Inlined into the query below.
        raise ValueError(f"Invalid metric {metric}")

    query_types_clause = "AND query_type IN (%(query_types)s)" if query_types and len(query_types) > 0 else ""

    query = f"""
        WITH JSONExtractInt(log_comment, 'team_id') as team_id,
            JSONExtractString(log_comment, 'query_type') as query_type,
            JSONExtractString(log_comment, 'access_method') as access_method
        SELECT team_id, sum({metric}) as count
        FROM clusterAllReplicas({CLICKHOUSE_CLUSTER}, system.query_log)
        WHERE (type = 'QueryFinish' OR type = 'ExceptionWhileProcessing')
        AND is_initial_query = 1
        {query_types_clause}
        AND query_start_time between %(begin)s AND %(end)s
        AND access_method = %(access_method)s
        GROUP BY team_id
    """
    result = sync_execute(
        query,
        {
            "begin": begin,
            "end": end,
            "query_types": query_types,
            "access_method": access_method,
        },
        workload=Workload.OFFLINE,
        settings=CH_BILLING_SETTINGS,
    )
    return result


@timed_log()
@retry(tries=QUERY_RETRIES, delay=QUERY_RETRY_DELAY, backoff=QUERY_RETRY_BACKOFF)
def get_teams_with_feature_flag_requests_count_in_period(
    begin: datetime, end: datetime, request_type: FlagRequestType
) -> list[tuple[int, int]]:
    # depending on the region, events are stored in different teams
    team_to_query = 1 if get_instance_region() == "EU" else 2
    validity_token = settings.DECIDE_BILLING_ANALYTICS_TOKEN

    target_event = "decide usage" if request_type == FlagRequestType.DECIDE else "local evaluation usage"

    result = sync_execute(
        """
        SELECT distinct_id as team, sum(JSONExtractInt(properties, 'count')) as sum
        FROM events
        WHERE team_id = %(team_to_query)s AND event=%(target_event)s AND timestamp between %(begin)s AND %(end)s
        AND has([%(validity_token)s], replaceRegexpAll(JSONExtractRaw(properties, 'token'), '^"|"$', ''))
        GROUP BY team
    """,
        {
            "begin": begin,
            "end": end,
            "team_to_query": team_to_query,
            "validity_token": validity_token,
            "target_event": target_event,
        },
        workload=Workload.OFFLINE,
        settings=CH_BILLING_SETTINGS,
    )

    return result


@timed_log()
@retry(tries=QUERY_RETRIES, delay=QUERY_RETRY_DELAY, backoff=QUERY_RETRY_BACKOFF)
def get_teams_with_survey_responses_count_in_period(
    begin: datetime,
    end: datetime,
) -> list[tuple[int, int]]:
    results = sync_execute(
        """
        SELECT team_id, COUNT() as count
        FROM events
        WHERE event = 'survey sent' AND timestamp between %(begin)s AND %(end)s
        GROUP BY team_id
    """,
        {"begin": begin, "end": end},
        workload=Workload.OFFLINE,
        settings=CH_BILLING_SETTINGS,
    )

    return results


@timed_log()
@retry(tries=QUERY_RETRIES, delay=QUERY_RETRY_DELAY, backoff=QUERY_RETRY_BACKOFF)
def get_teams_with_rows_synced_in_period(begin: datetime, end: datetime) -> list:
    return list(
        ExternalDataJob.objects.filter(created_at__gte=begin, created_at__lte=end)
        .values("team_id")
        .annotate(total=Sum("rows_synced"))
    )


@timed_log()
@retry(tries=QUERY_RETRIES, delay=QUERY_RETRY_DELAY, backoff=QUERY_RETRY_BACKOFF)
def get_teams_with_hog_function_calls_in_period(
    begin: datetime,
    end: datetime,
) -> list[tuple[int, int]]:
    results = sync_execute(
        """
        SELECT team_id, SUM(count) as count
        FROM app_metrics2
        WHERE app_source='hog_function' AND metric_name IN ('succeeded','failed') AND timestamp between %(begin)s AND %(end)s
        GROUP BY team_id, metric_name
    """,
        {"begin": begin, "end": end},
        workload=Workload.OFFLINE,
        settings=CH_BILLING_SETTINGS,
    )

    return results


@timed_log()
@retry(tries=QUERY_RETRIES, delay=QUERY_RETRY_DELAY, backoff=QUERY_RETRY_BACKOFF)
def get_teams_with_hog_function_fetch_calls_in_period(
    begin: datetime,
    end: datetime,
) -> list[tuple[int, int]]:
    results = sync_execute(
        """
        SELECT team_id, SUM(count) as count
        FROM app_metrics2
        WHERE app_source='hog_function' AND metric_name IN ('fetch') AND timestamp between %(begin)s AND %(end)s
        GROUP BY team_id, metric_name
    """,
        {"begin": begin, "end": end},
        workload=Workload.OFFLINE,
        settings=CH_BILLING_SETTINGS,
    )

    return results


@timed_log()
def get_teams_with_new_dashboards_in_last_7_days(
    end: datetime,
) -> QuerySet:
    begin = end - timedelta(days=7)
    return Dashboard.objects.filter(created_at__gt=begin, created_at__lte=end).values("team_id", "name", "id")


@timed_log()
def get_teams_with_new_event_definitions_in_last_7_days(
    end: datetime,
) -> QuerySet:
    begin = end - timedelta(days=7)
    return EventDefinition.objects.filter(created_at__gt=begin, created_at__lte=end).values("team_id", "name", "id")


@timed_log()
def get_teams_with_new_playlists_created_in_last_7_days(
    end: datetime,
) -> QuerySet:
    begin = end - timedelta(days=7)
    return SessionRecordingPlaylist.objects.filter(created_at__gt=begin, created_at__lte=end).values(
        "team_id", "name", "short_id"
    )


@timed_log()
def get_teams_with_new_experiments_launched_in_last_7_days(
    end: datetime,
) -> QuerySet:
    begin = end - timedelta(days=7)
    return Experiment.objects.filter(start_date__gt=begin, start_date__lte=end).values(
        "team_id", "name", "id", "start_date"
    )


@timed_log()
def get_teams_with_new_experiments_completed_in_last_7_days(
    end: datetime,
) -> QuerySet:
    begin = end - timedelta(days=7)
    return Experiment.objects.filter(end_date__gt=begin, end_date__lte=end).values(
        "team_id", "name", "id", "start_date", "end_date"
    )


@timed_log()
def get_teams_with_new_external_data_sources_connected_in_last_7_days(
    end: datetime,
) -> QuerySet:
    begin = end - timedelta(days=7)
    return ExternalDataSource.objects.filter(created_at__gt=begin, created_at__lte=end, deleted=False).values(
        "team_id", "source_type", "id"
    )


@timed_log()
def get_teams_with_new_surveys_launched_in_last_7_days(
    end: datetime,
) -> QuerySet:
    begin = end - timedelta(days=7)
    return Survey.objects.filter(start_date__gt=begin, start_date__lte=end).values(
        "team_id", "name", "id", "description", "start_date"
    )


@timed_log()
def get_teams_with_new_feature_flags_created_in_last_7_days(
    end: datetime,
) -> QuerySet:
    begin = end - timedelta(days=7)
    return (
        FeatureFlag.objects.filter(
            created_at__gt=begin,
            created_at__lte=end,
            deleted=False,
        )
        .exclude(name__contains="Feature Flag for Experiment")
        .exclude(name__contains="Targeting flag for survey")
        .values("team_id", "name", "id", "key")
    )


@shared_task(**USAGE_REPORT_TASK_KWARGS, max_retries=0)
def capture_report(
    *,
    capture_event_name: str,
    org_id: Optional[str] = None,
    team_id: Optional[int] = None,
    full_report_dict: dict[str, Any],
    at_date: Optional[datetime] = None,
    send_for_all_members: bool = False,
) -> None:
    if not org_id and not team_id:
        raise ValueError("Either org_id or team_id must be provided")
    pha_client = Client("sTMFPsFhdP1Ssg")
    try:
        capture_event(
            pha_client=pha_client,
            name=capture_event_name,
            organization_id=org_id,
            team_id=team_id,
            properties=full_report_dict,
            timestamp=at_date,
            send_for_all_members=send_for_all_members,
        )
        logger.info(f"UsageReport sent to PostHog for organization {org_id}")
    except Exception as err:
        logger.exception(
            f"UsageReport sent to PostHog for organization {org_id} failed: {str(err)}",
        )
        capture_event(
            pha_client=pha_client,
            name=f"{capture_event_name} failure",
            organization_id=org_id,
            team_id=team_id,
            properties={"error": str(err)},
            send_for_all_members=send_for_all_members,
        )
    pha_client.flush()


# extend this with future usage based products
def has_non_zero_usage(report: FullUsageReport) -> bool:
    return (
        report.event_count_in_period > 0
        or report.enhanced_persons_event_count_in_period > 0
        or report.recording_count_in_period > 0
        # explicitly not including mobile_recording_count_in_period for now
        or report.decide_requests_count_in_period > 0
        or report.local_evaluation_requests_count_in_period > 0
        or report.survey_responses_count_in_period > 0
        or report.rows_synced_in_period > 0
    )


def has_non_zero_digest(report: WeeklyDigestReport) -> bool:
    return any(len(getattr(report, key)) > 0 for key in report.__dataclass_fields__)


def convert_team_usage_rows_to_dict(rows: list[Union[dict, tuple[int, int]]]) -> dict[int, int]:
    team_id_map = {}
    for row in rows:
        if isinstance(row, dict) and "team_id" in row:
            # Some queries return a dict with team_id and total
            team_id_map[row["team_id"]] = row["total"]
        else:
            # Others are just a tuple with team_id and total
            team_id_map[int(row[0])] = row[1]
    return team_id_map


def convert_team_digest_items_to_dict(items: QuerySet) -> dict[int, QuerySet]:
    return {team_id: items.filter(team_id=team_id) for team_id in items.values_list("team_id", flat=True).distinct()}


def _get_all_usage_data(period_start: datetime, period_end: datetime) -> dict[str, Any]:
    """
    Gets all usage data for the specified period. Clickhouse is good at counting things so
    we count across all teams rather than doing it one by one
    """

    all_metrics = get_all_event_metrics_in_period(period_start, period_end)

    return {
        "teams_with_event_count_in_period": get_teams_with_billable_event_count_in_period(
            period_start, period_end, count_distinct=True
        ),
        "teams_with_enhanced_persons_event_count_in_period": get_teams_with_billable_enhanced_persons_event_count_in_period(
            period_start, period_end, count_distinct=True
        ),
        "teams_with_event_count_with_groups_in_period": get_teams_with_event_count_with_groups_in_period(
            period_start, period_end
        ),
        "teams_with_event_count_from_helicone_in_period": all_metrics["helicone_events"],
        "teams_with_event_count_from_langfuse_in_period": all_metrics["langfuse_events"],
        "teams_with_event_count_from_keywords_ai_in_period": all_metrics["keywords_ai_events"],
        "teams_with_event_count_from_traceloop_in_period": all_metrics["traceloop_events"],
        "teams_with_web_events_count_in_period": all_metrics["web_events"],
        "teams_with_web_lite_events_count_in_period": all_metrics["web_lite_events"],
        "teams_with_node_events_count_in_period": all_metrics["node_events"],
        "teams_with_android_events_count_in_period": all_metrics["android_events"],
        "teams_with_flutter_events_count_in_period": all_metrics["flutter_events"],
        "teams_with_ios_events_count_in_period": all_metrics["ios_events"],
        "teams_with_go_events_count_in_period": all_metrics["go_events"],
        "teams_with_java_events_count_in_period": all_metrics["java_events"],
        "teams_with_react_native_events_count_in_period": all_metrics["react_native_events"],
        "teams_with_ruby_events_count_in_period": all_metrics["ruby_events"],
        "teams_with_python_events_count_in_period": all_metrics["python_events"],
        "teams_with_php_events_count_in_period": all_metrics["php_events"],
        "teams_with_recording_count_in_period": get_teams_with_recording_count_in_period(
            period_start, period_end, snapshot_source="web"
        ),
        "teams_with_mobile_recording_count_in_period": get_teams_with_recording_count_in_period(
            period_start, period_end, snapshot_source="mobile"
        ),
        "teams_with_decide_requests_count_in_period": get_teams_with_feature_flag_requests_count_in_period(
            period_start, period_end, FlagRequestType.DECIDE
        ),
        "teams_with_local_evaluation_requests_count_in_period": get_teams_with_feature_flag_requests_count_in_period(
            period_start, period_end, FlagRequestType.LOCAL_EVALUATION
        ),
        "teams_with_group_types_total": list(
            GroupTypeMapping.objects.values("team_id").annotate(total=Count("id")).order_by("team_id")
        ),
        "teams_with_dashboard_count": list(
            Dashboard.objects.values("team_id").annotate(total=Count("id")).order_by("team_id")
        ),
        "teams_with_dashboard_template_count": list(
            Dashboard.objects.filter(creation_mode="template")
            .values("team_id")
            .annotate(total=Count("id"))
            .order_by("team_id")
        ),
        "teams_with_dashboard_shared_count": list(
            Dashboard.objects.filter(sharingconfiguration__enabled=True)
            .values("team_id")
            .annotate(total=Count("id"))
            .order_by("team_id")
        ),
        "teams_with_dashboard_tagged_count": list(
            Dashboard.objects.filter(tagged_items__isnull=False)
            .values("team_id")
            .annotate(total=Count("id"))
            .order_by("team_id")
        ),
        "teams_with_ff_count": list(
            FeatureFlag.objects.values("team_id").annotate(total=Count("id")).order_by("team_id")
        ),
        "teams_with_ff_active_count": list(
            FeatureFlag.objects.filter(active=True).values("team_id").annotate(total=Count("id")).order_by("team_id")
        ),
        "teams_with_query_app_bytes_read": get_teams_with_query_metric(
            period_start,
            period_end,
            metric="read_bytes",
            access_method="",
        ),
        "teams_with_query_app_rows_read": get_teams_with_query_metric(
            period_start,
            period_end,
            metric="read_rows",
            access_method="",
        ),
        "teams_with_query_app_duration_ms": get_teams_with_query_metric(
            period_start,
            period_end,
            metric="query_duration_ms",
            access_method="",
        ),
        "teams_with_query_api_bytes_read": get_teams_with_query_metric(
            period_start,
            period_end,
            metric="read_bytes",
            access_method="personal_api_key",
        ),
        "teams_with_query_api_rows_read": get_teams_with_query_metric(
            period_start,
            period_end,
            metric="read_rows",
            access_method="personal_api_key",
        ),
        "teams_with_query_api_duration_ms": get_teams_with_query_metric(
            period_start,
            period_end,
            metric="query_duration_ms",
            access_method="personal_api_key",
        ),
        "teams_with_event_explorer_app_bytes_read": get_teams_with_query_metric(
            period_start,
            period_end,
            metric="read_bytes",
            query_types=["EventsQuery"],
            access_method="",
        ),
        "teams_with_event_explorer_app_rows_read": get_teams_with_query_metric(
            period_start,
            period_end,
            metric="read_rows",
            query_types=["EventsQuery"],
            access_method="",
        ),
        "teams_with_event_explorer_app_duration_ms": get_teams_with_query_metric(
            period_start,
            period_end,
            metric="query_duration_ms",
            query_types=["EventsQuery"],
            access_method="",
        ),
        "teams_with_event_explorer_api_bytes_read": get_teams_with_query_metric(
            period_start,
            period_end,
            metric="read_bytes",
            query_types=["EventsQuery"],
            access_method="personal_api_key",
        ),
        "teams_with_event_explorer_api_rows_read": get_teams_with_query_metric(
            period_start,
            period_end,
            metric="read_rows",
            query_types=["EventsQuery"],
            access_method="personal_api_key",
        ),
        "teams_with_event_explorer_api_duration_ms": get_teams_with_query_metric(
            period_start,
            period_end,
            metric="query_duration_ms",
            query_types=["EventsQuery"],
            access_method="personal_api_key",
        ),
        "teams_with_survey_responses_count_in_period": get_teams_with_survey_responses_count_in_period(
            period_start, period_end
        ),
        "teams_with_rows_synced_in_period": get_teams_with_rows_synced_in_period(period_start, period_end),
        "teams_with_hog_function_calls_in_period": get_teams_with_hog_function_calls_in_period(
            period_start, period_end
        ),
        "teams_with_hog_function_fetch_calls_in_period": get_teams_with_hog_function_fetch_calls_in_period(
            period_start, period_end
        ),
    }


def _get_all_digest_data(period_start: datetime, period_end: datetime) -> dict[str, Any]:
    return {
        "teams_with_new_dashboards_in_last_7_days": get_teams_with_new_dashboards_in_last_7_days(period_end),
        "teams_with_new_event_definitions_in_last_7_days": get_teams_with_new_event_definitions_in_last_7_days(
            period_end
        ),
        "teams_with_new_playlists_created_in_last_7_days": get_teams_with_new_playlists_created_in_last_7_days(
            period_end
        ),
        "teams_with_new_experiments_launched_in_last_7_days": get_teams_with_new_experiments_launched_in_last_7_days(
            period_end
        ),
        "teams_with_new_experiments_completed_in_last_7_days": get_teams_with_new_experiments_completed_in_last_7_days(
            period_end
        ),
        "teams_with_new_external_data_sources_connected_in_last_7_days": get_teams_with_new_external_data_sources_connected_in_last_7_days(
            period_end
        ),
        "teams_with_new_surveys_launched_in_last_7_days": get_teams_with_new_surveys_launched_in_last_7_days(
            period_end
        ),
        "teams_with_new_feature_flags_created_in_last_7_days": get_teams_with_new_feature_flags_created_in_last_7_days(
            period_end
        ),
    }


def _get_all_usage_data_as_team_rows(period_start: datetime, period_end: datetime) -> dict[str, Any]:
    """
    Gets all usage data for the specified period as a map of team_id -> value. This makes it faster
    to access the data than looping over all_data to find what we want.
    """
    all_data = _get_all_usage_data(period_start, period_end)
    # convert it to a map of team_id -> value
    for key, rows in all_data.items():
        all_data[key] = convert_team_usage_rows_to_dict(rows)
    return all_data


def _get_teams_for_usage_reports() -> Sequence[Team]:
    return list(
        Team.objects.select_related("organization")
        .exclude(Q(organization__for_internal_metrics=True) | Q(is_demo=True))
        .only("id", "name", "organization__id", "organization__name", "organization__created_at")
    )


def _get_team_report(all_data: dict[str, Any], team: Team) -> UsageReportCounters:
    decide_requests_count_in_period = all_data["teams_with_decide_requests_count_in_period"].get(team.id, 0)
    local_evaluation_requests_count_in_period = all_data["teams_with_local_evaluation_requests_count_in_period"].get(
        team.id, 0
    )
    return UsageReportCounters(
        event_count_in_period=all_data["teams_with_event_count_in_period"].get(team.id, 0),
        enhanced_persons_event_count_in_period=all_data["teams_with_enhanced_persons_event_count_in_period"].get(
            team.id, 0
        ),
        event_count_with_groups_in_period=all_data["teams_with_event_count_with_groups_in_period"].get(team.id, 0),
        event_count_from_langfuse_in_period=all_data["teams_with_event_count_from_langfuse_in_period"].get(team.id, 0),
        event_count_from_traceloop_in_period=all_data["teams_with_event_count_from_traceloop_in_period"].get(
            team.id, 0
        ),
        event_count_from_helicone_in_period=all_data["teams_with_event_count_from_helicone_in_period"].get(team.id, 0),
        event_count_from_keywords_ai_in_period=all_data["teams_with_event_count_from_keywords_ai_in_period"].get(
            team.id, 0
        ),
        recording_count_in_period=all_data["teams_with_recording_count_in_period"].get(team.id, 0),
        mobile_recording_count_in_period=all_data["teams_with_mobile_recording_count_in_period"].get(team.id, 0),
        group_types_total=all_data["teams_with_group_types_total"].get(team.id, 0),
        decide_requests_count_in_period=decide_requests_count_in_period,
        local_evaluation_requests_count_in_period=local_evaluation_requests_count_in_period,
        billable_feature_flag_requests_count_in_period=decide_requests_count_in_period
        + (local_evaluation_requests_count_in_period * 10),
        dashboard_count=all_data["teams_with_dashboard_count"].get(team.id, 0),
        dashboard_template_count=all_data["teams_with_dashboard_template_count"].get(team.id, 0),
        dashboard_shared_count=all_data["teams_with_dashboard_shared_count"].get(team.id, 0),
        dashboard_tagged_count=all_data["teams_with_dashboard_tagged_count"].get(team.id, 0),
        ff_count=all_data["teams_with_ff_count"].get(team.id, 0),
        ff_active_count=all_data["teams_with_ff_active_count"].get(team.id, 0),
        query_app_bytes_read=all_data["teams_with_query_app_bytes_read"].get(team.id, 0),
        query_app_rows_read=all_data["teams_with_query_app_rows_read"].get(team.id, 0),
        query_app_duration_ms=all_data["teams_with_query_app_duration_ms"].get(team.id, 0),
        query_api_bytes_read=all_data["teams_with_query_api_bytes_read"].get(team.id, 0),
        query_api_rows_read=all_data["teams_with_query_api_rows_read"].get(team.id, 0),
        query_api_duration_ms=all_data["teams_with_query_api_duration_ms"].get(team.id, 0),
        event_explorer_app_bytes_read=all_data["teams_with_event_explorer_app_bytes_read"].get(team.id, 0),
        event_explorer_app_rows_read=all_data["teams_with_event_explorer_app_rows_read"].get(team.id, 0),
        event_explorer_app_duration_ms=all_data["teams_with_event_explorer_app_duration_ms"].get(team.id, 0),
        event_explorer_api_bytes_read=all_data["teams_with_event_explorer_api_bytes_read"].get(team.id, 0),
        event_explorer_api_rows_read=all_data["teams_with_event_explorer_api_rows_read"].get(team.id, 0),
        event_explorer_api_duration_ms=all_data["teams_with_event_explorer_api_duration_ms"].get(team.id, 0),
        survey_responses_count_in_period=all_data["teams_with_survey_responses_count_in_period"].get(team.id, 0),
        rows_synced_in_period=all_data["teams_with_rows_synced_in_period"].get(team.id, 0),
        hog_function_calls_in_period=all_data["teams_with_hog_function_calls_in_period"].get(team.id, 0),
        hog_function_fetch_calls_in_period=all_data["teams_with_hog_function_fetch_calls_in_period"].get(team.id, 0),
        web_events_count_in_period=all_data["teams_with_web_events_count_in_period"].get(team.id, 0),
        web_lite_events_count_in_period=all_data["teams_with_web_lite_events_count_in_period"].get(team.id, 0),
        node_events_count_in_period=all_data["teams_with_node_events_count_in_period"].get(team.id, 0),
        android_events_count_in_period=all_data["teams_with_android_events_count_in_period"].get(team.id, 0),
        flutter_events_count_in_period=all_data["teams_with_flutter_events_count_in_period"].get(team.id, 0),
        ios_events_count_in_period=all_data["teams_with_ios_events_count_in_period"].get(team.id, 0),
        go_events_count_in_period=all_data["teams_with_go_events_count_in_period"].get(team.id, 0),
        java_events_count_in_period=all_data["teams_with_java_events_count_in_period"].get(team.id, 0),
        react_native_events_count_in_period=all_data["teams_with_react_native_events_count_in_period"].get(team.id, 0),
        ruby_events_count_in_period=all_data["teams_with_ruby_events_count_in_period"].get(team.id, 0),
        python_events_count_in_period=all_data["teams_with_python_events_count_in_period"].get(team.id, 0),
        php_events_count_in_period=all_data["teams_with_php_events_count_in_period"].get(team.id, 0),
    )


def _get_all_digest_data_as_team_rows(period_start: datetime, period_end: datetime) -> dict[str, Any]:
    all_digest_data = _get_all_digest_data(period_start, period_end)
    # convert it to a map of team_id -> value
    for key, rows in all_digest_data.items():
        all_digest_data[key] = convert_team_digest_items_to_dict(rows)
    return all_digest_data


def _get_weekly_digest_report(all_digest_data: dict[str, Any], team: Team) -> WeeklyDigestReport:
    report = WeeklyDigestReport(
        new_dashboards_in_last_7_days=[
            {"name": dashboard.get("name"), "id": dashboard.get("id")}
            for dashboard in all_digest_data["teams_with_new_dashboards_in_last_7_days"].get(team.id, [])
        ],
        new_event_definitions_in_last_7_days=[
            {"name": event_definition.get("name"), "id": event_definition.get("id")}
            for event_definition in all_digest_data["teams_with_new_event_definitions_in_last_7_days"].get(team.id, [])
        ],
        new_playlists_created_in_last_7_days=[
            {"name": playlist.get("name"), "id": playlist.get("short_id")}
            for playlist in all_digest_data["teams_with_new_playlists_created_in_last_7_days"].get(team.id, [])
        ],
        new_experiments_launched_in_last_7_days=[
            {
                "name": experiment.get("name"),
                "id": experiment.get("id"),
                "start_date": experiment.get("start_date").isoformat(),
            }
            for experiment in all_digest_data["teams_with_new_experiments_launched_in_last_7_days"].get(team.id, [])
        ],
        new_experiments_completed_in_last_7_days=[
            {
                "name": experiment.get("name"),
                "id": experiment.get("id"),
                "start_date": experiment.get("start_date").isoformat(),
                "end_date": experiment.get("end_date").isoformat(),
            }
            for experiment in all_digest_data["teams_with_new_experiments_completed_in_last_7_days"].get(team.id, [])
        ],
        new_external_data_sources_connected_in_last_7_days=[
            {"source_type": source.get("source_type"), "id": source.get("id")}
            for source in all_digest_data["teams_with_new_external_data_sources_connected_in_last_7_days"].get(
                team.id, []
            )
        ],
        new_surveys_launched_in_last_7_days=[
            {
                "name": survey.get("name"),
                "id": survey.get("id"),
                "start_date": survey.get("start_date").isoformat(),
                "description": survey.get("description"),
            }
            for survey in all_digest_data["teams_with_new_surveys_launched_in_last_7_days"].get(team.id, [])
        ],
        new_feature_flags_created_in_last_7_days=[
            {"name": feature_flag.get("name"), "id": feature_flag.get("id"), "key": feature_flag.get("key")}
            for feature_flag in all_digest_data["teams_with_new_feature_flags_created_in_last_7_days"].get(team.id, [])
        ],
    )
    return report


def _add_team_report_to_org_reports(
    org_reports: dict[str, OrgReport],
    team: Team,
    team_report: UsageReportCounters,
    period_start: datetime,
) -> None:
    org_id = str(team.organization.id)
    if org_id not in org_reports:
        org_report = OrgReport(
            date=period_start.strftime("%Y-%m-%d"),
            organization_id=org_id,
            organization_name=team.organization.name,
            organization_created_at=team.organization.created_at.isoformat(),
            organization_user_count=get_org_user_count(org_id),
            team_count=1,
            teams={str(team.id): team_report},
            **dataclasses.asdict(team_report),  # Clone the team report as the basis
        )
        org_reports[org_id] = org_report
    else:
        org_report = org_reports[org_id]
        org_report.teams[str(team.id)] = team_report
        org_report.team_count += 1

        # Iterate on all fields of the UsageReportCounters and add the values from the team report to the org report
        for field in dataclasses.fields(UsageReportCounters):
            if hasattr(team_report, field.name):
                setattr(
                    org_report,
                    field.name,
                    getattr(org_report, field.name) + getattr(team_report, field.name),
                )


def _get_all_org_reports(period_start: datetime, period_end: datetime) -> dict[str, OrgReport]:
    logger.info("Getting all usage data...")  # noqa T201
    time_now = datetime.now()
    all_data = _get_all_usage_data_as_team_rows(period_start, period_end)
    all_digest_data = None
    if datetime.now().weekday() == 0:
        logger.debug("Getting all digest data...")  # noqa T201
        all_digest_data = _get_all_digest_data_as_team_rows(period_start, period_end)

    logger.debug(f"Getting all usage data took {(datetime.now() - time_now).total_seconds()} seconds.")  # noqa T201

    logger.info("Getting teams for usage reports...")  # noqa T201
    time_now = datetime.now()
    teams = _get_teams_for_usage_reports()
    logger.debug(f"Getting teams for usage reports took {(datetime.now() - time_now).total_seconds()} seconds.")  # noqa T201

    org_reports: dict[str, OrgReport] = {}

    logger.info("Generating reports for teams...")  # noqa T201
    time_now = datetime.now()
    for team in teams:
        team_report = _get_team_report(all_data, team)
        _add_team_report_to_org_reports(org_reports, team, team_report, period_start)

        # on mondays, send the weekly digest report
        if datetime.now().weekday() == 0 and all_digest_data:
            weekly_digest_report = _get_weekly_digest_report(all_digest_data, team)
            if has_non_zero_digest(weekly_digest_report):
                _send_weekly_digest_report(
                    team_id=team.id, team_name=team.name, weekly_digest_report=weekly_digest_report
                )

    time_since = datetime.now() - time_now
    logger.debug(f"Generating reports for teams took {time_since.total_seconds()} seconds.")  # noqa T201
    return org_reports


def _get_full_org_usage_report(org_report: OrgReport, instance_metadata: InstanceMetadata) -> FullUsageReport:
    return FullUsageReport(
        **dataclasses.asdict(org_report),
        **dataclasses.asdict(instance_metadata),
    )


def _get_full_org_usage_report_as_dict(full_report: FullUsageReport) -> dict[str, Any]:
    return dataclasses.asdict(full_report)


@shared_task(**USAGE_REPORT_TASK_KWARGS, max_retries=3)
def _send_weekly_digest_report(*, team_id: int, team_name: str, weekly_digest_report: WeeklyDigestReport) -> None:
    full_report_dict = {
        "team_id": team_id,
        "team_name": team_name,
        **dataclasses.asdict(weekly_digest_report),
    }
    capture_report.delay(
        capture_event_name="weekly digest report",
        team_id=team_id,
        full_report_dict=full_report_dict,
        send_for_all_members=True,
    )


@shared_task(**USAGE_REPORT_TASK_KWARGS, max_retries=3)
def send_all_org_usage_reports(
    dry_run: bool = False,
    at: Optional[str] = None,
    capture_event_name: Optional[str] = None,
    skip_capture_event: bool = False,
    only_organization_id: Optional[str] = None,
) -> None:
    capture_event_name = capture_event_name or "organization usage report"

    at_date = parser.parse(at) if at else None
    period = get_previous_day(at=at_date)
    period_start, period_end = period

    instance_metadata = get_instance_metadata(period)

    try:
        org_reports = _get_all_org_reports(period_start, period_end)

        logger.info("Sending usage reports to PostHog and Billing...")  # noqa T201
        time_now = datetime.now()
        for org_report in org_reports.values():
            org_id = org_report.organization_id

            if only_organization_id and only_organization_id != org_id:
                continue

            full_report = _get_full_org_usage_report(org_report, instance_metadata)
            full_report_dict = _get_full_org_usage_report_as_dict(full_report)

            if dry_run:
                continue

            # First capture the events to PostHog
            if not skip_capture_event:
                at_date_str = at_date.isoformat() if at_date else None
                capture_report.delay(
                    capture_event_name=capture_event_name,
                    org_id=org_id,
                    full_report_dict=full_report_dict,
                    at_date=at_date_str,
                )

            # Then capture the events to Billing
            if has_non_zero_usage(full_report):
                send_report_to_billing_service.delay(org_id, full_report_dict)
        time_since = datetime.now() - time_now
        logger.debug(f"Sending usage reports to PostHog and Billing took {time_since.total_seconds()} seconds.")  # noqa T201
    except Exception as err:
        capture_exception(err)
        raise
