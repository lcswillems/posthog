import time
from typing import Optional
from uuid import UUID

from celery import shared_task
from django.conf import settings
from django.db import connection
from django.utils import timezone
from prometheus_client import Gauge
from redis import Redis
from structlog import get_logger

from posthog.clickhouse.client.limit import limit_concurrency, CeleryConcurrencyLimitExceeded
from posthog.cloud_utils import is_cloud
from posthog.errors import CHQueryErrorTooManySimultaneousQueries
from posthog.hogql.constants import LimitContext
from posthog.metrics import pushed_metrics_registry
from posthog.ph_client import get_ph_client
from posthog.redis import get_client
from posthog.tasks.utils import CeleryQueue

logger = get_logger(__name__)


@shared_task(ignore_result=True)
def delete_expired_exported_assets() -> None:
    from posthog.models import ExportedAsset

    ExportedAsset.delete_expired_assets()


@shared_task(ignore_result=True)
def redis_heartbeat() -> None:
    get_client().set("POSTHOG_HEARTBEAT", int(time.time()))


@shared_task(
    ignore_result=True,
    queue=CeleryQueue.ANALYTICS_QUERIES.value,
    acks_late=True,
    autoretry_for=(
        # Important: Only retry for things that might be okay on the next try
        CHQueryErrorTooManySimultaneousQueries,
        CeleryConcurrencyLimitExceeded,
    ),
    retry_backoff=1,
    retry_backoff_max=10,
    max_retries=3,
    expires=60 * 10,  # Do not run queries that got stuck for more than this
    reject_on_worker_lost=True,
)
@limit_concurrency(90)  # Do not go above what CH can handle (max_concurrent_queries)
@limit_concurrency(
    10, key=lambda *args, **kwargs: kwargs.get("team_id") or args[0]
)  # Do not run too many queries at once for the same team
def process_query_task(
    team_id: int,
    user_id: Optional[int],
    query_id: str,
    query_json: dict,
    limit_context: Optional[LimitContext] = None,
    refresh_requested: bool = False,  # TODO: Remove this parameter after the next deploy
) -> None:
    """
    Kick off query
    Once complete save results to redis
    """
    from posthog.client import execute_process_query

    execute_process_query(
        team_id=team_id,
        user_id=user_id,
        query_id=query_id,
        query_json=query_json,
        limit_context=limit_context,
    )


@shared_task(ignore_result=True)
def pg_table_cache_hit_rate() -> None:
    from statshog.defaults.django import statsd

    with connection.cursor() as cursor:
        try:
            cursor.execute(
                """
                SELECT
                 relname as table_name,
                 sum(heap_blks_hit) / nullif(sum(heap_blks_hit) + sum(heap_blks_read),0) * 100 AS ratio
                FROM pg_statio_user_tables
                GROUP BY relname
                ORDER BY ratio ASC
            """
            )
            tables = cursor.fetchall()
            with pushed_metrics_registry("celery_pg_table_cache_hit_rate") as registry:
                hit_rate_gauge = Gauge(
                    "posthog_celery_pg_table_cache_hit_rate",
                    "Postgres query cache hit rate per table.",
                    labelnames=["table_name"],
                    registry=registry,
                )
                for row in tables:
                    hit_rate_gauge.labels(table_name=row[0]).set(float(row[1]))
                    statsd.gauge("pg_table_cache_hit_rate", float(row[1]), tags={"table": row[0]})
        except:
            # if this doesn't work keep going
            pass


@shared_task(ignore_result=True)
def pg_plugin_server_query_timing() -> None:
    from statshog.defaults.django import statsd

    with connection.cursor() as cursor:
        try:
            cursor.execute(
                """
                SELECT
                    substring(query from 'plugin-server:(\\w+)') AS query_type,
                    total_time as total_time,
                    (total_time / calls) as avg_time,
                    min_time,
                    max_time,
                    stddev_time,
                    calls,
                    rows as rows_read_or_affected
                FROM pg_stat_statements
                WHERE query LIKE '%%plugin-server%%'
                ORDER BY total_time DESC
                LIMIT 50
                """
            )

            for row in cursor.fetchall():
                row_dictionary = {column.name: value for column, value in zip(cursor.description, row)}

                for key, value in row_dictionary.items():
                    if key == "query_type":
                        continue
                    statsd.gauge(
                        f"pg_plugin_server_query_{key}",
                        value,
                        tags={"query_type": row_dictionary["query_type"]},
                    )
        except:
            # if this doesn't work keep going
            pass


