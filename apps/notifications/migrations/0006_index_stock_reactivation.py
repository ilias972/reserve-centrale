from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0005_workitem_work_one_event_projection_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="businessevent",
            index=models.Index(
                condition=models.Q(event_type="recommendation.stock_reactivated"),
                fields=["aggregate_id", "revision"],
                name="notif_stock_reactivation_idx",
            ),
        ),
    ]
