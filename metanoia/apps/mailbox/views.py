"""Metanoia Mail pages.

One page (:func:`mailbox`) draws Outlook's three panes — folders, message
list, reading pane — and static/js/mailbox.js swaps the list and the reading
pane in place through the fragment views below, so moving between messages
never reloads the page. Every view is limited to the viewer's own mailboxes.

``PAGE_SIZE`` is the number of messages per list page and ``MAX_UPLOAD`` the
largest single file a draft accepts.

Attachments are served with strict Content Security Policies:

``INLINE_CSP``
    The inline (preview) response for everything but PDFs. ``sandbox``
    without ``allow-scripts`` means that even opened in its own tab the file
    is an opaque-origin document that can run nothing; the source lists only
    let an image, a media stream or a browser's own plain-text styling load.
``PDF_CSP``
    PDFs get no ``sandbox``: it also switches off the browser's built-in PDF
    viewer (see ``core/media_headers.py``), and a PDF cannot reach our DOM or
    cookies anyway. ``frame-ancestors`` keeps it framable by our pages only.
``DOWNLOAD_CSP``
    Everything served as a download.
"""

import functools
import logging
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.http import content_disposition_header
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from . import actions, attachables, imap, parse, send, signatures, sync
from .access import can_manage_mail, can_use_mail
from .forms import AssignMailboxForm, MailAccountForm, MailServerForm
from .models import MailAccount, MailDraft, MailDraftAttachment, MailFolder, MailItem, MailServer, preview_type

logger = logging.getLogger('apps')

PAGE_SIZE = 50
MAX_UPLOAD = 25 * 1024 * 1024


def mail_required(view):
    """Decorator: signed-in staff, administrators and educators only.

    Others get a JSON 403 for an AJAX request, or a message and a redirect to
    the dashboard.
    """
    @functools.wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        """Run ``view`` when the user may use Metanoia Mail."""
        if not can_use_mail(request.user):
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'ok': False, 'error': 'Metanoia Mail is for staff and educators.'}, status=403)
            messages.error(request, 'Metanoia Mail is available to admin, staff and educator accounts.')
            return redirect('pages:dashboard')
        return view(request, *args, **kwargs)
    return wrapper


def _accounts(user):
    """The viewer's active mailboxes, with their folders."""
    return list(MailAccount.objects.filter(user=user, is_active=True).prefetch_related('folders'))


def _item(request, pk):
    """The viewer's message ``pk``, or 404."""
    return get_object_or_404(MailItem.objects.select_related('folder', 'account'), pk=pk,
                             account__user=request.user)


def _draft(request, pk):
    """The viewer's draft ``pk``, or 404."""
    return get_object_or_404(MailDraft.objects.select_related('account', 'source'), pk=pk,
                             account__user=request.user)


def _json_error(message, status=400):
    """A ``{ok: false, error}`` JSON response."""
    return JsonResponse({'ok': False, 'error': message}, status=status)


def _tree(accounts):
    """Per account: the well-known folders in Outlook's order, then the rest."""
    tree = []
    for account in accounts:
        folders = sorted(account.folders.all(), key=lambda f: f.sort_key)
        drafts = next((f for f in folders if f.role == MailFolder.ROLE_DRAFTS), None)
        local_drafts = account.drafts.count()
        tree.append({'account': account, 'folders': folders,
                     'drafts_key': str(drafts.pk) if drafts else f'd{account.pk}',
                     'has_drafts_folder': drafts is not None, 'local_drafts': local_drafts})
    return tree


def _resolve_folder(request, accounts, key):
    """``(account, folder_or_None, is_drafts_view)`` for the ``folder`` query value."""
    key = (key or '').strip()
    if key.startswith('d') and key[1:].isdigit():
        account = next((a for a in accounts if a.pk == int(key[1:])), None)
        if account:
            return account, None, True
    if key.isdigit():
        for account in accounts:
            for folder in account.folders.all():
                if folder.pk == int(key):
                    return account, folder, folder.role == MailFolder.ROLE_DRAFTS
    account = next((a for a in accounts if a.is_default), accounts[0])
    return account, account.inbox, False


def _group_label(when, now):
    """Outlook's list group heading (Today, Yesterday, This week…) for a message date."""
    day = timezone.localtime(when).date()
    today = timezone.localtime(now).date()
    if day == today:
        return 'Today'
    if day == today - timedelta(days=1):
        return 'Yesterday'
    week_start = today - timedelta(days=today.weekday())
    if day >= week_start:
        return 'This week'
    if day >= week_start - timedelta(days=7):
        return 'Last week'
    if day.year == today.year and day.month == today.month:
        return 'Earlier this month'
    if day.year == today.year:
        return 'Earlier this year'
    return 'Older'


