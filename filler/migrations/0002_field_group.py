from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('filler', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='field',
            name='group',
            field=models.TextField(blank=True),
        ),
    ]
