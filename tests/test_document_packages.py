"""Submitted documents become visible without additional agent calls."""
from pathlib import Path
import tempfile
import unittest
from labcore.store import Store
from labcore.document_packages import register


class DocumentPackageTests(unittest.TestCase):
    def test_one_named_report_is_visible_without_a_companion_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base=root/'projects/p/reports';base.mkdir(parents=True)
            report=base/'Q1_study_report.pdf';report.write_bytes(b'Draft PDF')
            store=Store(root/'db')
            with store.tx() as tx:
                tx.put('projects',dict(id='p',folder='p'))
                register(tx,root,dict(id='s',project='p',task='t'),[dict(path=str(report))])
            record=store.all('document_packages')[0]
            self.assertEqual(record['study_key'],'q1')
            self.assertEqual(record['report_pdf'],'reports/Q1_study_report.pdf')
            self.assertNotIn('paper_pdf',record)

    def test_only_complete_project_owned_named_pairs_are_registered(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base=root/'projects/p/reports';base.mkdir(parents=True)
            paths=[]
            for name in ('Q5_paper_section.pdf','Q5_detailed_report.pdf','Q1_paper_section.pdf'):
                path=base/name;path.write_bytes(b'Draft PDF');paths.append({'path':str(path)})
            foreign=root/'Other_detailed_report.pdf';foreign.write_bytes(b'Foreign');paths.append({'path':str(foreign)})
            store=Store(root/'db')
            with store.tx() as tx:
                tx.put('projects',dict(id='p',folder='p'))
                register(tx,root,dict(id='s',project='p',task='t'),paths)
            records=store.all('document_packages')
            self.assertEqual(len(records),1);self.assertEqual(records[0]['study_key'],'q5')
            self.assertEqual(records[0]['task'],'t')
            self.assertNotIn('accepted',records[0])
            with store.tx() as tx:register(tx,root,dict(id='s',project='p',task='t'),paths)
            self.assertEqual(len(store.all('document_packages')),1)