def _list_context(request, accounts):
    """Context for the message list: folder, search, filter and paging from the query string.

    A search with ``scope=all`` covers every folder except Deleted Items and
    Junk. The first page of the Drafts view also lists the local drafts.
    """
    account, folder, drafts_view = _resolve_folder(request, accounts, request.GET.get('folder'))
    q = (request.GET.get('q') or '').strip()
    flt = request.GET.get('filter') or 'all'
    scope = request.GET.get('scope') or 'folder'
    try:
        offset = max(int(request.GET.get('offset') or 0), 0)
    except ValueError:
        offset = 0

    items = MailItem.objects.filter(account__in=accounts).select_related('folder')
    if q and scope == 'all':
        items = items.exclude(folder__role__in=[MailFolder.ROLE_TRASH, MailFolder.ROLE_JUNK])
    elif folder is not None:
        items = items.filter(folder=folder)
    else:
        items = items.none()
    if q:
        items = items.filter(Q(subject__icontains=q) | Q(from_name__icontains=q) | Q(from_address__icontains=q)
                             | Q(snippet__icontains=q) | Q(body_text__icontains=q))
    if flt == 'unread':
        items = items.filter(is_read=False)
    elif flt == 'flagged':
        items = items.filter(is_flagged=True)
    elif flt == 'attachments':
        items = items.filter(has_attachments=True)
    items = items.defer('body_text', 'body_html', 'references')

    page = list(items[offset:offset + PAGE_SIZE + 1])
    more = len(page) > PAGE_SIZE
    page = page[:PAGE_SIZE]
    now = timezone.now()
    last = None
    rows = []
    for item in page:
        label = _group_label(item.date, now)
        rows.append({'item': item, 'group': label if label != last else ''})
        last = label

    local_drafts = []
    if drafts_view and offset == 0 and not q:
        local_drafts = list(account.drafts.select_related('account')[:100])

    title = 'Search results' if q else ('Drafts' if drafts_view else (folder.label if folder else 'Inbox'))
    return {
        'account': account, 'folder': folder, 'drafts_view': drafts_view,
        'folder_key': (str(folder.pk) if folder else f'd{account.pk}'),
        'rows': rows, 'local_drafts': local_drafts, 'more': more, 'next_offset': offset + PAGE_SIZE,
        'q': q, 'filter': flt, 'scope': scope, 'list_title': title, 'offset': offset,
        'search_all': q and scope == 'all',
    }


@mail_required
def mailbox(request):
    """The Metanoia Mail page (all three panes), or the setup page when the viewer has no mailbox.

    ``?m=<id>`` opens a message (marking it read) and ``?draft=<id>`` a draft.
    """
    accounts = _accounts(request.user)
    if not accounts:
        return render(request, 'mailbox/setup.html', {
            'page_title': 'Metanoia Mail', 'server': MailServer.get(),
            'form': MailAccountForm(owner=request.user, initial=_new_account_initial(request.user)),
            'can_manage': can_manage_mail(request.user),
        })
    ctx = _list_context(request, accounts)
    selected = draft = None
    if request.GET.get('m', '').isdigit():
        selected = MailItem.objects.filter(pk=request.GET['m'], account__user=request.user).select_related(
            'folder', 'account').first()
        if selected:
            actions.set_read_in_background(selected.account, selected)
            selected.is_read = True
    if request.GET.get('draft', '').isdigit():
        draft = MailDraft.objects.filter(pk=request.GET['draft'], account__user=request.user).first()
    ctx.update({
        'page_title': ctx['list_title'] + ' · Metanoia Mail',
        'accounts': accounts, 'tree': _tree(accounts),
        'selected': selected, 'draft': draft,
        'read_ctx': _read_context(selected) if selected else None,
        'compose_ctx': _compose_context(draft, accounts) if draft else None,
        'move_targets': _move_targets(ctx['account']),
        'errors': [a for a in accounts if a.last_error],
        'can_manage': can_manage_mail(request.user),
    })
    return render(request, 'mailbox/mailbox.html', ctx)


def _move_targets(account):
    """Folders a message in ``account`` can be moved to, in sidebar order."""
    return sorted(account.folders.all(), key=lambda f: f.sort_key)


