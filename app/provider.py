"""Provider dispatch: give the pipeline one mail interface that works for both
a Google (Gmail API) user and an IMAP/SMTP (e.g. Hostinger) user."""
import json

from . import auth, gmail_service, imap_service, crypto


class _GmailHandle:
    def __init__(self, creds):
        self._c = creds

    def scan_newsletters(self):
        return gmail_service.scan_newsletters(self._c)

    def fetch_new(self, senders, since):
        return gmail_service.fetch_new_from_senders(self._c, senders, since)

    def send_tracked(self, self_email, subject, html):
        return gmail_service.send_tracked(self._c, self_email, subject, html)

    def get_new_reply(self, thread_id, awaiting_msg_id):
        return gmail_service.get_new_reply(self._c, thread_id, awaiting_msg_id)

    def send_personalized(self, from_email, tos, subject, html_template):
        return gmail_service.send_personalized(self._c, from_email, tos, subject, html_template)


class _ImapHandle:
    def __init__(self, conn):
        self._conn = conn

    def scan_newsletters(self):
        return imap_service.scan_newsletters(self._conn)

    def fetch_new(self, senders, since):
        return imap_service.fetch_new_from_senders(self._conn, senders, since)

    def send_tracked(self, self_email, subject, html):
        return imap_service.send_tracked(self._conn, self_email, subject, html)

    def get_new_reply(self, thread_id, awaiting_msg_id):
        return imap_service.get_new_reply(self._conn, thread_id, awaiting_msg_id)

    def send_personalized(self, from_email, tos, subject, html_template):
        return imap_service.send_personalized(self._conn, from_email, tos, subject, html_template)


def get_handle(user):
    """Return a mail handle for the user's configured provider."""
    if user.provider == "imap":
        conn = json.loads(crypto.decrypt(user.imap_conn_enc))
        return _ImapHandle(conn)
    return _GmailHandle(auth.load_credentials(user))
