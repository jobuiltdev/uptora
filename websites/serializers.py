import re
from urllib.parse import urlsplit, urlunsplit

from rest_framework import serializers

from websites.models import HTTP_URL_VALIDATOR, Website

# A scheme is only present when it is followed by '://'. Matching on a bare
# colon would misread 'example.com:8080' as the scheme 'example.com'.
SCHEME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.\-]*://')


def normalize_url(value):
    """Tidy a user-supplied URL without dropping anything meaningful.

    A missing scheme is filled in as https. Scheme and host are lowercased. A
    bare trailing slash is removed, but real paths, queries and fragments are
    left exactly as given.
    """
    url = value.strip()
    if not url:
        return url

    if not SCHEME_RE.match(url):
        url = f'https://{url}'

    scheme, netloc, path, query, fragment = urlsplit(url)
    if path == '/' and not query and not fragment:
        path = ''
    return urlunsplit((scheme.lower(), netloc.lower(), path, query, fragment))


class WebsiteSerializer(serializers.ModelSerializer):
    url = serializers.URLField(max_length=500, validators=[HTTP_URL_VALIDATOR])

    class Meta:
        model = Website
        fields = ('id', 'owner', 'name', 'url', 'is_active', 'created_at', 'updated_at')
        # The owner is taken from the request, never from the payload.
        read_only_fields = ('id', 'owner', 'created_at', 'updated_at')

    def to_internal_value(self, data):
        # Normalize before super(), because the URL validators run inside it and
        # a scheme-less value would be rejected before we could fix it up.
        if isinstance(data, dict) and isinstance(data.get('url'), str):
            data = data.copy()
            data['url'] = normalize_url(data['url'])
        return super().to_internal_value(data)