@mail_required
def list_fragment(request):
    """JSON: the message list rows for the current query, plus paging and unread counts."""
    accounts = _accounts(request.user)
    if not accounts:
        return HttpResponse('')
    ctx = _list_context(request, accounts)
    html = render_to_string('mailbox/_list_rows.html', ctx, request=request)
    return JsonResponse({'ok': True, 'html': html, 'more': ctx['more'], 'next_offset': ctx['next_offset'],
                         'title': ctx['list_title'], 'counts': _counts(accounts)})


def _counts(accounts):
    """Unread per folder key, local drafts per account, and the inbox total (the sidebar badge)."""
    out, inbox_total = {}, 0
    for f in MailFolder.objects.filter(account__in=accounts).only('id', 'unread', 'role'):
        out[str(f.pk)] = f.unread
        if f.role == MailFolder.ROLE_INBOX:
            inbox_total += f.unread
    for account in accounts:
        out[f'd{account.pk}'] = account.drafts.count()
    out['inbox_total'] = inbox_total
    return out


def _read_context(item):
    """Context for the reading pane of ``item``."""
    attachments = [a for a in item.attachments.all() if not a.is_inline]
    return {'item': item, 'attachments': attachments,
            'body_url': reverse('mailbox:body', args=[item.pk]),
            'is_sent': item.folder.role in (MailFolder.ROLE_SENT, MailFolder.ROLE_DRAFTS),
            'in_junk': item.folder.role == MailFolder.ROLE_JUNK}


@mail_required
def read_fragment(request, pk):
    """JSON: the reading pane for message ``pk``, which is marked read."""
    item = _item(request, pk)
    actions.set_read_in_background(item.account, item)
    item.is_read = True
    html = render_to_string('mailbox/_read.html', {'read_ctx': _read_context(item)}, request=request)
    return JsonResponse({'ok': True, 'html': html, 'counts': _counts(_accounts(request.user))})


@mail_required
@xframe_options_sameorigin
def body(request, pk):
    """The message body as its own sandboxed document (the reading pane's iframe)."""
    item = _item(request, pk)
    allow_remote = request.GET.get('images') == '1'
    cid_urls = {a.content_id: reverse('mailbox:attachment', args=[item.pk, a.pk]) + '?inline=1'
                for a in item.attachments.all() if a.content_id}
    if item.body_html:
        html, blocked = parse.sanitize_html(item.body_html, cid_urls=cid_urls, allow_remote=allow_remote)
    else:
        html, blocked = parse.text_to_html(item.body_text), 0
    doc = render_to_string('mailbox/body.html', {'html': html, 'blocked': blocked})
    response = HttpResponse(doc)
    response['Content-Security-Policy'] = (
        "default-src 'none'; img-src 'self' data: " + ('https: http:' if allow_remote else '')
        + "; style-src 'unsafe-inline'; font-src data:" + (' https:' if allow_remote else '')
        + "; base-uri 'none'; form-action 'none'")
    response['X-Mail-Blocked-Images'] = str(blocked)
    response['Referrer-Policy'] = 'no-referrer'
    return response


INLINE_CSP = ("default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; "
              "sandbox; frame-ancestors 'self'")
PDF_CSP = "frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
DOWNLOAD_CSP = "sandbox; default-src 'none'"


def file_response(request, data, filename, content_type):
    """Serve attachment bytes as a download, or inline for the preview.

    ``?inline=1`` is honoured only for the types :func:`models.preview_type`
    can show safely (images, PDF, audio, video, plain text); anything else —
    Office files, HTML, SVG, unknown — downloads whatever the query says, so a
    crafted link can't make a hostile file render in our origin. With no
    query string it is always a download, which keeps old links working.

    An inline response is sent with ``X-Frame-Options: SAMEORIGIN``: the
    preview frames PDFs (and the body frame shows ``cid:`` images), and
    Django's default ``DENY`` would refuse that even from our own pages.
    """
    kind, served_type = (None, None)
    if request.GET.get('inline') == '1':
        kind, served_type = preview_type(filename, content_type)
    filename = (filename or 'attachment').replace('\r', ' ').replace('\n', ' ')
    response = HttpResponse(data, content_type=served_type or 'application/octet-stream')
    response['Content-Disposition'] = content_disposition_header(not kind, filename)
    response['X-Content-Type-Options'] = 'nosniff'
    response['Referrer-Policy'] = 'no-referrer'
    if kind == 'pdf':
        response['Content-Security-Policy'] = PDF_CSP
    elif kind:
        response['Content-Security-Policy'] = INLINE_CSP
    else:
        response['Content-Security-Policy'] = DOWNLOAD_CSP
    if kind:
        response['X-Frame-Options'] = 'SAMEORIGIN'
    return response


