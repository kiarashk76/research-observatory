import test from 'node:test';
import assert from 'node:assert/strict';
import {renderNotes} from '../frontend/research-notes/render.mjs';
import {readFontSize} from '../frontend/research-notes/font-syntax.mjs';
test('safe font sizes retain inline formatting, math and code',()=>{
 const source='<span data-notes-font-size="24">**Important** and $x^2$</span>';
 const html=renderNotes(source,'p');assert.match(html,/style="font-size:24px"/);assert.match(html,/<strong>Important<\/strong>/);assert.match(html,/class="katex"/);
 assert.equal(readFontSize(source).size,24);
 assert(!renderNotes('`'+source+'`','p').includes('style="font-size:24px"'));
});
test('font formatting does not accept arbitrary styles, attributes or sizes',()=>{
 for(const source of ['<span data-notes-font-size="9999">bad</span>','<span data-notes-font-size="24" onclick="alert(1)">bad</span>','<span style="background:url(https://evil)">bad</span>']){
  assert.equal(readFontSize(source),null);assert(!renderNotes(source,'p').includes('<span '));
 }
});
