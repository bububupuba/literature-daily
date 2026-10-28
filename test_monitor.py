import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import monitor as m

CONFIG = json.loads((m.ROOT / 'config.json').read_text())


def article(title='Mitochondrial dynamics control metabolism', abstract=None, **kwargs):
    result = dict(doi='10.1234/test', ids=['MED:123'], title=title, abstract=abstract or 'Mitochondrial fusion changes mitochondrial function in animal cells. We used genetic knockout and rescue experiments to measure protein signaling and cellular metabolism. These findings suggest a mechanism that warrants further testing in different tissues and model organisms.', authors='Author A', venue='Unlisted Journal', date='2026-10-27', indexed='', types=['Journal Article'], preprint=False, version=0, url='https://doi.org/10.1234/test', related=[], sources=['fixture'], retracted=False)
    result.update(kwargs)
    return result


class MonitorTests(unittest.TestCase):
    def test_independent_feed_no_overlap_required(self):
        p, _ = m.classify(article(), CONFIG)
        self.assertEqual(p['topics'], ['mitochondria'])
        self.assertFalse(p['preferred'])

    def test_species_anchor_required(self):
        p, _ = m.classify(article(title='Mitochondrial CRISPR screening in mice'), CONFIG)
        self.assertNotIn('celegans', p['topics'])

    def test_c_elegans_broad_topic(self):
        p, _ = m.classify(article(title='Neuronal development in C. elegans'), CONFIG)
        self.assertIn('celegans', p['topics'])

    def test_cross_feed(self):
        p, _ = m.classify(article(title='Mitochondrial lipid metabolism regulates C. elegans longevity'), CONFIG)
        self.assertEqual(len(p['topics']), 4)

    def test_retracted_excluded(self):
        self.assertIsNone(m.classify(article(retracted=True), CONFIG)[0])

    def test_short_abstract_excluded(self):
        self.assertIsNone(m.classify(article(abstract='Unavailable.'), CONFIG)[0])

    def test_incidental_mention_excluded(self):
        p = article(title='A method for measuring tissue development', abstract='Mitochondrial changes are a possible application. ' + 'We measure cell development and gene expression in tissues using fluorescent microscopy. ' * 4)
        self.assertIsNone(m.classify(p, CONFIG)[0])

    def test_doi_normalization(self):
        self.assertEqual(m.doi('https://doi.org/10.1234/ABC'), '10.1234/abc')

    def test_same_doi_across_sources_once(self):
        a = article()
        b = article(ids=['PMC:777'], sources=['other'])
        self.assertEqual(len(m.consolidate([a, b])), 1)

    def test_explicit_journal_relation(self):
        a = article(doi='10.1101/preprint', preprint=True, version=3, related=['10.1234/test'], ids=[])
        p = m.consolidate([a, article()])[0]
        self.assertFalse(p['preprint'])
        self.assertIn('10.1101/preprint', p['related'])

    def test_rs_versions(self):
        a = article(doi='10.21203/rs.3.rs-1/v1', preprint=True, version=1, ids=[])
        b = article(doi='10.21203/rs.3.rs-1/v2', preprint=True, version=2, ids=[])
        self.assertEqual(m.consolidate([a, b])[0]['version'], 2)
        state = {}
        self.assertEqual(len(m.ingest([a], state, CONFIG, '2026-10-28')[0]), 1)
        selected, _, _ = m.ingest([b], state, CONFIG, '2026-10-29')
        self.assertEqual(selected[0]['event'], '预印本版本更新')
        self.assertEqual(m.ingest([b], state, CONFIG, '2026-10-30')[0], [])

    def test_index_change_not_new_paper(self):
        state = {}
        m.ingest([article()], state, CONFIG, '2026-10-28')
        self.assertEqual(m.ingest([article(indexed='2026-10-29')], state, CONFIG, '2026-10-29')[0], [])

    def test_future_and_stale_excluded(self):
        self.assertEqual(m.ingest([article(date='2027-01-01')], {}, CONFIG, '2026-10-28')[0], [])
        self.assertEqual(m.ingest([article(date='2025-01-01')], {}, CONFIG, '2026-10-28')[0], [])

    def test_cap_and_overflow_not_marked_seen(self):
        config = copy.deepcopy(CONFIG)
        config['max_papers'] = 2
        rows = [article(doi=f'10.1234/{i}', ids=[]) for i in range(5)]
        state = {}
        first, deferred, _ = m.ingest(rows, state, config, '2026-10-28')
        second, _, _ = m.ingest(rows, state, config, '2026-10-29')
        self.assertEqual((len(first), deferred, len(second)), (2, 3, 2))
        self.assertFalse({p['doi'] for p in first} & {p['doi'] for p in second})

    def test_similar_titles_not_merged(self):
        self.assertEqual(len(m.consolidate([article(ids=[]), article(doi='10.1234/other', ids=[])])), 2)

    def test_failed_sources_never_zero_success_message(self):
        report = {'date':'2026-10-28', 'start':'2026-10-26', 'papers':[], 'deferred':0, 'sources':{'test':{'status':'failed'}}}
        text = m.render(report, CONFIG)
        self.assertIn('检索缺口', text)
        self.assertNotIn('本次没有新增或更新的合格文献', text)

    def test_missing_push_secret_does_not_send(self):
        with tempfile.TemporaryDirectory() as directory, patch('urllib.request.urlopen') as request:
            with self.assertRaises(RuntimeError):
                m.notify({'date':'2026-10-28'}, Path(directory), 'bububupuba/literature-daily', '')
            request.assert_not_called()

    def test_date_precision_not_invented(self):
        self.assertEqual(m.crossref_date({'posted':{'date-parts':[[2026,10]]}}, ['posted']), '')

    def test_workforce_aging_excluded(self):
        p = article(title='Aging workforce and job satisfaction', abstract='Human workers experience aging in the workplace. ' * 8)
        self.assertIsNone(m.classify(p, CONFIG)[0])

    def test_clinical_association_without_molecular_evidence_excluded(self):
        p = article(title='Triglyceride index and hypertension', abstract='This observational study measures triglyceride concentrations in human patients. Lipid levels were correlated with blood pressure in a prospective cohort. We describe associations in adults across several hospitals. The observed clinical association needs replication with larger samples.')
        self.assertIsNone(m.classify(p, CONFIG)[0])

    def test_html_entities_and_tags_removed(self):
        self.assertEqual(m.clean('NAD&lt;sup&gt;+&lt;/sup&gt;'), 'NAD +')


if __name__ == '__main__':
    unittest.main()
