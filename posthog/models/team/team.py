import re
from decimal import Decimal
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Optional
from zoneinfo import ZoneInfo

import posthoganalytics
import pydantic
import pytz
from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.validators import (
    MaxValueValidator,
    MinLengthValidator,
    MinValueValidator,
)
from django.db import connection, models, transaction
from django.db.models import QuerySet
from django.db.models.signals import post_delete, post_save

from posthog.clickhouse.query_tagging import tag_queries
from posthog.cloud_utils import is_cloud
from posthog.helpers.dashboard_templates import create_dashboard_from_template
from posthog.models.dashboard import Dashboard
from posthog.models.filters.filter import Filter
from posthog.models.filters.mixins.utils import cached_property
from posthog.models.filters.utils import GroupTypeIndex
from posthog.models.instance_setting import get_instance_setting
from posthog.models.signals import mutable_receiver
from posthog.models.utils import (
    UUIDClassicModel,
    generate_random_token_project,
    sane_repr,
)
from posthog.settings.utils import get_list
from posthog.utils import GenericEmails

from ...hogql.modifiers import set_default_modifier_values
from ...schema import HogQLQueryModifiers, PathCleaningFilter, PersonsOnEventsMode
from .team_caching import get_team_in_cache, set_team_in_cache

if TYPE_CHECKING:
    from posthog.models.user import User

TIMEZONES = [(tz, tz) for tz in pytz.all_timezones]

# TODO: DEPRECATED; delete when these attributes can be fully removed from `Team` model
DEPRECATED_ATTRS = (
    "plugins_opt_in",
    "opt_out_capture",
    "event_names",
    "event_names_with_usage",
    "event_properties",
    "event_properties_with_usage",
    "event_properties_numerical",
)


# keep in sync with posthog/frontend/src/scenes/project/Settings/ExtraTeamSettings.tsx
class AvailableExtraSettings:
    pass


class TeamManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().defer(*DEPRECATED_ATTRS)

    def set_test_account_filters(self, organization: Optional[Any]) -> list:
        filters = [
            {
                "key": "$host",
                "operator": "not_regex",
                "value": r"^(localhost|127\.0\.0\.1)($|:)",
                "type": "event",
            }
        ]
        if organization:
            example_emails = organization.members.only("email")
            generic_emails = GenericEmails()
            example_emails = [email.email for email in example_emails if not generic_emails.is_generic(email.email)]
            if len(example_emails) > 0:
                example_email = re.search(r"@[\w.]+", example_emails[0])
                if example_email:
                    return [
                        {"key": "email", "operator": "not_icontains", "value": example_email.group(), "type": "person"},
                        *filters,
                    ]
        return filters

    def create_with_data(self, user: Any = None, default_dashboards: bool = True, **kwargs) -> "Team":
        kwargs["test_account_filters"] = self.set_test_account_filters(kwargs.get("organization"))
        team = Team.objects.create(**kwargs)

        # Create default dashboards (skipped for demo projects)
        if default_dashboards:
            dashboard = Dashboard.objects.create(name="My App Dashboard", pinned=True, team=team)
            create_dashboard_from_template("DEFAULT_APP", dashboard)
            team.primary_dashboard = dashboard
            team.save()
        return team

    def create(self, *args, **kwargs) -> "Team":
        from ..project import Project

        with transaction.atomic():
            if "id" not in kwargs:
                kwargs["id"] = self.increment_id_sequence()
            if kwargs.get("project") is None and kwargs.get("project_id") is None:
                # If a parent project is not provided for this team, ensure there is one
                # This should be removed once environments are fully rolled out
                project_kwargs = {}
                if organization := kwargs.get("organization"):
                    project_kwargs["organization"] = organization
                elif organization_id := kwargs.get("organization_id"):
                    project_kwargs["organization_id"] = organization_id
                if name := kwargs.get("name"):
                    project_kwargs["name"] = name
                kwargs["project"] = Project.objects.create(id=kwargs["id"], **project_kwargs)
            return super().create(*args, **kwargs)

    def get_team_from_token(self, token: Optional[str]) -> Optional["Team"]:
        if not token:
            return None
        try:
            return Team.objects.get(api_token=token)
        except Team.DoesNotExist:
            return None

    def get_team_from_cache_or_token(self, token: Optional[str]) -> Optional["Team"]:
        if not token:
            return None
        try:
            team = get_team_in_cache(token)
            if team:
                return team

            team = Team.objects.get(api_token=token)
            set_team_in_cache(token, team)
            return team

        except Team.DoesNotExist:
            return None

    def increment_id_sequence(self) -> int:
        """Increment the `Team.id` field's sequence and return the latest value.

        Use only when actually neeeded to avoid wasting sequence values."""
        cursor = connection.cursor()
        cursor.execute("SELECT nextval('posthog_team_id_seq')")
        result = cursor.fetchone()
        return result[0]


