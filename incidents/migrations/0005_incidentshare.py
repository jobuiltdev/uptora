import django.db.models.deletion
import incidents.models
import uuid
from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [
        ('incidents', '0004_alter_incident_failure_type'),
        ('monitors', '0007_monitorrun_recovery_enqueued_at'),
    ]

    operations = [
        migrations.CreateModel(
            name='IncidentShare',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('token', models.CharField(default=incidents.models.incident_share_token, editable=False, max_length=64, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('include_evidence', models.BooleanField(default=True)),
                ('snapshot', models.JSONField(default=dict)),
                ('snapshot_updated_at', models.DateTimeField(default=timezone.now)),
                ('evidence_result', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='incident_shares', to='monitors.checkresult')),
                ('incident', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='shares', to='incidents.incident')),
            ],
            options={
                'ordering': ('-created_at',),
                'constraints': [models.UniqueConstraint(condition=models.Q(('revoked_at__isnull', True)), fields=('incident',), name='unique_unrevoked_share_per_incident')],
            },
        ),
    ]
