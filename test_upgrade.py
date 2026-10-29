import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET
import urllib.error
from email.message import Message
from unittest.mock import MagicMock

import monitor as m
import sources_extra as sources
import delivery as delivery
from test_monitor import article, CONFIG


def candidate(index, topics, preprint=False, score=20, venue='Unlisted Journal'):
    return dict(article(doi=f'10.1234/{"p" if preprint else "j"}{index}', ids=[], preprint=preprint, venue=venue),
                key=f'{preprint}:{index}', topics=topics, score=score, preferred=False,
                reasons={t: {'title_terms': ['fixture'], 'abstract_terms': [], 'supporting_terms': []} for t in topics}, signals={})


def report(date, papers=None):
    return {'date':date, 'start':'2026-10-26', 'papers':papers or [], 'deferred':0, 'sources':{'fixture':{'status':'ok'}}, 'preview':False}


class SelectionTests(unittest.TestCase):
    def test_five_by_five_in_each_pool_and_unique(self):
        rows = []
        for preprint in (False, True):
            for i, topic in enumerate(CONFIG['topics']):
                rows.extend(candidate(i * 10 + n, [topic], preprint) for n in range(5))
            rows.extend(candidate(100+n, ['mitochondria','aging'], preprint) for n in range(5))
        selected = m.choose(rows, CONFIG)
        self.assertEqual(len(selected), 50)
        self.assertEqual(len({p['key'] for p in selected}), 50)
        for preprint in (False, True):
            for feed in list(CONFIG['topics'])+['interdisciplinary']:
                self.assertEqual(sum(p['preprint']==preprint and p['feed']==feed for p in selected), 5)

    def test_shortage_redistributed_within_pool(self):
        rows = [candidate(i,['mitochondria']) for i in range(40)] + [candidate(100+i,['celegans']) for i in range(2)]
        rows += [candidate(200+i,['aging']) for i in range(8)] + [candidate(300+i,['lipids']) for i in range(8)]
        selected = m.choose(rows, CONFIG)
        self.assertEqual(len(selected),58)
        self.assertEqual(sum(p['feed']=='celegans' for p in selected),2)
        self.assertEqual(sum(p['feed']=='interdisciplinary' for p in selected),0)
        self.assertEqual(sum(p['feed']=='mitochondria' for p in selected),40)

    def test_pool_does_not_borrow_preprint_shortage(self):
        rows = [candidate(i,['mitochondria']) for i in range(40)] + [candidate(i,['aging'],True) for i in range(2)]
        selected=m.choose(rows,CONFIG)
        self.assertEqual(len(selected),42)
        self.assertEqual(sum(not p['preprint'] for p in selected),40)

    def test_impact_factor_overrides_score_for_journals(self):
        a=candidate(1,['lipids'],score=99,venue='Journal of Lipid Research')
        b=candidate(2,['lipids'],score=3,venue='Nature Communications')
        self.assertEqual(sorted([m.decorate(a,CONFIG),m.decorate(b,CONFIG)],key=m.rank)[0]['key'],b['key'])

    def test_preprint_never_inherits_venue_impact_factor(self):
        p=m.decorate(candidate(1,['aging'],True,venue='Nature'),CONFIG)
        self.assertIsNone(p['jif'])

    def test_venue_alias_matches_pubmed_style(self):
        p=m.decorate(candidate(1,['aging'],venue='Nature reviews. Cardiology'),CONFIG)
        self.assertEqual(p['jif']['journal'],'Nature Reviews Cardiology')

    def test_unknown_impact_factor_not_invented(self):
        p=m.decorate(candidate(1,['aging'],venue='New Unindexed Journal'),CONFIG)
        self.assertIsNone(p['jif'])

    def test_tier_independent_of_journal_prestige(self):
        self.assertEqual(m.decorate(candidate(1,['aging'],score=3,venue='Nature'),CONFIG)['tier'],'其他相关')

    def test_earliest_online_date_preserved_and_abstract_supplemented(self):
        a=article(date='2026-12-01',abstract='Short feed description',authors='')
        b=article(date='2026-10-27')
        p=m.consolidate([a,b])[0]
        self.assertEqual(p['date'],'2026-10-27')
        self.assertEqual(p['abstract'],b['abstract'])

    def test_preprint_revision_date_not_replaced_by_original_v1_date(self):
        a=article(preprint=True,version=2,date='2026-10-01',sources=['Europe PMC'])
        b=article(preprint=True,version=2,date='2026-10-27',sources=['biorxiv'],date_kind='version_posted')
        self.assertEqual(m.consolidate([a,b])[0]['date'],'2026-10-27')

    def test_identifiers_link_groups_transitively(self):
        a=article(doi='10.1234/a',ids=[])
        b=article(doi='10.1234/b',ids=[])
        bridge=article(doi='10.1234/c',ids=[],related=['10.1234/a','10.1234/b'])
        self.assertEqual(len(m.consolidate([a,b,bridge])),1)

    def test_render_has_two_separate_pools_and_metrics_sources(self):
        p=candidate(1,['aging'],venue='Nature');p.update(feed='aging',event='新收录',title_zh='衰老研究标题')
        text=m.render(report('2026-10-28',[p]),CONFIG)
        self.assertIn('期刊论文 · 1 篇',text)
        self.assertIn('预印本（未经同行评审） · 0 篇',text)
        self.assertIn('56.1',text)
        self.assertIn('journal-metrics',text)


