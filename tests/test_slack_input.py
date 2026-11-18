"""Incoming Slack tables, rich text and attachments reach the model as text."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_platform as fixtures
from labcore.slack_input import incoming_text, render


def table_message():
    return json.loads((Path(__file__).parent/'fixtures/slack_pasted_table.json').read_text())


class SlackInputTests(unittest.TestCase):
    def test_synthetic_pasted_table_preserves_every_cell_and_intro_once(self):
        event=table_message();text=incoming_text(event)
        table=next(b for b in event['blocks'] if b['type']=='table')
        self.assertEqual(len(table['rows']),9)
        for row in table['rows']:
            for cell in row:self.assertIn(cell['text'],text)
        self.assertEqual(text.count('Please add these papers to the reading list:'),1)
        self.assertEqual(len([line for line in text.splitlines() if line.startswith('| ')]),9)
        self.assertNotIn('with interactive elements',text)

    def test_rich_cells_preserve_links_numbers_and_cell_boundaries(self):
        event=dict(text='',blocks=[dict(type='table',rows=[[
            dict(type='rich_text',elements=[dict(type='rich_text_section',elements=[
                dict(type='link',text='Paper title',url='https://example.org/paper'),dict(type='text',text=' &amp; notes')])]),
            dict(type='raw_number',value=0),dict(type='raw_text',text='a|b\nsecond line')]])])
        text=incoming_text(event)
        self.assertIn('Paper title (https://example.org/paper) & notes',text)
        self.assertIn('| 0 |',text)
        self.assertIn(r'a\|b<br>second line',text)

    def test_lists_quotes_code_and_mentions_retain_content(self):
        section=lambda *items:dict(type='rich_text_section',elements=list(items))
        text=lambda value:dict(type='text',text=value)
        block=dict(type='rich_text',elements=[
            section(dict(type='user',user_id='BOT'),text(' Please compare these:')),
            dict(type='rich_text_list',style='ordered',elements=[section(text('First')),section(text('Second'))]),
            dict(type='rich_text_quote',elements=[text('Source claim')]),
            dict(type='rich_text_preformatted',elements=[text('x = 1\ny = 2')])])
        value=incoming_text(dict(text='',blocks=[block]))
        for expected in ('<@BOT>','1. First','2. Second','> Source claim','```\nx = 1\ny = 2\n```'):
            self.assertIn(expected,value)

    def test_attachment_tables_and_fallback_only_attachments_are_kept(self):
        table=next(b for b in table_message()['blocks'] if b['type']=='table')
        event=dict(text='My instruction',attachments=[dict(title='Sources',title_link='https://example.org',blocks=[table]),dict(fallback='Additional note')])
        text=incoming_text(event)
        self.assertIn('Synthetic paper 1',incoming_text(dict(attachments=[table])))
        for expected in ('My instruction','Sources (https://example.org)','Synthetic paper 1','Additional note'):
            self.assertIn(expected,text)

    def test_plain_messages_commands_and_partial_blocks(self):
        self.assertEqual(incoming_text(dict(text='pause this project')),'pause this project')
        self.assertEqual(incoming_text(dict(text='with interactive elements')),'with interactive elements')
        event=dict(text='Keep this instruction',blocks=[dict(type='section',text=dict(type='mrkdwn',text='More source text'))])
        value=incoming_text(event)
        self.assertIn('Keep this instruction',value);self.assertIn('More source text',value)
        event=dict(text='Repeated',blocks=[dict(type='section',text=dict(type='plain_text',text='Repeated'))])
        self.assertEqual(incoming_text(event),'Repeated')

    def test_slack_link_and_bold_fallbacks_are_not_duplicated(self):
        event=dict(text='See *this* <https://example.org/paper|Paper>',blocks=[dict(type='rich_text',elements=[
            dict(type='rich_text_section',elements=[dict(type='text',text='See this '),dict(type='link',text='Paper',url='https://example.org/paper')])])])
        self.assertEqual(incoming_text(event),'See this Paper (https://example.org/paper)')

    def test_malformed_blocks_do_not_crash_or_silently_hide_unsupported_content(self):
        self.assertEqual(incoming_text(dict(text='Keep me',blocks=42)),'Keep me')
        self.assertIn('Unsupported Slack block',incoming_text(dict(blocks=[dict(type='future_block')])))
        self.assertIn('Unsupported Slack table row',render(dict(type='table',rows=[None])))
        block=dict(type='rich_text',elements=[])
        for _ in range(40):block=dict(type='rich_text',elements=[block])
        self.assertIn('nesting exceeds',render(block))


class SlackTableIntegrationTests(unittest.TestCase):
    setUp=fixtures.PlatformTests.setUp
    cleanup_processes=fixtures.PlatformTests.cleanup_processes

    def event(self):
        return dict(table_message(),channel='C1',ts='101.1',thread_ts='100.1',user='human')

    def test_table_is_saved_in_the_supervisor_request_and_duplicate_delivery_is_idempotent(self):
        event=self.event()
        with patch.object(self.bridge.activity,'sync'),patch('labcore.slack.auth.notice'):
            self.bridge.message(event);self.bridge.message(event)
        message=self.engine.store.get('messages','C1:101.1')
        self.assertIn('Synthetic paper 1',message['text'])
        self.assertIn('Synthetic paper 7',message['text'])
        self.assertNotIn('with interactive elements',message['text'])
        requests=[r for r in self.engine.store.all('requests') if r['id']=='C1:101.1']
        self.assertEqual(len(requests),1)
        self.assertEqual(requests[0]['thread'],'100.1')

    def test_rich_text_only_message_is_received_and_plain_command_still_works(self):
        event=dict(channel='C1',ts='102.1',user='human',text='',blocks=[dict(type='rich_text',elements=[
            dict(type='rich_text_section',elements=[dict(type='text',text='Read this source')])])])
        with patch.object(self.bridge.activity,'sync'),patch('labcore.slack.auth.notice'):
            self.bridge.message(event)
        self.assertEqual(self.engine.store.get('messages','C1:102.1')['text'],'Read this source')
        self.bridge.message(dict(channel='C1',ts='103.1',user='human',text='pause this project',blocks=[dict(type='section',text=dict(type='plain_text',text='pause this project'))]))
        project=self.engine.store.get('channels','C1')['project']
        self.assertEqual(self.engine.store.get('projects',project)['status'],'paused')

    def test_sender_checks_still_apply_before_reading_blocks(self):
        self.cfg['slack']['allowed_user_ids']=['approved-user']
        self.bridge.message(self.event())
        self.assertIsNone(self.engine.store.get('messages','C1:101.1'))