@mail_required
def attachment(request, pk, att):
    """Download (or, with ``?inline=1``, preview) attachment ``att`` of message ``pk``."""
    item = _item(request, pk)
    meta = get_object_or_404(item.attachments, pk=att)
    part = parse.get_part(item.read_raw(), meta.part)
    if part is None:
        raise Http404('The attachment is no longer available.')
    filename, ctype, data = part
    return file_response(request, data, meta.filename or filename, ctype or meta.content_type)


@mail_required
def source(request, pk):
    """View source: the original message headers and body as plain text."""
    item = _item(request, pk)
    response = HttpResponse(item.read_raw(), content_type='text/plain; charset=utf-8')
    response['Content-Security-Policy'] = "sandbox; default-src 'none'"
    return response


@mail_required
@require_POST
def action(request):
    """Toolbar actions on the POSTed message ``ids``: read/unread, flag, delete, archive, junk, inbox, move.

    Archiving creates an Archive folder when the mailbox has none. Returns JSON
    with fresh counts, or a 502 error when the mail server refuses.
    """
    ids = [int(i) for i in request.POST.getlist('ids') if str(i).isdigit()]
    what = request.POST.get('action')
    items = list(MailItem.objects.filter(pk__in=ids, account__user=request.user).select_related('folder', 'account'))
    if not items:
        return _json_error('Select a message first.')
    by_account = {}
    for item in items:
        by_account.setdefault(item.account, []).append(item)
    try:
        for account, group in by_account.items():
            if what in ('read', 'unread'):
                actions.set_read(account, group, what == 'read')
            elif what in ('flag', 'unflag'):
                actions.set_flagged(account, group, what == 'flag')
            elif what == 'delete':
                actions.delete(account, group)
            elif what in ('archive', 'junk', 'inbox'):
                role = {'archive': MailFolder.ROLE_ARCHIVE, 'junk': MailFolder.ROLE_JUNK,
                        'inbox': MailFolder.ROLE_INBOX}[what]
                dest = account.folder(role)
                if dest is None and what == 'archive':
                    actions.create_folder(account, 'Archive')
                    dest = account.folders.filter(name__iexact='archive').first()
                    if dest:
                        dest.role = MailFolder.ROLE_ARCHIVE
                        dest.save(update_fields=['role'])
                actions.move(account, group, dest)
            elif what == 'move':
                dest = account.folders.filter(pk=request.POST.get('folder')).first()
                actions.move(account, group, dest)
            else:
                return _json_error('Unknown action.')
    except imap.MailError as exc:
        return _json_error(str(exc), 502)
    return JsonResponse({'ok': True, 'counts': _counts(_accounts(request.user))})


@mail_required
@require_POST
def folder_create(request):
    """Create a folder in one of the viewer's mailboxes."""
    account = get_object_or_404(MailAccount, pk=request.POST.get('account'), user=request.user)
    try:
        actions.create_folder(account, request.POST.get('name', ''))
    except imap.MailError as exc:
        return _json_error(str(exc), 502)
    return JsonResponse({'ok': True})


@mail_required
@require_POST
def sync_now(request):
    """Send/Receive: sync the folder being viewed (or everything) right now."""
    accounts = _accounts(request.user)
    key = request.POST.get('folder', '')
    new, errors = 0, []
    for account in accounts:
        folder = None
        if key.isdigit():
            folder = account.folders.filter(pk=key).first()
            if folder is None:
                continue
        result = sync.sync_account(account, folder=folder) if folder else sync.sync_account(account)
        if result is None:
            continue
        new += result['new']
        if result['error']:
            errors.append(f'{account.email_address}: {result["error"]}')
    return JsonResponse({'ok': not errors, 'new': new, 'errors': errors, 'counts': _counts(_accounts(request.user))})


@login_required
def poll(request):
    """Polled from every page (chrome.js): start a background inbox check, return the unread count."""
    if not can_use_mail(request.user):
        return JsonResponse({'unread': 0})
    sync.poll(request.user)
    unread = sum(MailFolder.objects.filter(account__user=request.user, account__is_active=True,
                                           role=MailFolder.ROLE_INBOX).values_list('unread', flat=True))
    return JsonResponse({'unread': unread})


