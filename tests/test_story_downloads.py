"""Current-study ZIP downloads: selections, boundaries and HTTP responses."""
import http.client
import io
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from labcore.dashboard import make_server, story_pdf_archive


class StoryDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {}
        for fid in ('paper', 'detailed', 'other', 'old'):
            path = self.root/(fid+'.pdf')
            path.write_bytes(b'%PDF-1.4\n'+fid.encode())
            self.paths[fid] = path
        self.data = dict(projects=[dict(id='p'), dict(id='q')],
            files=[dict(id=fid, project='q' if fid=='other' else 'p', path=fid+'.pdf') for fid in self.paths],
            studies=[dict(project='p', title='Adaptation / ../ study', paper_pdf='paper.pdf', detailed_pdf='detailed.pdf'),
                     dict(project='p', title='Duplicate link', paper_pdf='paper.pdf'),
                     dict(project='q', title='Other project', paper_pdf='other.pdf')])

    def archive(self, ids=None):
        return zipfile.ZipFile(io.BytesIO(story_pdf_archive(self.data, self.paths, 'p', ids)))

    def test_all_current_pdfs_deduplicated_with_readable_names(self):
        with self.archive() as archive:
            self.assertEqual(len(archive.namelist()), 2)
            self.assertTrue(any(n.endswith('/paper-section.pdf') for n in archive.namelist()))
            self.assertTrue(any(n.endswith('/detailed-report.pdf') for n in archive.namelist()))
            self.assertTrue(all('..' not in n for n in archive.namelist()))
            self.assertEqual(set(archive.read(n) for n in archive.namelist()),
                             {self.paths['paper'].read_bytes(), self.paths['detailed'].read_bytes()})

    def test_select_subset_and_reject_unlinked_or_other_project_files(self):
        with self.archive(['detailed', 'detailed']) as archive:
            self.assertEqual(len(archive.namelist()), 1)
            self.assertEqual(archive.read(archive.namelist()[0]), self.paths['detailed'].read_bytes())
        for ids in ([], ['other'], ['old'], ['../../runtime/lab.sqlite3'], ['paper', 'missing']):
            with self.assertRaises(ValueError):
                self.archive(ids)
        with self.assertRaises(ValueError):
            story_pdf_archive(self.data, self.paths, 'missing')

    def test_missing_links_and_size_limit(self):
        self.data['studies'][0]['detailed_pdf'] = 'missing.pdf'
        with self.archive() as archive:
            self.assertEqual(len(archive.namelist()), 1)
        with self.paths['paper'].open('r+b') as file:
            file.truncate(20_000_001)
        with self.assertRaisesRegex(ValueError, '20 MB'):
            self.archive()

    def test_http_attachment_and_request_boundaries(self):
        server = make_server(self.root, 0)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
        self.addCleanup(conn.close)
        with patch('labcore.dashboard.Dashboard.snapshot', return_value=(self.data, self.paths)):
            conn.request('GET', '/api/story-pdfs?project=p&id=paper', headers={'Accept-Encoding': 'gzip'})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Content-Type'), 'application/zip')
            self.assertIsNone(response.getheader('Content-Encoding'))
            self.assertIn('attachment;', response.getheader('Content-Disposition'))
            with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                self.assertEqual(len(archive.namelist()), 1)
            for path, headers, expected in (
                ('/api/story-pdfs?project=p&id=other', {}, 400),
                ('/api/story-pdfs?project=p&id=', {}, 400),
                ('/api/story-pdfs?project=p', {'Origin': 'https://evil.example'}, 403)):
                conn.request('GET', path, headers=headers)
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, expected)


if __name__ == '__main__':
    unittest.main()
