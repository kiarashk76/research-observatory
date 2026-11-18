"""Extract user-visible progress and reply text from Claude's JSONL stream.

Thinking text is never exposed; only a generic activity label is emitted.
StructuredOutput arguments are decoded incrementally without showing raw JSON.
"""
import json
import re


def partial_reply(value):
    match=re.search(r'(?:^\s*\{|,)\s*"reply"\s*:\s*"',value)
    if not match:return ''
    raw=value[match.end():];i=0
    while i<len(raw):
        if raw[i]=='"':break
        if raw[i]=='\\':
            if i+1>=len(raw):break
            if raw[i+1]=='u':
                if i+6>len(raw):break
                try:code=int(raw[i+2:i+6],16)
                except ValueError:break
                if 0xD800<=code<=0xDBFF:
                    if i+12>len(raw) or raw[i+6:i+8]!='\\u':break
                    i+=12;continue
                i+=6;continue
            i+=2;continue
        i+=1
    try:return json.loads('"'+raw[:i]+'"')
    except ValueError:return ''


class StreamProgress:
    def __init__(self):self.reply='';self.activity='Connecting to Claude…';self.steps=[];self.blocks={};self.tools={}
    def step(self,label):
        self.activity=label
        if not self.steps or self.steps[-1]!=label:self.steps=(self.steps+[label])[-24:]
    def tool(self,name,args):
        source=str(args.get('source',''))[:180] if isinstance(args,dict) else ''
        page=args.get('page') if isinstance(args,dict) else None
        if name.endswith('search_evidence'):label='Searching workspace evidence'+(': '+str(args.get('query',''))[:160] if isinstance(args,dict) and args.get('query') else '')+'…'
        elif name.endswith('read_evidence'):label='Reading '+(source or 'workspace evidence')+((' · page '+str(page)) if page else '')+'…'
        elif name.endswith(('view_figure','show_figure')):label='Viewing '+(source or 'a figure')+((' · page '+str(page)) if page else '')+'…'
        elif name.endswith('run_plot'):label='Generating a revised plotting copy…'
        elif name.endswith('propose_notes_edit'):label='Preparing a Research Notes proposal…'
        elif name=='StructuredOutput':label='Writing reply…'
        else:label='Working…'
        self.step(label)
    def feed(self,record):
        if not isinstance(record,dict):return
        if record.get('type')=='stream_event':
            event=record.get('event',{});kind=event.get('type');index=event.get('index',0)
            if kind=='message_start':self.blocks={};self.step('Thinking…')
            elif kind=='content_block_start':
                block=dict(event.get('content_block') or {});block['buffer']='';self.blocks[index]=block
                if block.get('type')=='thinking':self.step('Thinking…')
                elif block.get('type')=='tool_use':self.tool(block.get('name',''),block.get('input',{}))
            elif kind=='content_block_delta':
                delta=event.get('delta',{});block=self.blocks.get(index,{})
                if delta.get('type')=='thinking_delta':self.activity='Thinking…'
                elif delta.get('type')=='input_json_delta':
                    block['buffer']=block.get('buffer','')+delta.get('partial_json','')
                    if block.get('name')=='StructuredOutput':self.reply=partial_reply(block['buffer']);self.activity='Writing reply…'
                elif delta.get('type')=='text_delta':self.activity='Preparing response…'
            elif kind=='content_block_stop':
                block=self.blocks.get(index,{})
                if block.get('type')=='tool_use' and block.get('buffer'):
                    try:args=json.loads(block['buffer'])
                    except ValueError:return
                    if block.get('name')!='StructuredOutput':self.tool(block.get('name',''),args)
        elif record.get('type')=='assistant':
            for block in record.get('message',{}).get('content',[]):
                if isinstance(block,dict) and block.get('type')=='tool_use':
                    if block.get('id'):self.tools[block['id']]=block.get('name','')
                    self.tool(block.get('name',''),block.get('input',{}))
                    if block.get('name')=='StructuredOutput':self.reply=block.get('input',{}).get('reply',self.reply)
        elif record.get('type')=='user':
            content=record.get('message',{}).get('content',[])
            for result in content if isinstance(content,list) else []:
                if not isinstance(result,dict) or result.get('type')!='tool_result':continue
                name=self.tools.get(result.get('tool_use_id'),'')
                if not name.endswith(('read_evidence','view_figure')):continue
                for item in result.get('content',[]) if isinstance(result.get('content'),list) else []:
                    if not isinstance(item,dict) or item.get('type')!='text':continue
                    try:value=json.loads(item.get('text',''))
                    except ValueError:continue
                    if isinstance(value,dict) and value.get('title'):self.tool(name,dict(source=value['title'],page=value.get('page')))
        elif record.get('type')=='result' and isinstance(record.get('structured_output'),dict):self.reply=record['structured_output'].get('reply',self.reply)
    def state(self):return dict(live_reply=self.reply,activity=self.activity,progress=self.steps)