POSTGRES_TABLES = ["posthog_personoverride", "posthog_personoverridemapping"]


@shared_task(ignore_result=True)
def pg_row_count() -> None:
    with pushed_metrics_registry("celery_pg_row_count") as registry:
        row_count_gauge = Gauge(
            "posthog_celery_pg_table_row_count",
            "Number of rows per Postgres table.",
            labelnames=["table_name"],
            registry=registry,
        )
        with connection.cursor() as cursor:
            for table in POSTGRES_TABLES:
                QUERY = "SELECT count(*) FROM {table};"
                query = QUERY.format(table=table)

                try:
                    cursor.execute(query)
                    row = cursor.fetchone()
                    row_count_gauge.labels(table_name=table).set(row[0])
                except:
                    pass


CLICKHOUSE_TABLES = [
    "sharded_events",
    "person",
    "person_distinct_id2",
    "sharded_session_replay_events",
    "log_entries",
]

HEARTBEAT_EVENT_TO_INGESTION_LAG_METRIC = {
    "heartbeat": "ingestion",
    "heartbeat_buffer": "ingestion_buffer",
    "heartbeat_api": "ingestion_api",
}


@shared_task(ignore_result=True)
def ingestion_lag() -> None:
    from statshog.defaults.django import statsd

    from posthog.client import sync_execute

    # Requires https://github.com/PostHog/posthog-heartbeat-plugin to be enabled on team 2
    # Note that it runs every minute, and we compare it with now(), so there's up to 60s delay
    query = """
    SELECT event, date_diff('second', max(timestamp), now())
    FROM events
    WHERE team_id IN %(team_ids)s
        AND event IN %(events)s
        AND timestamp > yesterday() AND timestamp < now() + toIntervalMinute(3)
    GROUP BY event
    """

    try:
        results = sync_execute(
            query,
            {
                "team_ids": settings.INGESTION_LAG_METRIC_TEAM_IDS,
                "events": list(HEARTBEAT_EVENT_TO_INGESTION_LAG_METRIC.keys()),
            },
        )
        with pushed_metrics_registry("celery_ingestion_lag") as registry:
            lag_gauge = Gauge(
                "posthog_celery_observed_ingestion_lag_seconds",
                "End-to-end ingestion lag observed through several scenarios. Can be overestimated by up to 60 seconds.",
                labelnames=["scenario"],
                registry=registry,
            )
            for event, lag in results:
                metric = HEARTBEAT_EVENT_TO_INGESTION_LAG_METRIC[event]
                statsd.gauge(f"posthog_celery_{metric}_lag_seconds_rough_minute_precision", lag)
                lag_gauge.labels(scenario=metric).set(lag)
    except:
        pass


@shared_task(ignore_result=True)
def invalid_web_replays() -> None:
    from posthog.client import sync_execute

    # ultimately I want to observe values by team id, but at the moment that would be lots of series, let's reduce the value first
    query = """
    select
        --team_id,
        count()
    from (
        select any(team_id) as team_id, argMinMerge(first_url) as first_url, argMinMerge(snapshot_source) as snapshot_source
        from session_replay_events
        where min_first_timestamp >= now() - interval 1 hour
        and min_first_timestamp <= now()
        group by session_id
        having first_url is null and snapshot_source = 'web'
    )
    --group by team_id
    """

    try:
        results = sync_execute(
            query,
        )
        with pushed_metrics_registry("celery_replay_tracking") as registry:
            gauge = Gauge(
                "replay_tracking_web_replay_with_missing_first_url",
                "Acts as a proxy for replay sessions which haven't received a full snapshot",
                registry=registry,
            )
            count = results[0][0]
            gauge.set(count)
    except:
        pass


