from django.db import migrations


def forward(apps, schema_editor):
    Case = apps.get_model("cases", "Case")
    for source in apps.get_model("cases", "StockCase").objects.iterator():
        Case.objects.create(
            id=source.pk,
            stock_case_id=source.pk,
            origin_operation_id=source.operation_id,
            kind="STOCK_CONFLICT",
        )
    for source in apps.get_model("requests", "DeliveryIssue").objects.iterator():
        Case.objects.create(
            id=source.pk,
            delivery_id=source.delivery_id,
            delivery_issue_id=source.pk,
            origin_operation_id=source.operation_id,
            kind=source.kind,
        )


class Migration(migrations.Migration):
    dependencies = [("cases", "0003_m6_facts")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
