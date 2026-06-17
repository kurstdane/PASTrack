from django.db import migrations


def forwards(apps, schema_editor):
    Case = apps.get_model("core", "Case")
    Case.objects.filter(status="for_review").update(status="to_examine")
    Case.objects.filter(status="under_review").update(status="in_review")


def backwards(apps, schema_editor):
    Case = apps.get_model("core", "Case")
    Case.objects.filter(status="to_examine").update(status="for_review")
    Case.objects.filter(status="in_review").update(status="under_review")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0038_customuser_date_format_preference_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