def _signature(account):
    """``account``'s signature as composer HTML (cleaned), preceded by an empty line; ``''`` if none."""
    sig = (account.signature_html or '').strip()
    if not sig:
        return ''
    if '<' not in sig:
        sig = escape(sig).replace('\n', '<br>')
    return f'<p><br></p><div class="mb-signature">{parse.clean_compose_html(sig)}</div>'


def _compose_context(draft, accounts):
    """Context for the composer.

    With several mailboxes, each one's signature is included so switching From
    swaps the one in the message.
    """
    sigs = [(a.pk, _signature(a).replace('<p><br></p>', '', 1)) for a in accounts] if len(accounts) > 1 else []
    return {'draft': draft, 'accounts': accounts, 'attachments': list(draft.attachments.all()),
            'show_cc': bool(draft.cc or draft.bcc), 'signatures': sigs}


def _default_account(request, accounts):
    """The mailbox named by the ``account`` parameter, else the default one."""
    wanted = request.GET.get('account') or request.POST.get('account')
    return next((a for a in accounts if str(a.pk) == str(wanted)), None) or \
        next((a for a in accounts if a.is_default), accounts[0])


@mail_required
def compose(request):
    """Open a new message. Query values pre-fill it; ``attach=kind:ref`` adds system documents.

    e.g. ``/mail/compose/?attach=invoice:42`` — see :mod:`apps.mailbox.attachables`.
    """
    accounts = _accounts(request.user)
    if not accounts:
        messages.info(request, 'Connect your Metanoia mailbox first, then send from it.')
        return redirect('mailbox:inbox')
    account = _default_account(request, accounts)
    draft = MailDraft(account=account, to=request.GET.get('to', ''), cc=request.GET.get('cc', ''),
                      subject=request.GET.get('subject', ''))
    body = escape(request.GET.get('body', '')).replace('\n', '<br>')
    files, labels = [], []
    for spec in request.GET.getlist('attach'):
        try:
            found = attachables.resolve(request, spec)
        except PermissionDenied:
            messages.error(request, 'You may not send that document.')
            continue
        except LookupError as exc:
            messages.error(request, str(exc))
            continue
        files.extend(found.files)
        labels.append(found.label)
        draft.context_ref = draft.context_ref or spec[:100]
        draft.to = draft.to or found.to
        draft.subject = draft.subject or found.subject
        body = body or found.body_html
    draft.context_label = ', '.join(labels)[:200]
    draft.body_html = (body or '<p><br></p>') + _signature(account)
    draft.save()
    for filename, data, ctype in files:
        _add_attachment(draft, filename, data, ctype)
    return redirect(reverse('mailbox:inbox') + f'?draft={draft.pk}')


def _add_attachment(draft, filename, data, ctype):
    """Attach ``data`` to ``draft`` as ``filename``."""
    att = MailDraftAttachment(draft=draft, filename=filename[:255], content_type=ctype or '', size=len(data))
    att.file.save(filename, ContentFile(data), save=False)
    att.save()
    return att


def _prefix(subject, prefix):
    """Outlook's ``RE:`` / ``FW:``, without stacking ``RE: RE:``."""
    s = (subject or '').strip()
    already = ('re:',) if prefix == 'RE:' else ('fw:', 'fwd:')
    return s if s.lower().startswith(already) else f'{prefix} {s}'.strip()


def _fmt_people(pairs):
    """``[[name, address], ...]`` as escaped ``Name <address>; …`` text."""
    return '; '.join(escape(f'{n} <{a}>' if n else a) for n, a in pairs)


def _quote_block(item):
    """The quoted original for a reply or forward, with an Outlook-style header."""
    if item.body_html:
        original, _ = parse.sanitize_html(item.body_html, allow_remote=True)
    else:
        original = parse.text_to_html(item.body_text)
    when = timezone.localtime(item.date).strftime('%A, %d %B %Y %H:%M')
    header = (f'<b>From:</b> {_fmt_people([[item.from_name, item.from_address]])}<br>'
              f'<b>Sent:</b> {when}<br>'
              f'<b>To:</b> {_fmt_people(item.to)}<br>'
              + (f'<b>Cc:</b> {_fmt_people(item.cc)}<br>' if item.cc else '')
              + f'<b>Subject:</b> {escape(item.subject)}')
    return ('<div class="mb-quote"><hr style="border:none;border-top:1px solid #e1e1e1">'
            f'<div style="font-size:11pt">{header}</div><br>{original}</div>')