class SourceTests(unittest.TestCase):
    def test_crossref_rate_limit_respects_retry_after(self):
        headers=Message();headers['Retry-After']='7'
        rejected=urllib.error.HTTPError('https://api.crossref.org/works',429,'limit',headers,None)
        response=MagicMock();response.__enter__.return_value.read.return_value=b'{}'
        with patch('urllib.request.urlopen',side_effect=[rejected,response]) as request,patch.object(m.time,'sleep') as sleep:
            self.assertEqual(m.Client().get('https://api.crossref.org/works'),{})
            self.assertEqual(request.call_count,2)
            self.assertTrue(any(call.args[0]>=7 for call in sleep.call_args_list))

    def test_nature_news_not_recommended_as_original_research(self):
        p=article(doi='10.1038/d41586-026-00001-1')
        self.assertIsNone(m.classify(p,CONFIG)[0])

    def test_rdf_feed_parse_namespaces_doi_and_date(self):
        xml=b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:p="http://prismstandard.org/namespaces/basic/2.0/"><item><title>Mitochondrial work</title><link>https://example.org/article</link><dc:creator>Author A</dc:creator><dc:date>2026-10-27</dc:date><p:doi>10.1234/XYZ</p:doi><description>Abstract here</description></item></rdf:RDF>'''
        rows,status=sources.parse_feed(xml,{'name':'Nature','url':'https://www.nature.com/nature.rss'},'2026-10-26','2026-10-28')
        self.assertEqual(rows[0]['doi'],'10.1234/xyz')
        self.assertEqual(rows[0]['authors'],'Author A')
        self.assertIn('滚动',status['scope'])

    def test_atom_feed_parse_and_old_item_filtered(self):
        xml=b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Lipid research</title><link href="https://doi.org/10.1234/test"/><published>2026-10-27T12:00:00Z</published><summary>Abstract</summary></entry><entry><title>Old</title><link href="https://doi.org/10.1234/old"/><published>2025-01-01</published></entry></feed>'''
        rows,_=sources.parse_feed(xml,{'name':'Test','url':'https://example.org/feed'},'2026-10-26','2026-10-28')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['doi'],'10.1234/test')

    def test_pubmed_electronic_date_and_retraction(self):
        xml='''<PubmedArticle><MedlineCitation><PMID>123</PMID><Article><ArticleTitle>Test <i>mitochondrial</i> study</ArticleTitle><Journal><Title>Test Journal</Title><JournalIssue><PubDate><Year>2027</Year><Month>1</Month><Day>1</Day></PubDate></JournalIssue></Journal><Abstract><AbstractText Label="RESULTS">Experimental results</AbstractText></Abstract><ArticleDate DateType="Electronic"><Year>2026</Year><Month>Oct</Month><Day>27</Day></ArticleDate><PublicationTypeList><PublicationType>Retracted Publication</PublicationType></PublicationTypeList></Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">10.1234/test</ArticleId></ArticleIdList></PubmedData></PubmedArticle>'''
        p=sources.pubmed_article(ET.fromstring(xml))
        self.assertEqual(p['date'],'2026-10-27')
        self.assertTrue(p['retracted'])
        self.assertEqual(p['ids'],['PMID:123'])
        self.assertIn('RESULTS:',p['abstract'])

    def test_crossref_partial_query_failure_preserves_other_results(self):
        with patch.object(sources,'fetch_crossref_query',side_effect=[([article()],{'status':'ok','available':1}),RuntimeError()]):
            rows,status=sources.fetch_crossref_journals(None,['mitochondria','mitophagy'],'mitochondria','2026-10-26','2026-10-28',10)
        self.assertEqual(len(rows),1)
        self.assertEqual(status['status'],'partial')

    def test_epmc_outage_other_sources_continue(self):
        cfg=copy.deepcopy(CONFIG)
        cfg['sources'].update(biorxiv=False,medrxiv=False,research_square=False,rss=False,crossref_journals=False,pubmed=True)
        with patch.object(m,'fetch_epmc',side_effect=RuntimeError()),patch.object(sources,'fetch_pubmed',return_value=([article()],{'status':'ok','fetched':1,'available':1})):
            rows,status=m.collect(cfg,'2026-10-26','2026-10-28',False)
        self.assertEqual(len(rows),1)
        self.assertEqual(status['PubMed / 四方向']['status'],'ok')
        self.assertTrue(all(s['status']=='failed' for k,s in status.items() if k.startswith('Europe')))


