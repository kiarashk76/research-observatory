import test from 'node:test';
import assert from 'node:assert/strict';
import {renderNotes,readAlignment} from '../frontend/research-notes/render.mjs';
test('alignment accepts only fixed layout values and renders safe Markdown',()=>{
 const source=':::notes-align right\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n:::end-notes-align\n\nAfter';
 assert.equal(readAlignment(source).align,'right');
 const html=renderNotes(source,'p');assert.match(html,/<div data-notes-align="right">\s*<table>/);assert.match(html,/<\/div>\s*<p>After<\/p>/);
 assert(!html.includes(':::notes-align'));
 assert.equal(readAlignment(':::notes-align url(evil)\nBad\n:::end-notes-align'),null);
 assert.equal(readAlignment(':::notes-align center\nUnclosed'),null);
 assert(!renderNotes(':::notes-align center\n\n<script>alert(1)</script>\n\n:::end-notes-align','p').includes('<script>'));
});
test('nested alignment and fenced examples do not prematurely close the container',()=>{
 const source=':::notes-align center\n\n```\n:::end-notes-align\n```\n\n:::notes-align right\n\nInner\n\n:::end-notes-align\n\nOuter\n\n:::end-notes-align\n';
 assert.equal(readAlignment(source).raw,source);
 const html=renderNotes(source,'p');assert.match(html,/<div data-notes-align="center">/);assert.match(html,/<div data-notes-align="right">/);assert.match(html,/<code>:::end-notes-align/);
 assert.match(renderNotes('```\n'+source+'\n```','p'),/<pre><code>/);
});
