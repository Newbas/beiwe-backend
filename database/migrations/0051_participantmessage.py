# Generated by Django 2.2.19 on 2021-05-28 06:55

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('database', '0050_participant_unregistered'),
    ]

    operations = [
        migrations.CreateModel(
            name='ParticipantMessage',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_on', models.DateTimeField(auto_now_add=True)),
                ('last_updated', models.DateTimeField(auto_now=True)),
                ('message', models.TextField()),
                ('schedule_type', models.TextField(choices=[('asap', 'as soon as possible'), ('absolute', 'at a specific date/time')])),
                ('uuid', models.UUIDField(default=uuid.uuid4)),
                ('scheduled_send_datetime', models.DateTimeField(blank=True, null=True)),
                ('datetime_sent', models.DateTimeField(blank=True, null=True)),
                ('status', models.TextField(choices=[('cancelled', 'Cancelled'), ('error', 'Error'), ('scheduled', 'Scheduled'), ('sent', 'Sent')], default='scheduled')),
                ('participant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='participant_messages', to='database.Participant')),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
