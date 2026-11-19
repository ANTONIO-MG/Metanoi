"""Clickable links and preview cards (apps.communication.links / unfurl).

Nothing here reaches the network: DNS and sockets are mocked, so the tests pin
what the fetcher *would* connect to — and that it refuses before connecting.
"""

import socket
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import links, models, unfurl
from .tests_api_security import LOCMEM_CACHE, make_user

SITE = dict(SITE_URL='https://lms.example.org', ALLOWED_HOSTS=['lms.example.org', '.metanoia.example', 'testserver'])
JOIN = 'https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0'


@override_settings(CACHES=LOCMEM_CACHE, **SITE)
class LinkifyTests(TestCase):

    """Turning message text into escaped HTML with safe, correctly targeted links."""
    def setUp(self):
        cache.clear()

    def test_everything_is_escaped_and_only_http_links_are_made(self):
        html = links.linkify('<b>hi</b> javascript:alert(1) see https://example.com/a?b=1&c=2.')
        self.assertIn('&lt;b&gt;hi&lt;/b&gt;', html)
        self.assertNotIn('href="javascript', html)
        self.assertIn('href="https://example.com/a?b=1&amp;c=2"', html)
        self.assertTrue(html.endswith('</a>.'), 'the full stop ends the sentence, not the URL')

    def test_an_attribute_breakout_in_a_url_is_escaped(self):
        html = links.linkify('https://example.com/"onmouseover="alert(1)')
        self.assertNotIn('"onmouseover', html.replace('&quot;', ''))
        self.assertNotIn('onmouseover="', html)

    def test_external_links_open_in_a_new_tab_without_a_referrer(self):
        html = links.linkify('www.example.com')
        self.assertIn('href="https://www.example.com"', html)
        self.assertIn('target="_blank"', html)
        self.assertIn('rel="noopener noreferrer nofollow ugc"', html)

    def test_lms_links_stay_in_the_same_tab(self):
        for url in ('https://lms.example.org/learning/modules/', 'https://app.metanoia.example/x/'):
            html = links.linkify(url)
            self.assertNotIn('target=', html, url)
            self.assertIn(f'href="{url}"', html)

    def test_only_the_first_link_is_marked_for_a_preview(self):
        html = links.linkify('https://a.example.com and https://b.example.com')
        self.assertEqual(html.count('data-preview-url'), 1)
        self.assertEqual(links.linkify('https://a.example.com', previews=0).count('data-preview-url'), 0)

    def test_line_breaks_and_mentions(self):
        html = links.linkify('Hi @Ada Lovelace\nhttps://x.example.com', mentions=['@Ada Lovelace'])
        self.assertIn('<span class="feed-mention">@Ada Lovelace</span><br>', html)
        self.assertNotIn('<br>', links.linkify('a\nb', linebreaks=False))

    def test_parentheses_around_a_link(self):
        html = links.linkify('(see https://en.wikipedia.org/wiki/Tax_(disambiguation))')
        self.assertIn('href="https://en.wikipedia.org/wiki/Tax_(disambiguation)"', html)
        self.assertTrue(html.endswith('</a>)'))

    def test_a_teams_link_for_a_session_goes_through_the_lms(self):
        host = make_user('host@example.com', role='staff', is_staff=True)
        room = models.MeetingRoom.objects.create(title='Tax class', host=host, teams_join_url=JOIN)
        html = links.linkify(f'Join: {JOIN}')
        path = reverse('livesessions:session-join', args=[room.pk])
        self.assertIn(f'href="{path}"', html)
        self.assertNotIn('target=', html)
        email = links.linkify(JOIN, absolute_urls=True)
        self.assertIn(f'href="https://lms.example.org{path}"', email)

    def test_an_unknown_teams_link_stays_external(self):
        html = links.linkify('https://teams.microsoft.com/l/meetup-join/other')
        self.assertIn('target="_blank"', html)

    def test_the_template_filters(self):
        from django.template import Context, Template
        out = Template('{% load links %}{{ t|linkify }}|{{ t|linkify_email }}').render(
            Context({'t': 'go https://lms.example.org/x/ <now>'}))
        self.assertIn('&lt;now&gt;', out)
        self.assertEqual(out.count('<a '), 2)


