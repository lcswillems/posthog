# Generated by Django 3.2.12 on 2022-05-03 06:09

import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "0231_add_refreshing_data_to_tiles"),
    ]

    operations = [
        migrations.AddField(
            model_name="team",
            name="person_display_name_properties",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=400),
                blank=True,
                null=True,
                size=None,
            ),
        ),
    ]