KNOWN_CELERY_TASK_IDENTIFIERS = {
    "pluginJob",
    "runEveryHour",
    "runEveryMinute",
    "runEveryDay",
}


@shared_task(ignore_result=True)
def graphile_worker_queue_size() -> None:
    from django.db import connections
    from statshog.defaults.django import statsd

    connection = connections["graphile"] if "graphile" in connections else connections["default"]
    with connection.cursor() as cursor:
        cursor.execute(
            """
        SELECT count(*)
        FROM graphile_worker.jobs
        WHERE (jobs.locked_at is null or jobs.locked_at < (now() - INTERVAL '4 hours'))
        AND run_at <= now()
        AND attempts < max_attempts
        """
        )

        queue_size = cursor.fetchone()[0]
        statsd.gauge("graphile_worker_queue_size", queue_size)

        # Track the number of jobs that will still be run at least once or are currently running based on job type (i.e. task_identifier)
        # Completed jobs are deleted and "permanently failed" jobs have attempts == max_attempts
        # Jobs not yet eligible for execution are filtered out with run_at <= now()
        cursor.execute(
            """
        SELECT task_identifier, count(*) as c, EXTRACT(EPOCH FROM MIN(run_at)) as oldest FROM graphile_worker.jobs
        WHERE attempts < max_attempts
        AND run_at <= now()
        GROUP BY task_identifier
        """
        )

        seen_task_identifier = set()
        with pushed_metrics_registry("celery_graphile_worker_queue_size") as registry:
            processing_lag_gauge = Gauge(
                "posthog_celery_graphile_lag_seconds",
                "Oldest scheduled run on pending Graphile jobs per task identifier, zero if queue empty.",
                labelnames=["task_identifier"],
                registry=registry,
            )
            waiting_jobs_gauge = Gauge(
                "posthog_celery_graphile_waiting_jobs",
                "Number of Graphile jobs in the queue, per task identifier.",
                labelnames=["task_identifier"],
                registry=registry,
            )
            for task_identifier, count, oldest in cursor.fetchall():
                seen_task_identifier.add(task_identifier)
                waiting_jobs_gauge.labels(task_identifier=task_identifier).set(count)
                processing_lag_gauge.labels(task_identifier=task_identifier).set(time.time() - float(oldest))
                statsd.gauge(
                    "graphile_waiting_jobs",
                    count,
                    tags={"task_identifier": task_identifier},
                )

            # The query will not return rows for empty queues, creating missing points.
            # Let's emit updates for known queues even if they are empty.
            for task_identifier in KNOWN_CELERY_TASK_IDENTIFIERS - seen_task_identifier:
                waiting_jobs_gauge.labels(task_identifier=task_identifier).set(0)
                processing_lag_gauge.labels(task_identifier=task_identifier).set(0)


@shared_task(ignore_result=True)
def clickhouse_row_count() -> None:
    from statshog.defaults.django import statsd

    from posthog.client import sync_execute

    with pushed_metrics_registry("celery_clickhouse_row_count") as registry:
        row_count_gauge = Gauge(
            "posthog_celery_clickhouse_table_row_count",
            "Number of rows per ClickHouse table.",
            labelnames=["table_name"],
            registry=registry,
        )
        for table in CLICKHOUSE_TABLES:
            try:
                QUERY = """SELECT sum(rows) rows from system.parts
                       WHERE table = '{table}' and active;"""
                query = QUERY.format(table=table)
                rows = sync_execute(query)[0][0]
                row_count_gauge.labels(table_name=table).set(rows)
                statsd.gauge(
                    f"posthog_celery_clickhouse_table_row_count",
                    rows,
                    tags={"table": table},
                )
            except:
                pass


