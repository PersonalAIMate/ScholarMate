"""Email delivery via SMTP, configured entirely through environment variables.

Any SMTP provider works without code changes — Gmail, Resend, SendGrid,
Mailgun, Amazon SES, etc. To enable sending, set in the environment:

    SMTP_HOST     e.g. smtp.gmail.com / smtp.resend.com / email-smtp.<region>.amazonaws.com
    SMTP_USER     SMTP username (often the full email, or an API-key user)
    SMTP_PASS     SMTP password / API key / app password

Optional:
    SMTP_PORT     default 587
    SMTP_FROM     From address (default: SMTP_USER)
    SMTP_USE_TLS  '1' (default) to STARTTLS, '0' to disable

If the required vars are absent, send_email() is a no-op that returns False, so
the rest of the app keeps working without email configured.
"""
import logging
import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

log = logging.getLogger('scholarmate.email')


def email_enabled():
    return bool(
        os.environ.get('SMTP_HOST')
        and os.environ.get('SMTP_USER')
        and os.environ.get('SMTP_PASS')
    )


def send_email(to_addr, subject, body_text):
    """Send a plain-text email. Returns True on success, False otherwise.

    Never raises — failures are logged so a single bad address can't abort a
    batch digest run.
    """
    if not email_enabled():
        log.warning('email not configured (SMTP_HOST/USER/PASS missing); '
                    'skipping send to %s', to_addr)
        return False

    host    = os.environ['SMTP_HOST']
    port    = int(os.environ.get('SMTP_PORT', '587'))
    user    = os.environ['SMTP_USER']
    pwd     = os.environ['SMTP_PASS']
    sender  = os.environ.get('SMTP_FROM', user)
    use_tls = os.environ.get('SMTP_USE_TLS', '1') != '0'

    msg = MIMEText(body_text, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From']    = formataddr(('ScholarMate', sender))
    msg['To']      = to_addr

    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            if use_tls:
                server.starttls()
            server.login(user, pwd)
            server.sendmail(sender, [to_addr], msg.as_string())
        log.info('sent email to %s', to_addr)
        return True
    except Exception:
        log.exception('failed to send email to %s', to_addr)
        return False


def render_digest(email, papers, keywords):
    """Build the plain-text body of a recommendations digest."""
    lines = [
        f'Hi {email},',
        '',
        'Here are your latest paper recommendations from ScholarMate:',
        '',
    ]
    for i, p in enumerate(papers, 1):
        authors_list = p.get('authors', []) or []
        authors = ', '.join(authors_list[:3])
        if len(authors_list) > 3:
            authors += ' et al.'
        link = (p.get('link') or '').replace('http://', 'https://')
        lines.append(f'{i}. {p.get("title", "")}')
        if authors:
            lines.append(f'   {authors} · {p.get("published", "")}')
        lines.append(f'   {link}')
        lines.append('')

    if keywords:
        lines.append('Keywords used: ' + ', '.join(keywords))
        lines.append('')
    lines.append('— ScholarMate · https://scholarmate.org')
    return '\n'.join(lines)