class AddressVettingTests(SimpleTestCase):

    """The fetcher's address checks: only public IPs, http(s), default ports, no credentials."""
    def test_non_public_addresses_are_refused(self):
        for ip in ('127.0.0.1', '10.1.2.3', '172.16.0.1', '192.168.1.1', '169.254.169.254',
                   '100.64.0.1', '0.0.0.0', '224.0.0.1', '255.255.255.255', '::1', 'fe80::1',
                   'fc00::1', '::ffff:127.0.0.1', '::ffff:169.254.169.254', '64:ff9b::a9fe:a9fe',
                   '2002:7f00:1::1', 'ff02::1', '::'):
            self.assertFalse(unfurl.ip_is_public(ip), ip)
        for ip in ('93.184.216.34', '1.1.1.1', '2606:4700:4700::1111'):
            self.assertTrue(unfurl.ip_is_public(ip), ip)

    def _resolve(self, *ips):
        """Return a fake ``getaddrinfo`` answer for ``ips`` on port 443."""
        return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 (ip, 443)) for ip in ips]

    def test_vet_refuses_bad_schemes_ports_and_credentials(self):
        for url in ('ftp://example.com/', 'file:///etc/passwd', 'https://example.com:8080/',
                    'http://user:pw@example.com/', 'gopher://x/'):
            with self.assertRaises(unfurl.UnsafeURL, msg=url):
                unfurl.vet(url)

    def test_a_name_resolving_to_any_private_address_is_refused(self):
        with mock.patch('socket.getaddrinfo', return_value=self._resolve('93.184.216.34', '10.0.0.5')):
            with self.assertRaises(unfurl.UnsafeURL):
                unfurl.vet('https://rebind.example.com/')
        with mock.patch('socket.getaddrinfo', return_value=self._resolve('169.254.169.254')):
            with self.assertRaises(unfurl.UnsafeURL):
                unfurl.vet('http://metadata.example.com/latest/meta-data/')

    def test_the_socket_goes_to_the_vetted_address_not_a_fresh_lookup(self):
        """Resolve once, connect to that IP — a second DNS answer cannot steer it."""
        answers = iter([self._resolve('93.184.216.34'), self._resolve('127.0.0.1')])
        connected = []

        def fake_connect(addr, timeout=None, *a, **kw):
            connected.append(addr)
            raise OSError('stop here')

        with mock.patch('socket.getaddrinfo', side_effect=lambda *a, **k: next(answers)), \
                mock.patch('socket.create_connection', side_effect=fake_connect):
            final, html = unfurl.fetch_html('http://example.com/page')
        self.assertEqual(html, '')
        self.assertEqual(connected, [('93.184.216.34', 80)])


class _Resp:
    """Minimal stand-in for an :class:`http.client.HTTPResponse`."""
    def __init__(self, status=200, headers=None, body=b''):
        self.status, self._headers, self._body = status, headers or {}, body

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, n):
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk


class _Conn:
    """Minimal stand-in for an HTTP connection holding ``response``."""
    def __init__(self, response):
        self.response = response

    def close(self):
        pass


@override_settings(CACHES=LOCMEM_CACHE)
class FetchTests(SimpleTestCase):

    """Fetching a preview: redirects, content-type and size caps, Open Graph parsing and caching."""
    def setUp(self):
        cache.clear()

    def test_redirects_are_revetted_and_a_redirect_inside_is_refused(self):
        hops = []

        def fake_open(url, deadline):
            """Redirect the first hop inward, and run the real :func:`unfurl.vet` on the second."""
            hops.append(url)
            if url.startswith('https://example.com'):
                return _Conn(None), _Resp(302, {'Location': 'http://127.0.0.1/admin'})
            unfurl.vet(url)
            raise AssertionError('should have been refused')

        with mock.patch.object(unfurl, '_open', side_effect=fake_open):
            self.assertEqual(unfurl.fetch_html('https://example.com/x')[1], '')
        self.assertEqual(hops, ['https://example.com/x', 'http://127.0.0.1/admin'])

    def test_too_many_redirects_give_up(self):
        with mock.patch.object(unfurl, '_open',
                               return_value=(_Conn(None), _Resp(302, {'Location': '/again'}))) as op:
            self.assertEqual(unfurl.fetch_html('https://example.com/x')[1], '')
        self.assertEqual(op.call_count, unfurl.MAX_REDIRECTS + 1)

    def test_only_html_is_read_and_never_more_than_the_cap(self):
        with mock.patch.object(unfurl, '_open', return_value=(
                _Conn(None), _Resp(200, {'Content-Type': 'application/pdf'}, b'%PDF'))):
            self.assertEqual(unfurl.fetch_html('https://example.com/f.pdf')[1], '')
        big = b'<html><head><title>T</title></head>' + b'x' * (unfurl.MAX_BYTES * 2)
        with mock.patch.object(unfurl, '_open', return_value=(
                _Conn(None), _Resp(200, {'Content-Type': 'text/html; charset=utf-8'}, big))):
            html = unfurl.fetch_html('https://example.com/big')[1]
        self.assertLessEqual(len(html.encode()), unfurl.MAX_BYTES)

    def test_opengraph_is_parsed_cleaned_and_cached(self):
        page = (b'<html><head><title>Fallback</title>'
                b'<meta property="og:title" content="Deferred tax &amp; you">'
                b'<meta property="og:description" content="  A guide\n to IAS 12 ">'
                b'<meta property="og:image" content="/img/card.png">'
                b'<meta property="og:site_name" content="Tax Weekly"></head><body>x</body></html>')
        with mock.patch.object(unfurl, '_open', return_value=(
                _Conn(None), _Resp(200, {'Content-Type': 'text/html'}, page))) as op:
            card = unfurl.fetch_opengraph('https://news.example.com/a')
            again = unfurl.fetch_opengraph('https://news.example.com/a')
        self.assertEqual(op.call_count, 1, 'the second ask comes from the cache')
        self.assertEqual(card, again)
        self.assertEqual(card['title'], 'Deferred tax & you')
        self.assertEqual(card['description'], 'A guide to IAS 12')
        self.assertEqual(card['image'], 'https://news.example.com/img/card.png')
        self.assertEqual(card['site'], 'Tax Weekly')

    def test_an_insecure_image_is_dropped(self):
        card = unfurl.parse_opengraph('<meta property="og:title" content="T">'
                                      '<meta property="og:image" content="http://x.example/i.png">',
                                      'http://x.example/')
        self.assertEqual(card['image'], '')