@mail_required
@require_POST
def respond(request, pk, mode):
    """Reply, reply all or forward — a new draft, opened in the reading pane."""
    item = _item(request, pk)
    if mode not in (MailDraft.MODE_REPLY, MailDraft.MODE_REPLY_ALL, MailDraft.MODE_FORWARD):
        raise Http404
    account = item.account
    own = {a.email_address.lower() for a in _accounts(request.user)}
    draft = MailDraft(account=account, mode=mode, source=item)
    if mode == MailDraft.MODE_FORWARD:
        draft.subject = _prefix(item.subject, 'FW:')
    else:
        draft.subject = _prefix(item.subject, 'RE:')
        draft.in_reply_to = item.message_id
        draft.references = item.references
        sent_by_me = item.from_address.lower() in own
        primary = item.to if sent_by_me else (item.reply_to or [[item.from_name, item.from_address]])
        to = [p for p in primary if p[1].lower() not in own] or primary
        cc = []
        if mode == MailDraft.MODE_REPLY_ALL:
            seen = {a.lower() for _, a in to} | own
            for n, a in list(item.to) + list(item.cc):
                if a.lower() not in seen:
                    (to if sent_by_me else cc).append([n, a])
                    seen.add(a.lower())
        draft.to = '; '.join(f'{n} <{a}>' if n else a for n, a in to)
        draft.cc = '; '.join(f'{n} <{a}>' if n else a for n, a in cc)
    draft.body_html = '<p><br></p>' + _signature(account) + _quote_block(item)
    draft.save()
    if mode == MailDraft.MODE_FORWARD:
        raw = item.read_raw()
        for meta in item.attachments.filter(is_inline=False):
            part = parse.get_part(raw, meta.part)
            if part:
                _add_attachment(draft, meta.filename or part[0], part[2], meta.content_type)
    return _compose_json(request, draft)


def _compose_json(request, draft):
    """JSON: the composer for ``draft``."""
    html = render_to_string('mailbox/_compose.html',
                            {'compose_ctx': _compose_context(draft, _accounts(request.user))}, request=request)
    return JsonResponse({'ok': True, 'id': draft.pk, 'html': html})


@mail_required
def draft_pane(request, pk):
    """JSON: the composer for an existing draft."""
    return _compose_json(request, _draft(request, pk))


@mail_required
@require_POST
def draft_new(request):
    """The New mail button: a blank draft in the reading pane."""
    accounts = _accounts(request.user)
    if not accounts:
        return _json_error('Connect a mailbox first.')
    account = _default_account(request, accounts)
    draft = MailDraft.objects.create(account=account, body_html='<p><br></p>' + _signature(account))
    return _compose_json(request, draft)


def _apply_draft_fields(request, draft):
    """Copy the POSTed recipients, subject, body and From mailbox onto ``draft`` and save it."""
    for field in ('to', 'cc', 'bcc', 'subject'):
        if field in request.POST:
            setattr(draft, field, request.POST[field][:998 if field == 'subject' else 10000])
    if 'body_html' in request.POST:
        draft.body_html = request.POST['body_html']
    if request.POST.get('account'):
        account = MailAccount.objects.filter(pk=request.POST['account'], user=request.user, is_active=True).first()
        if account:
            draft.account = account
    draft.save()


@mail_required
@require_POST
def draft_save(request, pk):
    """Autosave a draft; returns the time it was saved."""
    draft = _draft(request, pk)
    _apply_draft_fields(request, draft)
    return JsonResponse({'ok': True, 'saved': timezone.localtime(draft.updated_at).strftime('%H:%M')})


@mail_required
@require_POST
def draft_attach(request, pk):
    """Upload files to a draft, refusing any that would take it over the message size limit."""
    draft = _draft(request, pk)
    added = []
    limit = MailServer.get().max_message_mb * 1024 * 1024
    used = sum(a.size for a in draft.attachments.all())
    for upload in request.FILES.getlist('files'):
        if upload.size > MAX_UPLOAD or used + upload.size > limit:
            return _json_error(f'{upload.name} would take this message over {limit // 1048576} MB.')
        att = MailDraftAttachment(draft=draft, filename=upload.name[:255],
                                  content_type=upload.content_type or '', size=upload.size)
        att.file.save(upload.name, upload, save=False)
        att.save()
        used += upload.size
        added.append(att)
    html = render_to_string('mailbox/_draft_attachments.html', {'attachments': list(draft.attachments.all()),
                                                                 'draft': draft}, request=request)
    return JsonResponse({'ok': True, 'html': html})