@shared_task(ignore_result=True)
def clickhouse_errors_count() -> None:
    """
    This task is used to track the recency of errors in ClickHouse.
    We can use this to alert on errors that are consistently being generated recently
    999 - KEEPER_EXCEPTION
    225 - NO_ZOOKEEPER
    242 - TABLE_IS_READ_ONLY
    """
    from posthog.client import sync_execute

    QUERY = """
        select
            getMacro('replica') replica,
            getMacro('shard') shard,
            name,
            value as errors,
            dateDiff('minute', last_error_time, now()) minutes_ago
        from clusterAllReplicas('posthog', system, errors)
        where code in (999, 225, 242)
        order by minutes_ago
    """
    rows = sync_execute(QUERY)
    with pushed_metrics_registry("celery_clickhouse_errors") as registry:
        errors_gauge = Gauge(
            "posthog_celery_clickhouse_errors",
            "Age of the latest error per ClickHouse errors table.",
            registry=registry,
            labelnames=["replica", "shard", "name"],
        )
        if isinstance(rows, list):
            for replica, shard, name, _, minutes_ago in rows:
                errors_gauge.labels(replica=replica, shard=shard, name=name).set(minutes_ago)


@shared_task(ignore_result=True)
def clickhouse_part_count() -> None:
    from statshog.defaults.django import statsd

    from posthog.client import sync_execute

    QUERY = """
        SELECT table, count(1) freq
        FROM system.parts
        WHERE active
        GROUP BY table
        ORDER BY freq DESC;
    """
    rows = sync_execute(QUERY)

    with pushed_metrics_registry("celery_clickhouse_part_count") as registry:
        parts_count_gauge = Gauge(
            "posthog_celery_clickhouse_table_parts_count",
            "Number of parts per ClickHouse table.",
            labelnames=["table"],
            registry=registry,
        )
        for table, parts in rows:
            parts_count_gauge.labels(table=table).set(parts)
            statsd.gauge(
                f"posthog_celery_clickhouse_table_parts_count",
                parts,
                tags={"table": table},
            )


@shared_task(ignore_result=True)
def clickhouse_mutation_count() -> None:
    from statshog.defaults.django import statsd

    from posthog.client import sync_execute

    QUERY = """
        SELECT
            table,
            count(1) AS freq
        FROM system.mutations
        WHERE is_done = 0
        GROUP BY table
        ORDER BY freq DESC
    """
    rows = sync_execute(QUERY)

    with pushed_metrics_registry("celery_clickhouse_mutation_count") as registry:
        mutations_count_gauge = Gauge(
            "posthog_celery_clickhouse_table_mutations_count",
            "Number of mutations per ClickHouse table.",
            labelnames=["table"],
            registry=registry,
        )
    for table, muts in rows:
        mutations_count_gauge.labels(table=table).set(muts)
        statsd.gauge(
            f"posthog_celery_clickhouse_table_mutations_count",
            muts,
            tags={"table": table},
        )


@shared_task(ignore_result=True)
def clickhouse_clear_removed_data() -> None:
    from posthog.models.async_deletion.delete_cohorts import AsyncCohortDeletion
    from posthog.models.async_deletion.delete_events import AsyncEventDeletion

    runner = AsyncEventDeletion()

    try:
        runner.mark_deletions_done()
    except Exception as e:
        logger.error("Failed to mark deletions done", error=e, exc_info=True)

    try:
        runner.run()
    except Exception as e:
        logger.error("Failed to run deletions", error=e, exc_info=True)

    cohort_runner = AsyncCohortDeletion()

    try:
        cohort_runner.mark_deletions_done()
    except Exception as e:
        logger.error("Failed to mark cohort deletions done", error=e, exc_info=True)

    try:
        cohort_runner.run()
    except Exception as e:
        logger.error("Failed to run cohort deletions", error=e, exc_info=True)


@shared_task(ignore_result=True)
def clear_clickhouse_deleted_person() -> None:
    from posthog.models.async_deletion.delete_person import remove_deleted_person_data

    remove_deleted_person_data()


@shared_task(ignore_result=True, queue=CeleryQueue.STATS.value)
def redis_celery_queue_depth() -> None:
    try:
        with pushed_metrics_registry("redis_celery_queue_depth_registry") as registry:
            celery_task_queue_depth_gauge = Gauge(
                "posthog_celery_queue_depth",
                "We use this to monitor the depth of the celery queue.",
                registry=registry,
                labelnames=["queue_name"],
            )

            for queue in CeleryQueue:
                llen = get_client().llen(queue.value)
                celery_task_queue_depth_gauge.labels(queue_name=queue.value).set(llen)

    except:
        # if we can't generate the metric don't complain about it.
        return


