"""Narrative publishing reuses note history; Slack homes update without duplicates."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from labcore.store import Store
from labcore.research_story import ProjectHomes, PATH, story_context, home_message


class ResearchStoryTests(unittest.TestCase):
    def test_table_context_excludes_old_narrative_and_editorial_feedback(self):
        self.engine.cfg={'research_story':{'format':'study_table'}}
        context=story_context(self.engine,'p')
        self.assertEqual(context['format'],'study_table')
        self.assertNotIn('text',context);self.assertNotIn('review',context)

    def test_home_is_native_blocks_not_truncated_markdown(self):
        text, blocks = home_message({'name':'Study'}, {'text':'# Paper\n\nA very long [link](notes/a.md) **paragraph**.\n\n### Q1: Memory (verified)\n\n![caption](reports/figure.png)', 'time':'2026-10-31'})
        self.assertIn('Q1: Memory (verified)', text)
        self.assertNotIn('notes/a.md', str(blocks))
        self.assertNotIn('![', str(blocks))
        self.assertEqual(blocks[0]['type'], 'header')
        self.assertTrue(all(b.get('text', {}).get('type') == 'plain_text' for b in blocks if 'text' in b))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = SimpleNamespace(store=Store(Path(self.temp.name)/'lab.sqlite3'))
        with self.engine.store.tx() as tx:
            tx.put('projects', dict(id='p',name='Study'))
            tx.put('channels', dict(id='c',name='study',project='p',active=True))
            tx.put('project_notes', dict(id='p:'+PATH,project='p',path=PATH,text='# Goal\nEvidence',time='2026-10-31T00:00:00Z'))
        self.client = Mock()
        self.client.chat_postMessage.return_value = {'ts':'123.456'}
        self.homes = ProjectHomes(self.engine,self.client)

    def test_post_pin_then_update_same_message(self):
        self.homes.sync()
        self.client.chat_postMessage.assert_called_once()
        self.client.pins_add.assert_called_once_with(channel='c',timestamp='123.456')
        self.homes.next_check=0
        self.homes.sync()
        self.client.chat_update.assert_not_called()
        with self.engine.store.tx() as tx:
            note=tx.get('project_notes','p:'+PATH);note['text']+='\nNew finding';tx.put('project_notes',note)
        self.homes.next_check=0
        self.homes.sync()
        self.client.chat_update.assert_called_once()
        self.assertEqual(self.client.chat_postMessage.call_count,1)
        self.assertIn('New finding',story_context(self.engine,'p')['text'])

    def test_pin_failure_keeps_message_and_backs_off(self):
        self.client.pins_add.side_effect=RuntimeError('Missing scope')
        self.homes.sync()
        state=self.engine.store.get('settings','project_home:c')
        self.assertEqual(state['timestamp'],'123.456')
        self.assertEqual(state['error'],'RuntimeError')
        self.homes.next_check=0
        self.homes.sync()
        self.assertEqual(self.client.chat_postMessage.call_count,1)
