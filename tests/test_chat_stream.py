import json
import unittest
from labcore.chat_stream import partial_reply,StreamProgress

class ChatStreamTests(unittest.TestCase):
 def test_incremental_reply_handles_escaped_text_and_unicode(self):
  text='A "quote", newline\nmath \\theta and emoji 🦉'
  encoded=json.dumps(dict(memory_additions=[],reply=text))
  for end in range(len(encoded)+1):
   partial=partial_reply(encoded[:end]);self.assertTrue(text.startswith(partial),(end,partial))
  self.assertEqual(partial_reply(encoded),text)
 def test_progress_and_partial_reply_never_expose_thinking(self):
  progress=StreamProgress()
  def feed(event):progress.feed(dict(type='stream_event',event=event))
  feed(dict(type='message_start'));feed(dict(type='content_block_delta',delta=dict(type='thinking_delta',thinking='private secret')))
  self.assertNotIn('private',json.dumps(progress.state()));self.assertEqual(progress.activity,'Thinking…')
  feed(dict(type='content_block_start',index=1,content_block=dict(type='tool_use',name='StructuredOutput')))
  feed(dict(type='content_block_delta',index=1,delta=dict(type='input_json_delta',partial_json='{"reply":"Hello')))
  self.assertEqual(progress.reply,'Hello')
  feed(dict(type='content_block_delta',index=1,delta=dict(type='input_json_delta',partial_json=' world","memory_additions":[]}')))
  self.assertEqual(progress.reply,'Hello world')
 def test_tools_have_bounded_user_facing_labels(self):
  progress=StreamProgress();progress.tool('mcp__research__read_evidence',dict(source='notes',page=2))
  self.assertIn('Reading notes',progress.activity);self.assertIn('page 2',progress.activity)
  for i in range(40):progress.step(str(i))
  self.assertEqual(len(progress.steps),24)