@shared_task(ignore_result=True)
def update_event_partitions() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "DO $$ BEGIN IF (SELECT exists(select * from pg_proc where proname = 'update_partitions')) THEN PERFORM update_partitions(); END IF; END $$"
        )


@shared_task(ignore_result=True)
def clean_stale_partials() -> None:
    """Clean stale (meaning older than 7 days) partial social auth sessions."""
    from social_django.models import Partial

    Partial.objects.filter(timestamp__lt=timezone.now() - timezone.timedelta(7)).delete()


@shared_task(ignore_result=True)
def monitoring_check_clickhouse_schema_drift() -> None:
    from posthog.tasks.check_clickhouse_schema_drift import (
        check_clickhouse_schema_drift,
    )

    check_clickhouse_schema_drift()


@shared_task(ignore_result=True, queue=CeleryQueue.LONG_RUNNING.value)
def calculate_cohort() -> None:
    from posthog.tasks.calculate_cohort import calculate_cohorts

    calculate_cohorts()


class Polling:
    _SINGLETON_REDIS_KEY = "POLL_QUERY_PERFORMANCE_SINGLETON_REDIS_KEY"
    NANOSECONDS_IN_SECOND = int(1e9)
    TIME_BETWEEN_RUNS_SECONDS = 2
    SOFT_TIME_LIMIT_SECONDS = 10
    HARD_TIME_LIMIT_SECONDS = 12
    ASSUME_TASK_DEAD_SECONDS = 14  # the time after which we start a new task

    TIME_BETWEEN_RUNS_NANOSECONDS = NANOSECONDS_IN_SECOND * TIME_BETWEEN_RUNS_SECONDS
    ASSUME_TASK_DEAD_NANOSECONDS = NANOSECONDS_IN_SECOND * ASSUME_TASK_DEAD_SECONDS

    @staticmethod
    def _encode_redis_key(time_ns: int) -> bytes:
        return time_ns.to_bytes(8, "big")

    @staticmethod
    def _decode_redis_key(time_ns: bytes | None) -> int:
        return 0 if time_ns is None else int.from_bytes(time_ns, "big")

    @staticmethod
    def set_last_run_time(client: Redis, time_ns: int) -> None:
        client.set(Polling._SINGLETON_REDIS_KEY, Polling._encode_redis_key(time_ns))

    @staticmethod
    def get_last_run_time(client: Redis) -> int:
        return Polling._decode_redis_key(client.get(Polling._SINGLETON_REDIS_KEY))


@shared_task(
    ignore_result=True,
    max_retries=0,
    soft_time_limit=Polling.SOFT_TIME_LIMIT_SECONDS,
    time_limit=Polling.HARD_TIME_LIMIT_SECONDS,
)
def poll_query_performance(last_known_run_time_ns: int) -> None:
    start_time_ns = time.time_ns()

    try:
        redis_client = get_client()
        if Polling.get_last_run_time(redis_client) != last_known_run_time_ns:
            logger.error("Poll query performance task terminating: another poller is running")
            return
        Polling.set_last_run_time(redis_client, start_time_ns)
        from posthog.tasks.poll_query_performance import poll_query_performance as poll_query_performance_nontask

        poll_query_performance_nontask()
    except Exception as e:
        logger.error("Poll query performance failed", error=e)

    elapsed_ns = time.time_ns() - start_time_ns
    if elapsed_ns > Polling.TIME_BETWEEN_RUNS_NANOSECONDS:
        # right again right away if more than time_between_runs has elapsed
        poll_query_performance.delay(start_time_ns)
    else:
        # delay until time_between_runs has elapsed
        poll_query_performance.apply_async(
            args=[start_time_ns],
            countdown=((Polling.TIME_BETWEEN_RUNS_NANOSECONDS - elapsed_ns) / Polling.NANOSECONDS_IN_SECOND),
        )