class DeliveryTests(unittest.TestCase):
    def test_definite_failure_later_retry_and_no_duplicate_after_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(delivery,'send',side_effect=delivery.DeliveryError('rejected')):
                with self.assertRaises(delivery.DeliveryError):delivery.deliver([report('2026-10-27')],root,'owner/repo','SCTtest')
            self.assertEqual(m.json_read(root/'state/delivery_attempts.json',{})['2026-10-27']['status'],'failed')
            with patch.object(delivery,'send') as send:
                self.assertEqual(delivery.deliver([report('2026-10-27'),report('2026-10-28')],root,'owner/repo','SCTtest'),2)
                self.assertIn('2026-10-27',send.call_args.args[2])
                self.assertEqual(delivery.deliver([report('2026-10-27'),report('2026-10-28')],root,'owner/repo','SCTtest'),0)
                self.assertEqual(send.call_count,1)

    def test_uncertain_delivery_not_automatically_resent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(delivery,'send',side_effect=delivery.DeliveryError('timeout',uncertain=True)):
                with self.assertRaises(delivery.DeliveryError):delivery.deliver([report('2026-10-28')],root,'o/r','SCTtest')
            with patch.object(delivery,'send') as send:
                with self.assertRaises(delivery.DeliveryError):delivery.deliver([report('2026-10-28')],root,'o/r','SCTtest')
                send.assert_not_called()
                self.assertEqual(delivery.deliver([report('2026-10-28')],root,'o/r','SCTtest',retry_uncertain=True),1)

    def test_old_receipt_schema_migrates_without_resend(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);m.json_write(root/'state/deliveries.json',{'2026-10-28':{'acknowledged':True,'at':'old'}})
            with patch.object(delivery,'send') as send:
                self.assertEqual(delivery.deliver([report('2026-10-28')],root,'o/r','SCTtest'),0)
                send.assert_not_called()

    def test_interrupted_post_requires_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);m.json_write(root/'state/delivery_attempts.json',{'2026-10-28':{'status':'sending'}})
            with patch.object(delivery,'send') as send:
                with self.assertRaises(delivery.DeliveryError):delivery.deliver([report('2026-10-28')],root,'o/r','SCTtest')
                send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
