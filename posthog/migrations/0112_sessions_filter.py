# Generated by Django 3.0.6 on 2021-01-08 20:56

import django.contrib.postgres.fields.jsonb
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "0111_plugin_storage"),
    ]

    operations = [
        migrations.CreateModel(
            name="SessionsFilter",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(blank=True, max_length=400, null=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "filters",
                    django.contrib.postgres.fields.jsonb.JSONField(default=dict),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="posthog.Team"),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="sessionsfilter",
            index=models.Index(fields=["team_id", "name"], name="posthog_ses_team_id_453d24_idx"),
        ),
    ]