@shared_task(ignore_result=True, max_retries=1)
def start_poll_query_performance() -> None:
    redis_client = get_client()
    last_run_start_time_ns = Polling.get_last_run_time(redis_client)
    now_ns: int = time.time_ns()
    try:
        # The key should never be in the future
        # If the key is in the future or more than 15 seconds in the past, start a worker
        if last_run_start_time_ns > now_ns + Polling.TIME_BETWEEN_RUNS_NANOSECONDS:
            logger.error("Restarting poll query performance because key is in future")
            poll_query_performance.delay(last_run_start_time_ns)
        elif now_ns - last_run_start_time_ns > Polling.ASSUME_TASK_DEAD_NANOSECONDS:
            logger.error("Restarting poll query performance because of a long delay")
            poll_query_performance.delay(last_run_start_time_ns)

    except Exception as e:
        logger.error("Restarting poll query performance because of an error", error=e)
        poll_query_performance.delay(last_run_start_time_ns)


@shared_task(ignore_result=True)
def process_scheduled_changes() -> None:
    from posthog.tasks.process_scheduled_changes import process_scheduled_changes

    process_scheduled_changes()


@shared_task(ignore_result=True)
def validate_proxy_domains() -> None:
    from posthog.tasks.validate_proxy_domains import validate_proxy_domains

    validate_proxy_domains()


@shared_task(ignore_result=True)
def sync_insight_cache_states_task() -> None:
    from posthog.caching.insight_caching_state import sync_insight_cache_states

    sync_insight_cache_states()


@shared_task(ignore_result=True)
def schedule_cache_updates_task() -> None:
    from posthog.caching.insight_cache import schedule_cache_updates

    schedule_cache_updates()


@shared_task(
    ignore_result=True,
    autoretry_for=(CHQueryErrorTooManySimultaneousQueries,),
    retry_backoff=10,
    retry_backoff_max=30,
    max_retries=3,
    retry_jitter=True,
    queue=CeleryQueue.LONG_RUNNING.value,
)
def update_cache_task(caching_state_id: UUID) -> None:
    from posthog.caching.insight_cache import update_cache

    update_cache(caching_state_id)


@shared_task(ignore_result=True)
def sync_insight_caching_state(
    team_id: int,
    insight_id: Optional[int] = None,
    dashboard_tile_id: Optional[int] = None,
) -> None:
    from posthog.caching.insight_caching_state import sync_insight_caching_state

    sync_insight_caching_state(team_id, insight_id, dashboard_tile_id)


@shared_task(ignore_result=True)
def calculate_decide_usage() -> None:
    from posthog.models.feature_flag.flag_analytics import (
        capture_usage_for_all_teams as capture_decide_usage_for_all_teams,
    )

    ph_client = get_ph_client()

    capture_decide_usage_for_all_teams(ph_client)

    ph_client.shutdown()


@shared_task(ignore_result=True)
def find_flags_with_enriched_analytics() -> None:
    from datetime import datetime, timedelta

    from posthog.models.feature_flag.flag_analytics import (
        find_flags_with_enriched_analytics,
    )

    end = datetime.now()
    begin = end - timedelta(hours=12)

    find_flags_with_enriched_analytics(begin, end)


@shared_task(ignore_result=True)
def demo_reset_master_team() -> None:
    from posthog.tasks.demo_reset_master_team import demo_reset_master_team

    if is_cloud() or settings.DEMO:
        demo_reset_master_team()


@shared_task(ignore_result=True)
def sync_all_organization_available_product_features() -> None:
    from posthog.tasks.sync_all_organization_available_product_features import (
        sync_all_organization_available_product_features,
    )

    sync_all_organization_available_product_features()


@shared_task(ignore_result=False, track_started=True, max_retries=0)
def check_async_migration_health() -> None:
    from posthog.tasks.async_migrations import check_async_migration_health

    check_async_migration_health()


@shared_task(ignore_result=True)
def verify_persons_data_in_sync() -> None:
    from posthog.tasks.verify_persons_data_in_sync import (
        verify_persons_data_in_sync as verify,
    )

    if not is_cloud():
        return

    verify()


