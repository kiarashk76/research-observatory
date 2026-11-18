import test from 'node:test';
import assert from 'node:assert/strict';
import {readColumns} from '../frontend/research-notes/columns.mjs';
import {renderNotes} from '../frontend/research-notes/render.mjs';
const columns=(left,right)=>`:::notes-columns\n\n:::notes-column\n\n${left}\n\n:::end-notes-column\n\n:::notes-column\n\n${right}\n\n:::end-notes-column\n\n:::end-notes-columns`;
test('renders tables, math and alignment in two bounded columns',()=>{
 const source=columns('| A | B |\n| --- | --- |\n| 1 | 2 |',':::notes-align center\n\n> $x^2$\n\n:::end-notes-align');
 assert.equal(readColumns(source).columns.length,2);
 const html=renderNotes(source+'\n\nAfter','p');
 assert.equal((html.match(/data-notes-column=""/g)||[]).length,2);
 assert.match(html,/<table>/);assert.match(html,/class="katex"/);assert.match(html,/<p>After<\/p>/);assert(!html.includes(':::'));
});
test('preserves nested columns and ignores layout markers in fenced code or math',()=>{
 const source=columns('```\n:::end-notes-column\n:::end-notes-columns\n```\n\n$$\n:::end-notes-column\n$$',columns('left','right'));
 assert(readColumns(source));assert.equal((renderNotes(source,'p').match(/data-notes-columns=""/g)||[]).length,2);
 assert.equal(readColumns(':::notes-columns\n\n:::notes-column\nOnly one\n:::end-notes-column\n:::end-notes-columns'),null);
 assert.equal(readColumns(columns('a','b').replace(':::notes-columns',':::notes-columns onclick=evil')),null);
});
test('unequal widths round-trip safely, including nested rows',()=>{
 const source=columns(columns('a','b').replace(':::notes-columns',':::notes-columns 65'),'c').replace(':::notes-columns',':::notes-columns 35');
 assert.equal(readColumns(source).width,35);
 const html=renderNotes(source,'p');assert.match(html,/--notes-column-left:35fr/);assert.match(html,/--notes-column-right:65fr/);assert.match(html,/data-notes-column-width="65"/);
 assert.equal(readColumns(columns('a','b').replace(':::notes-columns',':::notes-columns 99')),null);
 assert.equal(readColumns(columns('a','b').replace(':::notes-columns',':::notes-columns 35;evil')),null);
});
