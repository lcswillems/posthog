# Generated by Django 4.2.11 on 2024-05-15 21:02

from django.db import migrations, models
import posthog.models.referrals.referral_program_referrer


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "0412_referralprogram_referralprogramreferrer_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="referralprogramredeemer",
            name="email",
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AddField(
            model_name="referralprogramreferrer",
            name="email",
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AlterField(
            model_name="referralprogramreferrer",
            name="code",
            field=models.TextField(
                default=posthog.models.referrals.referral_program_referrer.generate_referral_code, max_length=128
            ),
        ),
    ]
