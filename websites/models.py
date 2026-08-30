from django.conf import settings
from django.core.validators import URLValidator
from django.db import models

# Uptora only monitors websites over HTTP(S). Declared once here and reused by
# the serializer, because DRF drops URLValidator instances it inherits from a
# model field and would otherwise accept schemes such as ftp://.
HTTP_URL_VALIDATOR = URLValidator(
    schemes=('http', 'https'),
    message='Enter a valid http:// or https:// URL.',
)


class Website(models.Model):
    """A website a user wants Uptora to watch."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='websites',
    )
    name = models.CharField(max_length=200)
    url = models.URLField(max_length=500, validators=[HTTP_URL_VALIDATOR])
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at', '-id')

    def __str__(self):
        return f'{self.name} ({self.url})'