def get_default_data_attributes() -> list[str]:
    return ["data-attr"]


class WeekStartDay(models.IntegerChoices):
    SUNDAY = 0, "Sunday"
    MONDAY = 1, "Monday"

    @property
    def clickhouse_mode(self) -> str:
        return "3" if self == WeekStartDay.MONDAY else "0"


class Team(UUIDClassicModel):
    class Meta:
        verbose_name = "team (soon to be environment)"
        verbose_name_plural = "teams (soon to be environments)"

    organization: models.ForeignKey = models.ForeignKey(
        "posthog.Organization",
        on_delete=models.CASCADE,
        related_name="teams",
        related_query_name="team",
    )
    project: models.ForeignKey = models.ForeignKey(
        "posthog.Project",
        on_delete=models.CASCADE,
        related_name="teams",
        related_query_name="team",
    )
    api_token: models.CharField = models.CharField(
        max_length=200,
        unique=True,
        default=generate_random_token_project,
        validators=[MinLengthValidator(10, "Project's API token must be at least 10 characters long!")],
    )
    app_urls: ArrayField = ArrayField(models.CharField(max_length=200, null=True), default=list, blank=True)
    name: models.CharField = models.CharField(
        max_length=200,
        default="Default project",
        validators=[MinLengthValidator(1, "Project must have a name!")],
    )
    slack_incoming_webhook: models.CharField = models.CharField(max_length=500, null=True, blank=True)
    created_at: models.DateTimeField = models.DateTimeField(auto_now_add=True)
    updated_at: models.DateTimeField = models.DateTimeField(auto_now=True)
    anonymize_ips: models.BooleanField = models.BooleanField(default=False)
    completed_snippet_onboarding: models.BooleanField = models.BooleanField(default=False)
    has_completed_onboarding_for: models.JSONField = models.JSONField(null=True, blank=True)
    ingested_event: models.BooleanField = models.BooleanField(default=False)
    autocapture_opt_out: models.BooleanField = models.BooleanField(null=True, blank=True)
    autocapture_exceptions_opt_in: models.BooleanField = models.BooleanField(null=True, blank=True)
    autocapture_exceptions_errors_to_ignore: models.JSONField = models.JSONField(null=True, blank=True)
    session_recording_opt_in: models.BooleanField = models.BooleanField(default=False)
    session_recording_sample_rate: models.DecimalField = models.DecimalField(
        # will store a decimal between 0 and 1 allowing up to 2 decimal places
        null=True,
        blank=True,
        max_digits=3,
        decimal_places=2,
        validators=[MinValueValidator(Decimal(0)), MaxValueValidator(Decimal(1))],
    )
    session_recording_minimum_duration_milliseconds: models.IntegerField = models.IntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(15000)],
    )
    session_recording_linked_flag: models.JSONField = models.JSONField(null=True, blank=True)
    session_recording_network_payload_capture_config: models.JSONField = models.JSONField(null=True, blank=True)
    session_replay_config: models.JSONField = models.JSONField(null=True, blank=True)
    capture_console_log_opt_in: models.BooleanField = models.BooleanField(null=True, blank=True)
    capture_performance_opt_in: models.BooleanField = models.BooleanField(null=True, blank=True)
    surveys_opt_in: models.BooleanField = models.BooleanField(null=True, blank=True)
    heatmaps_opt_in: models.BooleanField = models.BooleanField(null=True, blank=True)
    session_recording_version: models.CharField = models.CharField(null=True, blank=True, max_length=24)
    signup_token: models.CharField = models.CharField(max_length=200, null=True, blank=True)
    is_demo: models.BooleanField = models.BooleanField(default=False)
    access_control: models.BooleanField = models.BooleanField(default=False)
    week_start_day: models.SmallIntegerField = models.SmallIntegerField(
        null=True, blank=True, choices=WeekStartDay.choices
    )
    # This is not a manual setting. It's updated automatically to reflect if the team uses site apps or not.
    inject_web_apps: models.BooleanField = models.BooleanField(null=True)

    test_account_filters: models.JSONField = models.JSONField(default=list)
    test_account_filters_default_checked: models.BooleanField = models.BooleanField(null=True, blank=True)

    path_cleaning_filters: models.JSONField = models.JSONField(default=list, null=True, blank=True)
    timezone: models.CharField = models.CharField(max_length=240, choices=TIMEZONES, default="UTC")
    data_attributes: models.JSONField = models.JSONField(default=get_default_data_attributes)
    person_display_name_properties: ArrayField = ArrayField(models.CharField(max_length=400), null=True, blank=True)
    live_events_columns: ArrayField = ArrayField(models.TextField(), null=True, blank=True)
    recording_domains: ArrayField = ArrayField(models.CharField(max_length=200, null=True), blank=True, null=True)

    primary_dashboard: models.ForeignKey = models.ForeignKey(
        "posthog.Dashboard",
        on_delete=models.SET_NULL,
        null=True,
        related_name="primary_dashboard_teams",
        blank=True,
    )  # Dashboard shown on project homepage

    # Generic field for storing any team-specific context that is more temporary in nature and thus
    # likely doesn't deserve a dedicated column. Can be used for things like settings and overrides
    # during feature releases.
    extra_settings: models.JSONField = models.JSONField(null=True, blank=True)

    # Project level default HogQL query modifiers
    modifiers: models.JSONField = models.JSONField(null=True, blank=True)

    # This is meant to be used as a stopgap until https://github.com/PostHog/meta/pull/39 gets implemented
    # Switches _most_ queries to using distinct_id as aggregator instead of person_id
    @property
    def aggregate_users_by_distinct_id(self) -> bool:
        return str(self.pk) in get_list(get_instance_setting("AGGREGATE_BY_DISTINCT_IDS_TEAMS"))

    # This correlation_config is intended to be used initially for
    # `excluded_person_property_names` but will be used as a general config
    # repository for correlation related settings.
    # NOTE: we're not doing any schema checking here, just storing whatever is
    # thrown at us. Correlation code can handle schema related issues.
    correlation_config = models.JSONField(default=dict, null=True, blank=True)

    # DEPRECATED, DISUSED: recordings on CH are cleared with Clickhouse's TTL
    session_recording_retention_period_days: models.IntegerField = models.IntegerField(
        null=True, default=None, blank=True
    )
    # DEPRECATED, DISUSED: plugins are enabled for everyone now
    plugins_opt_in: models.BooleanField = models.BooleanField(default=False)
    # DEPRECATED, DISUSED: replaced with env variable OPT_OUT_CAPTURE and User.anonymized_data
    opt_out_capture: models.BooleanField = models.BooleanField(default=False)
    # DEPRECATED: in favor of `EventDefinition` model
    event_names: models.JSONField = models.JSONField(default=list, blank=True)
    event_names_with_usage: models.JSONField = models.JSONField(default=list, blank=True)
    # DEPRECATED: in favor of `PropertyDefinition` model
    event_properties: models.JSONField = models.JSONField(default=list, blank=True)
    event_properties_with_usage: models.JSONField = models.JSONField(default=list, blank=True)
    event_properties_numerical: models.JSONField = models.JSONField(default=list, blank=True)
    external_data_workspace_id: models.CharField = models.CharField(max_length=400, null=True, blank=True)
    external_data_workspace_last_synced_at: models.DateTimeField = models.DateTimeField(null=True, blank=True)

    objects: TeamManager = TeamManager()

    @property
    def default_modifiers(self) -> dict:
        modifiers = HogQLQueryModifiers()
        set_default_modifier_values(modifiers, self)
        return modifiers.model_dump()

    @property
    def actors_skip_enrichment(self) -> bool:
        return posthoganalytics.feature_enabled(
            "actors-skip-enrichment",
            str(self.uuid),
            groups={"organization": str(self.organization_id)},
            group_properties={
                "organization": {
                    "id": str(self.organization_id),
                    "created_at": self.organization.created_at,
                }
            },
            only_evaluate_locally=True,
            send_feature_flag_events=False,
        )

    @property
    def person_on_events_mode(self) -> PersonsOnEventsMode:
        if self._person_on_events_person_id_override_properties_on_events:
            tag_queries(person_on_events_mode=PersonsOnEventsMode.PERSON_ID_OVERRIDE_PROPERTIES_ON_EVENTS)
            return PersonsOnEventsMode.PERSON_ID_OVERRIDE_PROPERTIES_ON_EVENTS

        if self._person_on_events_person_id_no_override_properties_on_events:
            # also tag person_on_events_enabled for legacy compatibility
            tag_queries(
                person_on_events_enabled=True,
                person_on_events_mode=PersonsOnEventsMode.PERSON_ID_NO_OVERRIDE_PROPERTIES_ON_EVENTS,
            )
            return PersonsOnEventsMode.PERSON_ID_NO_OVERRIDE_PROPERTIES_ON_EVENTS

        if self._person_on_events_person_id_override_properties_joined:
            tag_queries(
                person_on_events_enabled=True,
                person_on_events_mode=PersonsOnEventsMode.PERSON_ID_OVERRIDE_PROPERTIES_JOINED,
            )
            return PersonsOnEventsMode.PERSON_ID_OVERRIDE_PROPERTIES_JOINED

        return PersonsOnEventsMode.DISABLED

    # KLUDGE: DO NOT REFERENCE IN THE BACKEND!
    # Keeping this property for now only to be used by the frontend in certain cases
    @property
    def person_on_events_querying_enabled(self) -> bool:
        return self.person_on_events_mode != PersonsOnEventsMode.DISABLED

    @property
    def _person_on_events_person_id_no_override_properties_on_events(self) -> bool:
        if settings.PERSON_ON_EVENTS_OVERRIDE is not None:
            return settings.PERSON_ON_EVENTS_OVERRIDE

        # on PostHog Cloud, use the feature flag
        if is_cloud():
            return posthoganalytics.feature_enabled(
                "persons-on-events-person-id-no-override-properties-on-events",
                str(self.uuid),
                groups={"project": str(self.id)},
                group_properties={"project": {"id": str(self.id), "created_at": self.created_at, "uuid": self.uuid}},
                only_evaluate_locally=True,
                send_feature_flag_events=False,
            )

        # on self-hosted, use the instance setting
        return get_instance_setting("PERSON_ON_EVENTS_ENABLED")

    @property
    def _person_on_events_person_id_override_properties_on_events(self) -> bool:
        if settings.PERSON_ON_EVENTS_V2_OVERRIDE is not None:
            return settings.PERSON_ON_EVENTS_V2_OVERRIDE

        # on PostHog Cloud, use the feature flag
        if is_cloud():
            return posthoganalytics.feature_enabled(
                "persons-on-events-v2-reads-enabled",
                str(self.uuid),
                groups={"organization": str(self.organization_id)},
                group_properties={
                    "organization": {
                        "id": str(self.organization_id),
                        "created_at": self.organization.created_at,
                    }
                },
                only_evaluate_locally=True,
                send_feature_flag_events=False,
            )

        return get_instance_setting("PERSON_ON_EVENTS_V2_ENABLED")

    @property
    def _person_on_events_person_id_override_properties_joined(self) -> bool:
        # on PostHog Cloud, use the feature flag
        if is_cloud():
            return posthoganalytics.feature_enabled(
                "persons-on-events-person-id-override-properties-joined",
                str(self.uuid),
                groups={"organization": str(self.organization_id)},
                group_properties={
                    "organization": {
                        "id": str(self.organization_id),
                        "created_at": self.organization.created_at,
                    }
                },
                only_evaluate_locally=True,
                send_feature_flag_events=False,
            )

        return False

    @property
    def strict_caching_enabled(self) -> bool:
        enabled_teams = get_list(get_instance_setting("STRICT_CACHING_TEAMS"))
        return str(self.pk) in enabled_teams or "all" in enabled_teams

    @cached_property
    def persons_seen_so_far(self) -> int:
        from posthog.client import sync_execute
        from posthog.queries.person_query import PersonQuery

        filter = Filter(data={"full": "true"})
        person_query, person_query_params = PersonQuery(filter, self.id).get_query()

        return sync_execute(
            f"""
            SELECT count(1) FROM (
                {person_query}
            )
        """,
            {**person_query_params, **filter.hogql_context.values},
        )[0][0]

    @lru_cache(maxsize=5)
    def groups_seen_so_far(self, group_type_index: GroupTypeIndex) -> int:
        from posthog.client import sync_execute

        return sync_execute(
            f"""
            SELECT
                count(DISTINCT group_key)
            FROM groups
            WHERE team_id = %(team_id)s AND group_type_index = %(group_type_index)s
        """,
            {"team_id": self.pk, "group_type_index": group_type_index},
        )[0][0]

    @property
    def timezone_info(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def path_cleaning_filter_models(self) -> list[PathCleaningFilter]:
        filters = []
        for f in self.path_cleaning_filters:
            try:
                filters.append(PathCleaningFilter.model_validate(f))
            except pydantic.ValidationError:
                continue
        return filters

    def all_users_with_access(self) -> QuerySet["User"]:
        from ee.models.explicit_team_membership import ExplicitTeamMembership
        from posthog.models.organization import OrganizationMembership
        from posthog.models.user import User

        if not self.access_control:
            user_ids_queryset = OrganizationMembership.objects.filter(organization_id=self.organization_id).values_list(
                "user_id", flat=True
            )
        else:
            user_ids_queryset = (
                OrganizationMembership.objects.filter(
                    organization_id=self.organization_id, level__gte=OrganizationMembership.Level.ADMIN
                )
                .values_list("user_id", flat=True)
                .union(
                    ExplicitTeamMembership.objects.filter(team_id=self.id).values_list(
                        "parent_membership__user_id", flat=True
                    )
                )
            )
        return User.objects.filter(is_active=True, id__in=user_ids_queryset)

    def __str__(self):
        if self.name:
            return self.name
        if self.app_urls and self.app_urls[0]:
            return ", ".join(self.app_urls)
        return str(self.pk)

    __repr__ = sane_repr("uuid", "name", "api_token")


@mutable_receiver(post_save, sender=Team)
def put_team_in_cache_on_save(sender, instance: Team, **kwargs):
    set_team_in_cache(instance.api_token, instance)


@mutable_receiver(post_delete, sender=Team)
def delete_team_in_cache_on_delete(sender, instance: Team, **kwargs):
    set_team_in_cache(instance.api_token, None)


def check_is_feature_available_for_team(team_id: int, feature_key: str, current_usage: Optional[int] = None):
    available_product_features: Optional[list[dict[str, str]]] = (
        Team.objects.select_related("organization")
        .values_list("organization__available_product_features", flat=True)
        .get(id=team_id)
    )
    if available_product_features is None:
        return False

    for feature in available_product_features:
        if feature.get("key") == feature_key:
            if current_usage is not None and feature.get("limit") is not None:
                return current_usage < int(feature["limit"])
            return True
    return False
