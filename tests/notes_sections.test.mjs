import test from 'node:test';
import assert from 'node:assert/strict';
import {markdownSections} from '../frontend/research-notes/sections.mjs';
test('copies through next same-level heading, including intervening higher levels',()=>{
 const source='# A\nintro\n## B\n> box\n### C\n$x^2$\n# D\nmore\n## E\nend';
 const sections=markdownSections(source);
 assert.equal(sections[0].markdown,'# A\nintro\n## B\n> box\n### C\n$x^2$\n');
 assert.equal(sections[1].markdown,'## B\n> box\n### C\n$x^2$\n# D\nmore\n');
 assert.equal(sections[2].markdown,'### C\n$x^2$\n# D\nmore\n## E\nend\n');
 assert.equal(sections.at(-1).markdown,'## E\nend\n');
});
test('ignores code and quote headings, supports duplicate titles and tables',()=>{
 const source='# Same\n```md\n# Fake\n```\n> # Quote\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n# Same\nlast';
 const sections=markdownSections(source);assert.equal(sections.length,2);
 assert.match(sections[0].markdown,/\| 1 \| 2 \|/);assert.equal(sections[1].title,'Same');
 assert.equal(markdownSections('no headings').length,0);
});
test('balances alignment containers crossing either section boundary',()=>{
 const source=':::notes-align center\n\n# A\n\n:::end-notes-align\n\n:::notes-align right\n\n# B\ntext\n\n:::end-notes-align';
 const sections=markdownSections(source);
 assert.equal(sections[0].markdown,':::notes-align center\n\n# A\n\n:::end-notes-align\n\n:::notes-align right\n\n:::end-notes-align\n');
 assert.equal(sections[1].markdown,':::notes-align right\n\n# B\ntext\n\n:::end-notes-align\n');
});
test('display equations with equals signs are not mistaken for H1 headings',()=>{
 for(const [open,close] of [['$$','$$'],['\\[','\\]']]){
  const equation=`${open}\nR_t\n=\n\\mathbb E_{X\\sim q}[X]\n${close}`;
  const source=`## Learning and Evaluation\n\n:::notes-align center\n\n${equation}\n\n:::end-notes-align\n\n## Experiments\nnext`;
  const sections=markdownSections(source);
  assert.deepEqual(sections.map(s=>s.title),['Learning and Evaluation','Experiments']);
  assert(sections[0].markdown.includes(equation));
 }
});
test('section exports flatten partial columns but preserve fenced layout examples',()=>{
 const source=':::notes-columns\n\n:::notes-column\n\n## Left\n\nLeft text\n\n:::end-notes-column\n\n:::notes-column\n\n## Right\n\n```\n:::notes-column\n```\n\n:::end-notes-column\n\n:::end-notes-columns';
 const sections=markdownSections(source);
 assert.equal(sections[0].markdown,'## Left\n\nLeft text\n');
 assert.equal(sections[1].markdown,'## Right\n\n```\n:::notes-column\n```\n');
});