@shared_task(ignrore_result=True)
def stop_surveys_reached_target() -> None:
    from posthog.tasks.stop_surveys_reached_target import stop_surveys_reached_target

    stop_surveys_reached_target()


@shared_task(ignrore_result=True)
def update_survey_iteration() -> None:
    from posthog.tasks.update_survey_iteration import update_survey_iteration

    update_survey_iteration()


def recompute_materialized_columns_enabled() -> bool:
    from posthog.models.instance_setting import get_instance_setting

    if get_instance_setting("MATERIALIZED_COLUMNS_ENABLED") and get_instance_setting(
        "COMPUTE_MATERIALIZED_COLUMNS_ENABLED"
    ):
        return True
    return False


@shared_task(ignore_result=True)
def clickhouse_materialize_columns() -> None:
    if recompute_materialized_columns_enabled():
        try:
            from ee.clickhouse.materialized_columns.analyze import (
                materialize_properties_task,
            )
        except ImportError:
            pass
        else:
            materialize_properties_task()


@shared_task(ignore_result=True)
def clickhouse_mark_all_materialized() -> None:
    if recompute_materialized_columns_enabled():
        try:
            from ee.tasks.materialized_columns import mark_all_materialized
        except ImportError:
            pass
        else:
            mark_all_materialized()


@shared_task(ignore_result=True, queue=CeleryQueue.USAGE_REPORTS.value)
def send_org_usage_reports() -> None:
    from posthog.tasks.usage_report import send_all_org_usage_reports

    send_all_org_usage_reports.delay()


@shared_task(ignore_result=True)
def update_quota_limiting() -> None:
    try:
        from ee.billing.quota_limiting import update_all_org_billing_quotas

        update_all_org_billing_quotas()
    except ImportError:
        pass


@shared_task(ignore_result=True)
def schedule_all_subscriptions() -> None:
    try:
        from ee.tasks.subscriptions import (
            schedule_all_subscriptions as _schedule_all_subscriptions,
        )
    except ImportError:
        pass
    else:
        _schedule_all_subscriptions()


@shared_task(ignore_result=True, retries=3)
def clickhouse_send_license_usage() -> None:
    try:
        if not is_cloud():
            from ee.tasks.send_license_usage import send_license_usage

            send_license_usage()
    except ImportError:
        pass


@shared_task(ignore_result=True)
def check_flags_to_rollback() -> None:
    try:
        from ee.tasks.auto_rollback_feature_flag import check_flags_to_rollback

        check_flags_to_rollback()
    except ImportError:
        pass


@shared_task(ignore_result=True)
def ee_persist_single_recording(id: str, team_id: int) -> None:
    try:
        from ee.session_recordings.persistence_tasks import persist_single_recording

        persist_single_recording(id, team_id)
    except ImportError:
        pass


@shared_task(ignore_result=True)
def ee_persist_finished_recordings() -> None:
    try:
        from ee.session_recordings.persistence_tasks import persist_finished_recordings
    except ImportError:
        pass
    else:
        persist_finished_recordings()


@shared_task(ignore_result=True)
def check_data_import_row_limits() -> None:
    try:
        from posthog.tasks.warehouse import check_synced_row_limits
    except ImportError:
        pass
    else:
        check_synced_row_limits()


# this task runs a CH query and triggers other tasks
# it can run on the default queue
@shared_task(ignore_result=True)
def calculate_replay_embeddings() -> None:
    try:
        from ee.tasks.replay import generate_recordings_embeddings_batch

        generate_recordings_embeddings_batch()
    except ImportError:
        pass
    except Exception as e:
        logger.error("Failed to calculate replay embeddings", error=e, exc_info=True)


# this task triggers other tasks
# it can run on the default queue
@shared_task(ignore_result=True)
def calculate_replay_error_clusters() -> None:
    try:
        from ee.tasks.replay import generate_replay_embedding_error_clusters

        generate_replay_embedding_error_clusters()
    except ImportError:
        pass
    except Exception as e:
        logger.error("Failed to calculate replay error clusters", error=e, exc_info=True)