@mail_required
@require_POST
def draft_attach_delete(request, pk, att):
    """Remove one attachment from a draft."""
    draft = _draft(request, pk)
    draft.attachments.filter(pk=att).delete()
    html = render_to_string('mailbox/_draft_attachments.html', {'attachments': list(draft.attachments.all()),
                                                                 'draft': draft}, request=request)
    return JsonResponse({'ok': True, 'html': html})


@mail_required
def draft_attachment_download(request, pk, att):
    """Download (or preview) a draft attachment."""
    draft = _draft(request, pk)
    meta = get_object_or_404(draft.attachments, pk=att)
    with meta.file.open('rb') as fh:
        data = fh.read()
    return file_response(request, data, meta.filename, meta.content_type)


@mail_required
@require_POST
def draft_send(request, pk):
    """Save the POSTed fields and send the draft; JSON with an error the sender can act on."""
    draft = _draft(request, pk)
    _apply_draft_fields(request, draft)
    try:
        send.send_draft(draft)
    except send.SendError as exc:
        return _json_error(str(exc))
    return JsonResponse({'ok': True, 'counts': _counts(_accounts(request.user))})


@mail_required
def drafts_outbox(request):
    """The "Send drafts" list: every draft in the viewer's mailboxes, oldest first."""
    rows = []
    for d in (MailDraft.objects.filter(account__user=request.user, account__is_active=True)
              .select_related('account').order_by('account__email_address', 'created_at')):
        try:
            recipients = send.split_addresses(d.to) + send.split_addresses(d.cc) + send.split_addresses(d.bcc)
            problem = '' if recipients else 'No recipient'
        except send.SendError:
            recipients, problem = [], 'Check the addresses'
        rows.append({'id': d.pk, 'from': d.account.email_address, 'to': d.to or d.cc or d.bcc,
                     'subject': d.subject or '(no subject)', 'problem': problem,
                     'attachments': d.attachments.count(),
                     'send_url': reverse('mailbox:draft-send', args=[d.pk])})
    return JsonResponse({'ok': True, 'drafts': rows})


def signature_avatar(request, token):
    """A mailbox owner's ringed profile picture, for signatures pasted into other mail apps.

    Public on purpose (a signature's pictures must load for anyone reading the
    mail); the signed token keeps the address from being guessed.
    """
    from apps.communication.emails import ringed_avatar
    account = signatures.account_from_token(token)
    data = ringed_avatar(account.user) if account else None
    if not data:
        raise Http404
    response = HttpResponse(data, content_type='image/png')
    response['Cache-Control'] = 'public, max-age=86400'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@mail_required
@require_POST
def draft_discard(request, pk):
    """Delete a draft."""
    _draft(request, pk).delete()
    return JsonResponse({'ok': True})


@mail_required
def contacts(request):
    """Recipient suggestions: people on the platform, then people you have mailed."""
    q = (request.GET.get('q') or '').strip()
    if len(q) < 2:
        return JsonResponse({'results': []})
    User = get_user_model()
    out, seen = [], set()
    for user in (User.objects.filter(is_active=True).exclude(email='')
                 .filter(Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
                         | Q(profile__first_name__icontains=q) | Q(profile__last_name__icontains=q))
                 .select_related('profile').distinct()[:8]):
        if user.email.lower() in seen:
            continue
        seen.add(user.email.lower())
        out.append({'name': (user.get_full_name() or '').strip(), 'email': user.email, 'source': 'Metanoia'})
    for name, address in (MailItem.objects.filter(account__user=request.user)
                          .filter(Q(from_address__icontains=q) | Q(from_name__icontains=q))
                          .values_list('from_name', 'from_address').distinct()[:20]):
        if address.lower() in seen or len(out) >= 12:
            continue
        seen.add(address.lower())
        out.append({'name': name, 'email': address, 'source': 'Recent'})
    return JsonResponse({'results': out})


def _new_account_initial(user):
    """Initial values for connecting a mailbox: the viewer's name, and their address when it is on the mail domain."""
    server = MailServer.get()
    initial = {'display_name': (user.get_full_name() or '').strip()}
    if server.domain and user.email and user.email.lower().endswith('@' + server.domain.lower()):
        initial['email_address'] = user.email
    return initial


