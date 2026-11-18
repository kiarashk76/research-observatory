import test from 'node:test';
import assert from 'node:assert/strict';
import {renderNotes} from '../frontend/research-notes/render.mjs';
import {readUnderline} from '../frontend/research-notes/underline-syntax.mjs';
test('underline combines with bold, font sizes and equations',()=>{
 const html=renderNotes('<u>**Bold** <span data-notes-font-size="24">large</span> $x^2$</u>','p');
 assert.match(html,/<u><strong>Bold<\/strong>/);assert.match(html,/font-size:24px/);assert.match(html,/class="katex"/);assert.match(html,/<\/u>/);
 assert.equal(readUnderline('<u>`</u>` text</u>').body,'`</u>` text');
 assert(!renderNotes('`<u>code</u>`','p').includes('<u>'));
});
test('underline accepts no arbitrary tag attributes',()=>{
 for(const source of ['<u onclick="evil()">bad</u>','<u style="color:red">bad</u>','<u>unclosed'])assert.equal(readUnderline(source),null);
 assert(!renderNotes('<u onclick="evil()">bad</u>','p').includes('<u '));
});
