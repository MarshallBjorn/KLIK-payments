from django.db import models


class Zone(models.TextChoices):
    PL = 'PL', 'Polska'
    EU = 'EU', 'European Union'
    UK = 'UK', 'United Kingdom'
    US = 'US', 'United States'
