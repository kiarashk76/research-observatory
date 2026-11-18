"""Offline CLI fixture; never uses subscription capacity."""
import json,os,sys,time
from pathlib import Path
text=sys.stdin.read();args=sys.argv
if 'slow response' in text:time.sleep(30)
if 'panel delayed' in text:time.sleep(2.5)
account=Path(os.environ['CLAUDE_CONFIG_DIR']).name
reply='Reply from '+account+'. '+('Shared memory found.' if 'Plain language please' in text else 'Research evidence is available.')
if 'long scroll probe' in text:reply+='\n\n'+'\n\n'.join('Paragraph '+str(i)+': '+('Detailed research explanation. '*12) for i in range(45))+'\n\nEND_OF_LONG_REPLY'
if 'stream probe' in text:
 def emit(event):print(json.dumps(dict(type='stream_event',event=event)),flush=True)
 emit(dict(type='message_start'))
 emit(dict(type='content_block_start',index=0,content_block=dict(type='thinking')))
 emit(dict(type='content_block_delta',index=0,delta=dict(type='thinking_delta',thinking='PRIVATE_REASONING_DO_NOT_SHOW')))
 time.sleep(.3)
 emit(dict(type='content_block_start',index=1,content_block=dict(type='tool_use',name='mcp__research__read_evidence',input={})))
 emit(dict(type='content_block_delta',index=1,delta=dict(type='input_json_delta',partial_json='{"source":"notes"}')))
 emit(dict(type='content_block_stop',index=1));time.sleep(.6)
 reply='Streaming answer: first visible words, then the completed response.'
 emit(dict(type='content_block_start',index=2,content_block=dict(type='tool_use',name='StructuredOutput',input={})))
 encoded=json.dumps(dict(reply=reply,memory_additions=[]))
 for start in range(0,len(encoded),6):
  emit(dict(type='content_block_delta',index=2,delta=dict(type='input_json_delta',partial_json=encoded[start:start+6])));time.sleep(.12)
 time.sleep(.5)
if 'figure probe' in text:
 sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
 from labcore.chat_evidence import Evidence
 root=Path.cwd().parents[3];pid=Path.cwd().parent.name;cid=Path.cwd().name
 evidence=Evidence(root,pid,cid)
 source=next(key for key,value in evidence.sources.items() if value['path'] and value['path'].name=='original.png')
 evidence.call('show_figure',dict(source=source,caption='Original pilot figure'))
 code="from pathlib import Path\nimport matplotlib.pyplot as plt\nfig,ax=plt.subplots()\nax.plot([1,2,3],[2,4,6],label='Revised pilot')\nax.set_xlabel('Observation number');ax.set_ylabel('Response');ax.legend()\nfig.savefig(Path(OUTPUT_DIR)/'revised.png')\nfig.savefig(Path(OUTPUT_DIR)/'revised.pdf')\n"
 evidence.call('run_plot',dict(sources=[],code=code,caption='Revised pilot labels'))
 reply='The original figure and revised plotting copies are attached.'
output=dict(reply=reply,memory_additions=['Plain language please'] if 'remember my preference' in text else [])
sid=args[args.index('--session-id')+1]
folder=Path(os.environ['CLAUDE_CONFIG_DIR'])/'projects/fake';folder.mkdir(parents=True,exist_ok=True)
(folder/(sid+'.jsonl')).write_text(json.dumps(dict(type='user',message=dict(role='user',content=text)))+'\n'+json.dumps(dict(type='assistant',message=dict(role='assistant',content=[dict(type='text',text=reply)])))+'\n')
print(json.dumps(dict(type='result',is_error=False,structured_output=output,usage=dict(input_tokens=10,output_tokens=10))))
