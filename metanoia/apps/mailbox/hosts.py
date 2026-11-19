"""Mail server hosts may only point at the public internet.

The IMAP/SMTP host is a place the *server* opens a socket to — and a connection
test reports back whether it got through. Left unchecked, that is a way to make
the LMS probe its own network: ``localhost``, the VPC, the EC2 metadata service
at 169.254.169.254. So a host is resolved and every address it answers with
must be globally routable (the same rule as link previews,
:func:`apps.communication.unfurl.ip_is_public`).
"""

import socket

from apps.communication.unfurl import ip_is_public


class UnsafeHost(ValueError):
    """The host is empty, does not resolve, or resolves to a non-public address."""


def vet_host(host, port=None):
    """Return ``host`` (trimmed) when it resolves only to public addresses.

    Raises :class:`UnsafeHost` with a user-facing message otherwise. Every
    address the name resolves to must be public: one public plus one private
    address is exactly what a DNS-rebinding attempt looks like.
    """
    host = (host or '').strip().rstrip('.')
    if not host:
        raise UnsafeHost('Enter a server name.')
    if any(ch in host for ch in '/@: \t') and not host.startswith('['):
        raise UnsafeHost('Enter just the server name, e.g. mail.example.com.')
    lookup = host[1:-1] if host.startswith('[') and host.endswith(']') else host
    try:
        infos = socket.getaddrinfo(lookup, port or None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise UnsafeHost(f'“{host}” could not be found. Check the server name.')
    ips = {info[4][0].split('%', 1)[0] for info in infos}
    if not ips or not all(ip_is_public(ip) for ip in ips):
        raise UnsafeHost(f'“{host}” is not a public mail server. Internal, private and '
                         'loopback addresses cannot be used.')
    return host


def check_override(account, kind):
    """Refuse to connect to a mailbox's own ``kind`` host unless it is public.

    The domain's :class:`~apps.mailbox.models.MailServer` hosts are set by an
    administrator and vetted when saved. A per-mailbox override may predate
    that rule (any mail user could once set one), so it is vetted again at the
    moment of connecting, which also defeats a name re-pointed after saving.
    """
    host = getattr(account, f'{kind}_host', '') or ''
    if host:
        vet_host(host, getattr(account, f'{kind}_port', None))
