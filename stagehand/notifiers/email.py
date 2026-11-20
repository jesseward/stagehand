from __future__ import annotations

import asyncio
import logging
import smtplib
from collections.abc import Sequence
from email.message import EmailMessage
from typing import TYPE_CHECKING

from .base import NotifierBase
from .email_config import config as modconfig

if TYPE_CHECKING:
    from ..tvdb import Episode

__all__ = ['Notifier', 'modconfig']

log = logging.getLogger('stagehand.notifiers.email')


class Notifier(NotifierBase):
    def _do_smtp(self, msg: EmailMessage) -> None:
        """
        Send email over SMTP.

        smtplib uses blocking sockets, so this is run in a thread.
        """
        cls = smtplib.SMTP_SSL if modconfig.ssl else smtplib.SMTP
        with cls(str(modconfig.hostname), int(modconfig.port)) as mail:
            mail.ehlo_or_helo_if_needed()
            if not modconfig.ssl and mail.has_extn('starttls'):
                mail.starttls()
                mail.ehlo()
            if modconfig.username:
                mail.login(str(modconfig.username), str(modconfig.password))
            mail.send_message(msg)

    @staticmethod
    def build_message(episodes: Sequence[Episode], sender: str, recipients: list[str]) -> EmailMessage:
        summary = 'Summary of Episodes\n'
        overview = '\nOverview of Episodes\n'
        for i, ep in enumerate(episodes, 1):
            summary += f'{i:02d}: {ep.series.name} {ep.code} {ep.name}\n'
            overview += f'{i:02d}: {ep.series.name} {ep.code} {ep.name} ({ep.airdatetime})\n{ep.overview}\n\n'

        msg = EmailMessage()
        msg['Subject'] = f'[stagehand] downloaded {len(episodes)} episodes'
        msg['From'] = sender
        msg['To'] = ', '.join(recipients)
        msg.set_content(f'{summary}\n{overview}')
        return msg

    async def _notify(self, episodes: Sequence[Episode]) -> None:
        # Sanity check configuration
        if '@' not in modconfig.recipients:
            log.error('invalid recipients, skipping email notification')
            return

        recipients = [addr.strip() for addr in str(modconfig.recipients).split(',')]
        msg = self.build_message(episodes, str(modconfig.sender), recipients)
        try:
            await asyncio.to_thread(self._do_smtp, msg)
        except (smtplib.SMTPException, OSError) as e:
            log.error('unable to send email notification: %s %s', type(e).__name__, e)
        else:
            log.info('sent email notification to %s', modconfig.recipients)
