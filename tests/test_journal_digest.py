"""Offline tests: python -m unittest discover -s tests -v"""
import importlib.util
import json
import os
import tempfile
import unittest
from collections import OrderedDict
from datetime import date
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'journal_digest.py'
spec = importlib.util.spec_from_file_location('journal_digest', MODULE_PATH)
jd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jd)


class DummyResponse:
    def __init__(self, json_body=None, content=b''):
        self.data = json_body
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class DummySession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.outcomes.pop(0)


class DigestTests(unittest.TestCase):
    def test_date_precision_and_online_priority(self):
        item = {
            'DOI': '10.1000/ABC',
            'title': ['Paper <em>title</em>'],
            'published-online': {'date-parts': [[2026, 11, 4]]},
            'published': {'date-parts': [[2027, 1, 1]]},
            'created': {'date-parts': [[2026, 11, 5]]},
        }
        paper = jd.make_crossref_paper(item, 'IJRMMS')
        self.assertEqual(paper['doi'], '10.1000/abc')
        self.assertEqual(paper['pub_date'], date(2026, 11, 4))
        self.assertEqual(paper['date_kind'], '在线发表')
        self.assertEqual(paper['title'], 'Paper title')

    def test_crossref_falls_back_to_creation_if_publication_date_imprecise(self):
        item = {'DOI': '10.1000/example', 'title': ['Sample'],
                'published-online': {'date-parts': [[2026, 11]]},
                'issued': {'date-parts': [[2026]]},
                'created': {'date-parts': [[2026, 11, 5]]}}
        paper = jd.make_crossref_paper(item, 'Journal')
        self.assertEqual(paper['pub_date'], date(2026, 11, 5))
        self.assertIn('DOI', paper['date_kind'])

    def test_crossref_query_uses_creation_and_online_publication(self):
        rec = {'DOI': '10.1000/test', 'title': ['X'], 'created': {'date-parts': [[2026,11,5]]}}
        session = DummySession([DummyResponse({'message': {'items': [rec]}}), DummyResponse({'message': {'items': [rec]}})])
        papers = jd.fetch_crossref(session, {'crossref': {}}, '1365-1609', 'Test', date(2026, 11, 1), date(2026, 11, 6))
        self.assertEqual(len(papers), 1)
        self.assertIn('from-created-date:2026-11-01', session.calls[0][1]['params']['filter'])
        self.assertIn('from-online-pub-date:2026-11-01', session.calls[1][1]['params']['filter'])

    def test_rss_and_atom(self):
        rss = b'''<rss><channel><item><title>Rock paper</title><link>https://doi.org/10.1234/abc</link><pubDate>Thu, 05 Nov 2026 00:00:00 GMT</pubDate></item></channel></rss>'''
        atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Geo paper</title><id>doi:10.1234/xyz</id><link rel="alternate" href="https://example.org/a"/><published>2026-11-04T01:02:00Z</published></entry></feed>'''
        session = DummySession([DummyResponse(content=rss), DummyResponse(content=atom)])
        a = jd.fetch_rss(session, 'https://example.org/rss', 'A', 10)
        b = jd.fetch_rss(session, 'https://example.org/atom', 'B', 10)
        self.assertEqual(a[0]['doi'], '10.1234/abc')
        self.assertEqual(b[0]['doi'], '10.1234/xyz')
        self.assertEqual(b[0]['pub_date'], date(2026, 11, 4))

    def test_selection_dedup_and_exclusion_of_old(self):
        def p(doi, title, dt, source='Crossref'):
            return {'doi': doi, 'title': title, 'pub_date': dt, 'authors': [],
                    'source': source, 'date_kind': '在线发表',
                    'url': 'https://doi.org/' + doi, 'journal': 'J'}
        raw = [p('10.2/recent', 'Recent', date(2026,11,5)),
               p('10.2/recent', 'Recent', date(2026,11,5), 'RSS'),
               p('10.2/old', 'Old', date(2024,1,1)),
               p('10.2/sent', 'Sent', date(2026,11,4))]
        cfg = {'lookback_days': 7, 'journals': [{'name': 'J', 'issn':'1365-1609'}]}
        state = {'seen': {'doi:10.2/sent': '2026-11-04'}}
        with patch.object(jd, 'fetch_crossref', return_value=raw):
            groups, keys, _ = jd.select_new(cfg, state, DummySession([]), date(2026,11,6))
        self.assertEqual([x['title'] for x in groups['J']], ['Recent'])
        self.assertIn('doi:10.2/recent', keys)
        self.assertNotIn('doi:10.2/sent', keys)

    def test_html_escapes_untrusted_titles(self):
        groups = OrderedDict([('J', [{'title': '<script>alert(1)</script>',
                                       'pub_date': date(2026, 11, 5),
                                       'date_kind':'在线发表', 'doi':'10.1/abc',
                                       'authors': [], 'source': 'Crossref',
                                       'url': 'javascript:alert(1)'}])])
        rendered = jd.render_html(groups, date(2026, 11, 6), 7)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('href="javascript:', rendered)

    def test_dry_run_no_mail_or_state_change_and_successful_send_updates_state(self):
        with tempfile.TemporaryDirectory() as work:
            cfg_path = Path(work)/'config.yaml'
            cfg_path.write_text('lookback_days: 7\njournals:\n  - name: J\n    issn: "1365-1609"\n', encoding='utf-8')
            state_path = Path(work)/'state.json'
            state_path.write_text('{"schema": 1, "seen": {}}', encoding='utf-8')
            report = Path(work)/'reports'
            paper = {'title': 'One', 'doi': '10.1000/p', 'url':'https://doi.org/10.1000/p',
                     'authors': [], 'pub_date': date(2026, 11, 5),
                     'date_kind':'在线发表','journal':'J','source':'Crossref'}
            with patch.object(jd, 'fetch_crossref', return_value=[paper]), \
                    patch.object(jd, 'send_email') as mail, \
                    patch.dict(os.environ, {'DRY_RUN':'false'}, clear=False):
                argv = ['--config', str(cfg_path), '--state', str(state_path),
                        '--report-dir', str(report), '--date', '2026-11-06']
                self.assertEqual(jd.main(argv + ['--dry-run']), 0)
                mail.assert_not_called()
                self.assertEqual(json.loads(state_path.read_text())['seen'], {})
                self.assertEqual(jd.main(argv), 0)
                mail.assert_called_once()
                self.assertIn('doi:10.1000/p', json.loads(state_path.read_text())['seen'])


if __name__ == '__main__':
    unittest.main()