@override_settings(CACHES=LOCMEM_CACHE, MS_TEAMS_ENABLED=False, **SITE)
class PreviewPermissionTests(TestCase):

    """Preview cards for in-app links only reveal what the viewer may see."""
    def setUp(self):
        cache.clear()
        self.host = make_user('host@example.com', role='staff', is_staff=True)
        self.student = make_user('student@example.com')
        self.outsider = make_user('out@example.com')
        self.room = models.MeetingRoom.objects.create(
            title='Secret strategy session', host=self.host, teams_join_url=JOIN,
            scheduled_start=timezone.now(), audience=models.MeetingRoom.AUDIENCE_PRIVATE)
        self.room.participants.create(user=self.student)

    def test_an_invited_viewer_gets_the_session_card(self):
        card = unfurl.preview_for(JOIN, self.student)
        self.assertEqual(card['title'], 'Secret strategy session')
        self.assertEqual(card['href'], reverse('livesessions:session-join', args=[self.room.pk]))
        url = 'https://lms.example.org' + reverse('livesessions:session-detail', args=[self.room.pk])
        self.assertEqual(unfurl.preview_for(url, self.student)['title'], 'Secret strategy session')

    def test_someone_who_may_not_see_it_gets_no_title(self):
        self.assertIsNone(unfurl.preview_for(JOIN, self.outsider))
        url = 'https://lms.example.org' + reverse('livesessions:session-detail', args=[self.room.pk])
        self.assertIsNone(unfurl.preview_for(url, self.outsider))

    def test_an_unknown_teams_link_gets_the_generic_card(self):
        card = unfurl.preview_for('https://teams.microsoft.com/l/meetup-join/zzz', self.outsider)
        self.assertEqual(card['title'], 'Microsoft Teams meeting')

    def test_the_endpoint_needs_a_login_and_answers_json(self):
        url = reverse('communication:link-preview')
        self.assertNotEqual(self.client.get(url, {'url': JOIN}).status_code, 200)
        self.client.force_login(self.outsider)
        self.assertIsNone(self.client.get(url, {'url': JOIN}).json()['card'])
        self.client.force_login(self.student)
        body = self.client.get(url, {'url': JOIN}).json()
        self.assertEqual(body['card']['title'], 'Secret strategy session')

    def test_an_external_link_is_not_fetched_while_a_page_renders(self):
        with mock.patch.object(unfurl, 'fetch_html') as fetch:
            links.linkify('https://example.com/a and more')
        fetch.assert_not_called()

    def test_chat_messages_carry_linkified_html(self):
        group = models.ChatGroup.objects.create(kind=models.ChatGroup.KIND_CUSTOM, name='G')
        group.add_member(self.student)
        models.Message.objects.create(group=group, sender=self.student,
                                      body='<script>x</script> https://example.com')
        self.client.force_login(self.student)
        data = self.client.get(f'/api/communication/messages/?group={group.pk}').json()
        item = (data['results'] if isinstance(data, dict) else data)[0]
        self.assertIn('&lt;script&gt;', item['body_html'])
        self.assertIn('<a href="https://example.com"', item['body_html'])