def _test_account(account):
    """``(ok, message)`` after trying IMAP and SMTP sign-in."""
    problems = []
    try:
        with imap.connect(account) as client:
            client.list_folders()
    except imap.MailError as exc:
        problems.append(f'Receiving (IMAP): {exc}')
    try:
        conn = send.smtp_connection(account)
        conn.open()
        conn.close()
    except send.SendError as exc:
        problems.append(f'Sending (SMTP): {exc}')
    except Exception as exc:
        problems.append(f'Sending (SMTP): {send._friendly_smtp_error(exc)}')
    if problems:
        return False, ' '.join(problems)
    return True, 'Signed in to both the incoming and outgoing servers.'


@mail_required
def settings_page(request):
    """Mail settings: the viewer's mailboxes and, for mail managers, the server and everyone's mailboxes.

    The POSTed ``form`` value picks the action: ``account`` (connect or edit),
    ``server``, ``assign`` (connect for someone else), or ``test`` / ``delete`` /
    ``resync`` / ``signature`` on one mailbox. Only managers may edit others'
    mailboxes or set server overrides.
    """
    user = request.user
    manage = can_manage_mail(user)
    server = MailServer.get()
    edit = None
    if request.GET.get('edit', '').isdigit():
        qs = MailAccount.objects.all() if manage else MailAccount.objects.filter(user=user)
        edit = get_object_or_404(qs, pk=request.GET['edit'])

    account_form = MailAccountForm(instance=edit, owner=edit.user if edit else user, can_override_servers=manage,
                                   initial=None if edit else _new_account_initial(user), prefix='acct')
    server_form = MailServerForm(instance=server, prefix='srv') if manage else None
    assign_form = AssignMailboxForm(prefix='assign') if manage else None

    if request.method == 'POST':
        what = request.POST.get('form')
        if what == 'account':
            account_form = MailAccountForm(request.POST, instance=edit, owner=edit.user if edit else user,
                                           can_override_servers=manage,
                                           prefix='acct')
            if account_form.is_valid():
                account = account_form.save()
                return _after_account_save(request, account, created=edit is None)
        elif what == 'server' and manage:
            server_form = MailServerForm(request.POST, instance=server, prefix='srv')
            if server_form.is_valid():
                server_form.save()
                messages.success(request, 'Mail server settings saved.')
                return redirect('mailbox:settings')
        elif what == 'assign' and manage:
            assign_form = AssignMailboxForm(request.POST, prefix='assign')
            if assign_form.is_valid():
                account = assign_form.save()
                return _after_account_save(request, account, created=True)
        elif what in ('test', 'delete', 'resync', 'signature'):
            qs = MailAccount.objects.all() if manage else MailAccount.objects.filter(user=user)
            account = get_object_or_404(qs, pk=request.POST.get('account'))
            if what == 'test':
                ok, text = _test_account(account)
                (messages.success if ok else messages.error)(request, f'{account.email_address}: {text}')
            elif what == 'signature':
                signatures.apply_default(account, force=True)
                messages.success(request, f'{account.email_address}: the Metanoia signature is back in place.')
                return redirect(reverse('mailbox:settings') + f'?edit={account.pk}#account-form')
            elif what == 'resync':
                sync.run_in_background(sync.sync_account, account)
                messages.info(request, f'{account.email_address}: syncing in the background.')
            else:
                address = account.email_address
                account.delete()
                messages.success(request, f'{address} disconnected. Its mail stays on the server.')
            return redirect('mailbox:settings')

    mine = MailAccount.objects.filter(user=user)
    everyone = MailAccount.objects.select_related('user').order_by('user__first_name', 'email_address') \
        if manage else None
    return render(request, 'mailbox/settings.html', {
        'page_title': 'Mail settings', 'server': server, 'mine': mine, 'everyone': everyone,
        'account_form': account_form, 'server_form': server_form, 'assign_form': assign_form,
        'edit': edit, 'can_manage': manage,
    })


def _after_account_save(request, account, *, created):
    """Apply the default signature, test the connection and start the import after saving a mailbox."""
    signatures.apply_default(account)
    ok, text = _test_account(account)
    if ok:
        sync.run_in_background(sync.sync_account, account, notify=not created)
        messages.success(request, f'{account.email_address} connected. {text} Mail is being imported now.')
    else:
        messages.warning(request, f'{account.email_address} saved, but the connection test failed. {text}')
    if account.user_id == request.user.id and created and ok:
        return redirect('mailbox:inbox')
    return redirect('mailbox:settings')
