import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
import monitor as m
import title_translation as tr
from test_monitor import CONFIG, article
from test_upgrade import candidate, report


class ChineseReportTests(unittest.TestCase):
    def test_all_top_tiers_without_category_or_pool_cap(self):
        rows = [candidate(i, ['mitochondria'], score=28) for i in range(110)]
        rows += [candidate(i, ['aging'], preprint=True, score=16) for i in range(55)]
        rows += [candidate(900, ['aging'], score=15, venue='Nature')]
        selected = m.choose(rows, CONFIG)
        self.assertEqual(len(selected), 165)
        self.assertTrue(all(p['tier'] in ('必读候选', '值得一读') for p in selected))

    def test_cross_topic_single_display_and_if_order(self):
        rows = [candidate(1, ['aging', 'lipids'], venue='Unknown', score=80),
                candidate(2, ['aging', 'lipids'], venue='Nature', score=16),
                candidate(3, ['aging', 'lipids'], venue='Nature Communications', score=40)]
        selected = m.choose(rows, CONFIG)
        self.assertEqual([p['key'] for p in selected], ['False:2', 'False:3', 'False:1'])
        self.assertTrue(all(p['feed'] == 'interdisciplinary' for p in selected))

    def test_compact_chinese_title_journal_and_direct_link(self):
        p = candidate(1, ['aging'], venue='Nature', score=28)
        p['title_zh'] = '衰老机制研究'
        text = m.render(report('2026-10-28', [p]), CONFIG)
        self.assertIn('**[衰老机制研究](https://doi.org/10.1234/test)**', text)
        self.assertIn('Nature · **必读候选**', text)
        self.assertNotIn('文献详情', text)
        self.assertNotIn(p['abstract'], text)
        self.assertNotIn('/25', text)
        self.assertNotIn('#paper-', text)

    def test_missing_translation_cannot_render(self):
        with self.assertRaises(m.SourceError):
            m.render(report('2026-10-28', [candidate(1, ['aging'])]), CONFIG)

    def test_translation_cache_reuses_only_exact_source_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)/'cache.json'
            rows = [{'title': 'Mitochondrial dynamics'}]
            with patch.object(tr, 'translate_batch', return_value=['线粒体动力学']) as translate:
                tr.translate_titles(rows, cache)
                tr.translate_titles(rows, cache)
                self.assertEqual(translate.call_count, 1)
            rows[0]['title'] = 'Mitochondrial genetics'
            with patch.object(tr, 'translate_batch', return_value=['线粒体遗传学']) as translate:
                tr.translate_titles(rows, cache)
                translate.assert_called_once()

    def test_failed_translation_preserves_report_history_and_blocks_delivery(self):
        for translated in ([], ['English only']):
            with self.subTest(translated=translated), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                previous = report('2026-10-28')
                m.json_write(root/'reports/2026-10-28.json', previous)
                (root/'reports/2026-10-28.md').write_text('Existing')
                m.json_write(root/'state/history.json', {'papers': {}})
                paths = [root/'reports/2026-10-28.json', root/'reports/2026-10-28.md', root/'state/history.json']
                before = [p.read_bytes() for p in paths]
                with patch.object(m, 'collect', return_value=([article()], {'RSS': {'status': 'ok'}})), patch.object(tr, 'translate_batch', return_value=translated):
                    with self.assertRaises(RuntimeError):
                        m.build(CONFIG, root, '2026-10-28', refresh=True)
                self.assertEqual(before, [p.read_bytes() for p in paths])
                self.assertFalse(m.json_read(root/'state/retrieval_status.json', {})['complete'])

    def test_refresh_restores_earlier_same_day_selected_and_excludes_prior_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {}
            m.ingest([article(doi='10.1234/yesterday', ids=[])], state, CONFIG, '2026-10-27')
            m.ingest([article(doi='10.1234/today', ids=[])], state, CONFIG, '2026-10-28')
            # Simulate migration from old history lacking announcement dates.
            for item in state['papers'].values(): item.pop('announced_dates', None)
            m.json_write(root/'state/history.json', state)
            m.json_write(root/'reports/2026-10-28.json', report('2026-10-28'))
            with patch.object(m, 'collect', return_value=([], {'RSS': {'status': 'ok'}})), patch.object(tr, 'translate_batch', return_value=['线粒体动力学调节代谢']):
                result = m.build(CONFIG, root, '2026-10-28', refresh=True)
            self.assertEqual([p['doi'] for p in result['papers']], ['10.1234/today'])
            with patch.object(m, 'collect', return_value=([], {'RSS': {'status': 'ok'}})):
                result = m.build(CONFIG, root, '2026-10-28', refresh=True)
            self.assertEqual(len(result['papers']), 1)

    def test_model_checksum_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/tr.MODEL_NAME).write_bytes(b'corrupt model')
            with patch.dict(tr.os.environ, {'TITLE_MODEL_CACHE': tmp}), patch.object(tr.urllib.request, 'urlopen', side_effect=OSError('fixture')), patch.object(tr.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, 'publication stopped'):
                    tr.ensure_model()

    def test_missing_gene_identifier_is_retried_and_never_silently_accepted(self):
        model = MagicMock()
        model.tokenize.return_value = [1, 2]
        model.side_effect = [
            {'choices': [{'finish_reason': 'stop', 'text': '线粒体自噬的调节'}]},
            {'choices': [{'finish_reason': 'stop', 'text': 'PHB2介导的线粒体自噬调节'}]},
        ]
        with patch.dict('sys.modules', {'llama_cpp': SimpleNamespace(Llama=MagicMock(return_value=model))}), patch.object(tr, 'ensure_model', return_value=Path('fixture')):
            result = tr.translate_batch(['PHB2 regulates mitophagy'])
        self.assertEqual(result, ['PHB2介导的线粒体自噬调节'])
        self.assertEqual(model.call_count, 2)
        model.close.assert_called_once()

    def test_title_prompt_supplies_species_and_lipid_terminology(self):
        text = tr.translation_prompt('Phosphoinositides control Caenorhabditis elegans lifespan')
        self.assertIn('磷脂酰肌醇类', text)
        self.assertIn('秀丽隐杆线虫', text)
        self.assertIn('保留基因和蛋白名称及缩写', text)

    def test_render_sorts_each_feed_even_if_input_unsorted(self):
        rows = [candidate(1, ['aging'], score=80), candidate(2, ['aging'], venue='Nature', score=16)]
        rows[0]['title_zh'] = '未知期刊文章'
        rows[1]['title_zh'] = '已核验高影响因子文章'
        text = m.render(report('2026-10-28', rows), CONFIG)
        self.assertLess(text.index('**[已核验'), text.index('**[未知'))

    def test_push_has_chinese_journal_and_no_old_quota_claim(self):
        p = candidate(1, ['aging'], venue='Nature');p['title_zh'] = '衰老机制研究'
        _, text = m.push_payload(report('2026-10-28', [p]), 'o/r')
        self.assertIn('衰老机制研究', text)
        self.assertIn('Nature', text)
        self.assertNotIn('/25', text)
        self.assertNotIn('原文摘要', text)


if __name__ == '__main__': unittest.main()
