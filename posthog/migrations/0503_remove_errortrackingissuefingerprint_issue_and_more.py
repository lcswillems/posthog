# Generated by Django 4.2.15 on 2024-10-30 17:37

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "0502_team_session_recording_url_blocklist_config"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="errortrackingissuefingerprint",
            name="issue",
        ),
        migrations.RemoveField(
            model_name="errortrackingissuefingerprint",
            name="team",
        ),
        migrations.DeleteModel(
            name="ErrorTrackingGroup",
        ),
        migrations.DeleteModel(
            name="ErrorTrackingIssueFingerprint",
        ),
    ]
