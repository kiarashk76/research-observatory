"""Publication gates check real artifacts without calling any model."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
from PIL import Image
from labcore.store import Store
from labcore import studies


class StudyTests(unittest.TestCase):
    def test_table_updates_replace_pdf_link_in_one_current_row(self):
        self.engine.cfg={'research_story':{'format':'study_table'}}
        self.update()
        self.pdf('paper-v2.pdf','Revised standalone scientific report with corrected exposition and linked results.')
        self.update(record=dict(self.record,paper_pdf='reports/paper-v2.pdf',status_note='Detailed report available; paper section updated.'))
        records=self.store.all('studies');self.assertEqual(len(records),1)
        table=self.store.get('project_notes','p:notes/research-story.md')['text']
        self.assertIn('paper-v2.pdf',table);self.assertNotIn('(reports/paper.pdf)',table)
        self.assertIn('paper section updated',table)

    def test_table_mode_needs_two_pdfs_but_no_narrative_or_figure_gallery(self):
        self.engine.cfg={'research_story':{'format':'study_table'}}
        record={k:v for k,v in self.record.items() if k not in ('story_draft','figures')}
        (self.base/'reports/story.md').unlink();(self.base/'reports/figure.png').unlink()
        with self.store.tx() as tx:tx.put('project_notes',dict(id='p:notes/research-story.md',text='Old narrative without current PDF links'))
        value=self.update(record=record)
        self.assertEqual(value['documentation'],'complete')
        note=self.store.get('project_notes','p:notes/research-story.md')
        self.assertIn('| Study | Brief summary | Status | Paper section | Detailed report |',note['text'])
        self.assertIn('reports/paper.pdf',note['text']);self.assertIn('reports/detailed.pdf',note['text'])
        self.assertNotIn('Old narrative',note['text'])
        (self.base/'reports/detailed.pdf').unlink()
        self.assertEqual(self.update(record=record)['documentation'],'incomplete')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.base=self.root/'projects/p';(self.base/'reports').mkdir(parents=True)
        self.store=Store(self.root/'db');self.engine=SimpleNamespace(root=self.root,store=self.store)
        self.session=dict(role='supervisor',project='p')
        with self.store.tx() as tx:
            tx.put('projects',dict(id='p',folder='p'))
            tx.put('tasks',dict(id='v',project='p',role='verifier',status='accepted',verdict='pass'))
            tx.put('project_notes',dict(id='p:notes/research-story.md',text='[Paper](reports/paper.pdf) [Details](reports/detailed.pdf) ![Key result](reports/figure.png)'))
        self.record=dict(title='Study',question='What changes?',execution='completed',verification='verified',verification_task='v',
            task_ids=['v'],technical_record='reports/technical.json',paper_pdf='reports/paper.pdf',detailed_pdf='reports/detailed.pdf',
            story_draft='reports/story.md',figures=['reports/figure.png'],consistency_record='reports/checks.md',qa_record='reports/qa.md',known_gaps=[],review_notes='Reviewed current methods, results, readability and consistency.')
        self.write_sources()

    def pdf(self,name,text):
        writer=PdfWriter();page=writer.add_blank_page(width=600,height=800)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream=DecodedStreamObject();stream.set_data(('BT /F1 12 Tf 50 700 Td ('+text+') Tj ET').encode())
        page[NameObject('/Contents')]=writer._add_object(stream)
        with (self.base/'reports'/name).open('wb') as output:writer.write(output)

    def write_sources(self):
        self.pdf('paper.pdf','Human readable method and experiment results with a clear conclusion.')
        self.pdf('detailed.pdf','Exact technical data configuration and reproduction instructions for this study.')
        (self.base/'reports/source.txt').write_text('Actual implementation record')
        data={key:'Source-backed detail' for key in studies.TECHNICAL_SECTIONS}
        data.update(runs=['run-one'],results={'loss':0.1},limitations=[],missing_information=[],source_paths=['reports/source.txt'])
        (self.base/'reports/technical.json').write_text(json.dumps(data))
        (self.base/'reports/story.md').write_text('Methods, experiment, results and conclusions.')
        (self.base/'reports/checks.md').write_text('Numbers traced to source.txt')
        (self.base/'reports/qa.md').write_text('Version-specific visual and standalone-readability checks')
        Image.new('RGB',(10,10),'white').save(self.base/'reports/figure.png')

    def update(self,publish=True,record=None):
        with self.store.tx() as tx:
            return studies.update(self.engine,tx,self.session,dict(study_id='study-one',study_record=record or self.record,publish=publish))

    def test_complete_requires_available_distinct_searchable_pdfs_and_current_links(self):
        value=self.update();self.assertEqual(value['documentation'],'complete');self.assertEqual(value['checks']['issues'],[])
        self.assertEqual(value['checks']['files']['paper_pdf']['pages'],1)
        self.assertEqual(len(self.store.all('study_versions')),1)
        (self.base/'reports/detailed.pdf').unlink()
        value=self.update();self.assertEqual(value['documentation'],'incomplete')
        self.assertTrue(any('detailed_pdf' in issue for issue in value['checks']['issues']))

    def test_missing_artifacts_do_not_discard_other_completed_work(self):
        value=self.update(record=dict(title='Study',question='Why?'))
        self.assertEqual(value['documentation'],'incomplete')
        self.assertTrue(value['checks']['issues'])
        self.assertEqual(self.store.get('studies','p:study-one')['title'],'Study')

    def test_publishing_needs_supervisor_review_and_story_links(self):
        draft=self.update(False);self.assertEqual(draft['documentation'],'ready_for_review')
        with self.store.tx() as tx:tx.put('project_notes',dict(id='p:notes/research-story.md',text='No links yet'))
        value=self.update(record=dict(self.record,review_notes=''))
        self.assertEqual(value['documentation'],'incomplete')
        self.assertGreaterEqual(len(value['checks']['issues']),3)

    def test_modified_artifacts_invalidate_saved_completion(self):
        saved=self.update();(self.base/'reports/story.md').write_text('Changed version')
        view=studies.current_view(self.root,dict(folder='p'),saved)
        self.assertEqual(view['documentation'],'incomplete')
        self.assertEqual(saved['documentation'],'complete')

    def test_security_and_verification_claims_are_enforced(self):
        self.session['role']='literature'
        with self.assertRaises(ValueError):self.update()
        self.session['role']='supervisor'
        with self.assertRaises(ValueError):self.update(record=dict(self.record,paper_pdf='../../secret.pdf'))
        with self.assertRaises(ValueError):self.update(record=dict(self.record,verification_task='unknown'))
        with self.assertRaises(ValueError):self.update(record=dict(self.record,task_ids=['other-project-task']))

    def test_unfilled_technical_records_and_duplicate_pdfs_do_not_pass(self):
        (self.base/'reports/technical.json').write_text('{"question":"TODO"}')
        value=self.update(record=dict(self.record,detailed_pdf='reports/paper.pdf'))
        self.assertEqual(value['documentation'],'incomplete')
        self.assertTrue(any('different documents' in issue for issue in value['checks']['issues']))

    def test_explicit_missing_provenance_stays_visible_after_publication(self):
        path=self.base/'reports/technical.json';data=json.loads(path.read_text());data['missing_information']=['Original library patch version was not recorded'];path.write_text(json.dumps(data))
        value=self.update();self.assertEqual(value['documentation'],'published_with_gaps')

    def unified_record(self):
        self.pdf('appendix.pdf','Appendix A: Exact reproduction commands, configurations, seeds and evidence inventory.')
        writer=PdfWriter();writer.append(self.base/'reports/paper.pdf');writer.append(self.base/'reports/appendix.pdf')
        with (self.base/'reports/Study_study_report.pdf').open('wb') as output:writer.write(output)
        record={k:v for k,v in self.record.items() if k not in ('paper_pdf','detailed_pdf','technical_record')}
        (self.base/'reports/source.txt').write_text('Implementation evidence: actual data, configuration, run inventory and reproduction commands from checked source files.')
        record.update(report_pdf='reports/Study_study_report.pdf',source_record='reports/source.txt')
        with self.store.tx() as tx:tx.put('project_notes',dict(id='p:notes/research-story.md',text='[Study report](reports/Study_study_report.pdf) ![Key result](reports/figure.png)'))
        return record

    def test_one_report_with_appendix_and_existing_source_can_publish(self):
        record=self.unified_record()
        for path in ('technical.json','paper.pdf','detailed.pdf'):(self.base/'reports'/path).unlink()
        value=self.update(record=record)
        self.assertEqual(value['documentation'],'complete')
        self.assertEqual(value['checks']['files']['report_pdf']['pages'],2)
        self.assertNotIn('paper_pdf',value['checks']['files'])
        self.assertNotIn('technical_record',value['checks']['files'])
        (self.base/'reports/Study_study_report.pdf').unlink()
        self.assertEqual(studies.current_view(self.root,dict(folder='p'),value)['documentation'],'incomplete')

    def test_unified_report_needs_appendix_and_current_link(self):
        record=self.unified_record();record['report_pdf']='reports/paper.pdf'
        value=self.update(record=record)
        self.assertEqual(value['documentation'],'incomplete')
        issues=value['checks']['issues']
        self.assertTrue(any('appendix heading' in issue for issue in issues))
        self.assertTrue(any('not linked' in issue for issue in issues))
        with self.assertRaises(ValueError):self.update(record=dict(record,source_record='../../credentials.txt'))
