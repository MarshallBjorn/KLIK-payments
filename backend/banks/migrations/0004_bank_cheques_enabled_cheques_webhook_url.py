# Generated manually — KLIK Cheques module: dodanie pól cheques do modelu Bank

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("banks", "0002_bank_c2b_enabled_bank_p2p_enabled_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="bank",
            name="cheques_enabled",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Czy bank uczestniczy w module Cheques (czeki). "
                    "Aktywowane świadomie — bank musi wystawiać endpointy "
                    "/cheques/redeemed i /cheques/released."
                ),
            ),
        ),
        migrations.AddField(
            model_name="bank",
            name="cheques_webhook_url",
            field=models.URLField(
                blank=True,
                default="",
                max_length=500,
                help_text=(
                    "URL bazowy dla webhooków modułu Cheques. "
                    "KLIK uderza w {url}/redeemed i {url}/released. "
                    'Jeśli puste — fallback do webhook_url + "/cheques".'
                ),
            ),
        ),
    ]
