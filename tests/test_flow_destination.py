"""The same-origin submission policy.

Pure functions, so the boundary can be pinned down exhaustively without a
browser. The cases that matter are the ones a looser rule would wave through.
"""

import pytest

from monitors.checks.flows.destination import (
    build_submission_policy,
    parse_origin,
    same_origin,
    without_www,
)


class TestParseOrigin:
    def test_the_scheme_default_port_is_filled_in(self):
        assert parse_origin('https://example.com/contact').port == 443
        assert parse_origin('http://example.com/contact').port == 80

    def test_an_explicit_port_is_kept(self):
        assert parse_origin('https://example.com:8443/x').port == 8443

    def test_the_host_is_lowercased_and_the_root_dot_dropped(self):
        assert parse_origin('https://EXAMPLE.com./x').host == 'example.com'

    @pytest.mark.parametrize(
        'url',
        ['ftp://example.com/x', 'file:///etc/passwd', 'https:///nohost', 'not a url'],
    )
    def test_anything_without_a_usable_origin_is_none(self, url):
        assert parse_origin(url) is None


class TestWithoutWww:
    def test_one_leading_label_is_removed(self):
        assert without_www('www.example.com') == 'example.com'

    def test_a_host_without_the_prefix_is_untouched(self):
        assert without_www('example.com') == 'example.com'

    def test_only_the_leading_label_is_removed(self):
        assert without_www('a.www.example.com') == 'a.www.example.com'

    def test_collapsing_cannot_widen_a_host(self):
        """'www.com' collapses to 'com', which still only matches itself."""
        assert not same_origin('https://evil.com/x', 'https://www.com/x')
        assert same_origin('https://com/x', 'https://www.com/x')


class TestSameOrigin:
    @pytest.mark.parametrize(
        ('destination', 'monitored'),
        [
            # Exact origin.
            ('https://example.com/contact', 'https://example.com/'),
            ('https://www.example.com/api/contact', 'https://www.example.com/'),
            ('http://example.com/contact', 'http://example.com/'),
            # The explicit www <-> apex exception, both directions.
            ('https://example.com/contact', 'https://www.example.com/'),
            ('https://www.example.com/contact', 'https://example.com/'),
            # Default ports are equivalent to writing them out.
            ('https://example.com:443/contact', 'https://example.com/'),
            ('http://example.com:80/contact', 'http://example.com/'),
            # Matching non-default ports.
            ('http://example.com:8080/contact', 'http://example.com:8080/'),
            # Case and trailing root dot are URL grammar, not policy.
            ('https://EXAMPLE.COM/contact', 'https://example.com/'),
        ],
    )
    def test_allowed(self, destination, monitored):
        assert same_origin(destination, monitored)

    @pytest.mark.parametrize(
        ('destination', 'monitored'),
        [
            # Sibling and child subdomains are separate origins.
            ('https://forms.example.com/x', 'https://example.com/'),
            ('https://example.com/x', 'https://shop.example.com/'),
            ('https://forms.example.com/x', 'https://shop.example.com/'),
            ('https://a.www.example.com/x', 'https://www.example.com/'),
            # Unrelated hosts.
            ('https://unrelated.com/x', 'https://example.com/'),
            ('https://example.net/x', 'https://example.com/'),
            # The lookalikes a suffix comparison would let through.
            ('https://evilexample.com/x', 'https://example.com/'),
            ('https://example.com.evil.net/x', 'https://example.com/'),
            ('https://example.como/x', 'https://example.com/'),
            # Scheme must match: a downgrade would send the message in clear.
            ('http://example.com/x', 'https://example.com/'),
            ('https://example.com/x', 'http://example.com/'),
            # Effective port must match.
            ('https://example.com:8443/x', 'https://example.com/'),
            ('http://example.com:8080/x', 'http://example.com:9090/'),
            ('http://example.com/x', 'http://example.com:8080/'),
            # Unusable origins.
            ('ftp://example.com/x', 'https://example.com/'),
            ('', 'https://example.com/'),
            ('https://example.com/x', ''),
        ],
    )
    def test_refused(self, destination, monitored):
        assert not same_origin(destination, monitored)


class TestPolicy:
    def test_the_monitored_origin_is_allowed(self):
        policy = build_submission_policy('https://www.example.com/contact')

        allowed, reason = policy('https://www.example.com/submit')

        assert allowed
        assert reason is None

    def test_the_apex_is_allowed_from_www(self):
        policy = build_submission_policy('https://www.example.com/contact')

        allowed, _ = policy('https://example.com/submit')

        assert allowed

    def test_a_sibling_subdomain_is_refused_with_both_origins_named(self):
        policy = build_submission_policy('https://example.com/contact')

        allowed, reason = policy('https://forms.example.com/submit')

        assert not allowed
        assert 'forms.example.com' in reason
        assert 'example.com' in reason

    def test_an_unrelated_destination_is_refused(self):
        policy = build_submission_policy('https://example.com/contact')

        allowed, reason = policy('https://victim.example/spam')

        assert not allowed
        assert 'victim.example' in reason

    def test_a_scheme_downgrade_is_refused(self):
        policy = build_submission_policy('https://example.com/contact')

        allowed, _ = policy('http://example.com/submit')

        assert not allowed

    def test_a_different_port_is_refused(self):
        policy = build_submission_policy('http://example.com:8080/contact')

        allowed, _ = policy('http://example.com:9000/submit')

        assert not allowed

    def test_a_non_default_port_matching_exactly_is_allowed(self):
        policy = build_submission_policy('http://example.com:8080/contact')

        allowed, _ = policy('http://example.com:8080/submit')

        assert allowed
