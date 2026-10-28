import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import monitor as m
import sources_extra as sources
import delivery
from test_monitor import CONFIG
from test_upgrade import report


class StrictRetrievalTests(unittest.TestCase):
    def test_failed_or_partial_source_preserves_report_and_history(self):
        for status in ('failed', 'partial'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                previous = report('2026-10-28')
                m.json_write(root/'reports/2026-10-28.json', previous)
                (root/'reports/2026-10-28.md').write_text('Existing report')
                history = {'papers': {}, 'last_complete_date': '2026-10-27'}
                m.json_write(root/'state/history.json', history)
                paths = [root/'reports/2026-10-28.json', root/'reports/2026-10-28.md', root/'state/history.json']
                before = [path.read_bytes() for path in paths]
                statuses = {'PubMed': {'status': 'ok'}, 'RSS / Nature Aging': {'status': status}}
                with patch.object(m, 'collect', return_value=([], statuses)), patch.object(m, 'ingest') as ingest:
                    with self.assertRaisesRegex(m.SourceError, 'publication and delivery stopped'):
                        m.build(copy.deepcopy(CONFIG), root, '2026-10-28', refresh=True)
                    ingest.assert_not_called()
                self.assertEqual([path.read_bytes() for path in paths], before)
                attempt = m.json_read(root/'state/retrieval_status.json', {})
                self.assertFalse(attempt['complete'])
                self.assertEqual(attempt['phase'], 'failed')
                health = delivery.status_summary(root, '2026-10-28')
                self.assertTrue(health['retrieval_blocked'])
                self.assertEqual(health['source_problems'], ['RSS / Nature Aging'])

    def test_previous_incomplete_report_automatically_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = report('2026-10-28')
            previous['sources']['RSS'] = {'status': 'failed'}
            m.json_write(root/'reports/2026-10-28.json', previous)
            with patch.object(m, 'collect', return_value=([], {'RSS': {'status': 'ok'}})) as collect:
                result = m.build(copy.deepcopy(CONFIG), root, '2026-10-28')
            collect.assert_called_once()
            m.require_complete(result)
            self.assertTrue(m.json_read(root/'state/retrieval_status.json', {})['complete'])

    def test_no_sources_cannot_publish_empty_report(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'collect', return_value=([], {})):
            with self.assertRaises(m.SourceError):
                m.build(copy.deepcopy(CONFIG), Path(tmp), '2026-10-28')
            self.assertFalse((Path(tmp)/'reports/2026-10-28.json').exists())

    def test_interrupted_retrieval_blocks_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            m.json_write(root/'state/retrieval_status.json', {'complete': False, 'sources': {}, 'phase': 'collecting'})
            with patch.object(delivery, 'send') as send:
                with self.assertRaises(m.SourceError):
                    delivery.deliver([report('2026-10-28')], root, 'o/r', 'SCTtest')
                send.assert_not_called()

    def test_incomplete_legacy_report_blocks_entire_delivery_batch(self):
        bad = report('2026-10-27')
        bad['sources']['RSS'] = {'status': 'partial'}
        with tempfile.TemporaryDirectory() as tmp, patch.object(delivery, 'send') as send:
            with self.assertRaises(m.SourceError):
                delivery.deliver([bad, report('2026-10-28')], Path(tmp), 'o/r', 'SCTtest')
            send.assert_not_called()
            self.assertFalse((Path(tmp)/'state/delivery_attempts.json').exists())

    def test_generation_exception_keeps_stop_marker(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'collect', return_value=([], {'RSS': {'status': 'ok'}})), patch.object(m, 'ingest', side_effect=RuntimeError('fixture')):
            root = Path(tmp)
            with self.assertRaises(RuntimeError):
                m.build(copy.deepcopy(CONFIG), root, '2026-10-28')
            with patch.object(delivery, 'send') as send:
                with self.assertRaises(m.SourceError):
                    delivery.deliver([report('2026-10-27')], root, 'o/r', 'SCTtest')
                send.assert_not_called()


class RSSRecoveryTests(unittest.TestCase):
    feed = {'name': 'Nature Aging', 'url': 'https://www.nature.com/nataging.rss'}

    def test_invalid_xml_retried_then_recovers_with_diagnostics(self):
        client = MagicMock()
        client.read_feed.side_effect = [b'<html>verification', b'<rss><channel/></rss>']
        with patch.object(sources.time, 'sleep'):
            rows, status = sources.fetch_rss(client, self.feed, '2026-10-26', '2026-10-28')
        self.assertEqual(rows, [])
        self.assertEqual(status['status'], 'ok')
        self.assertEqual([a['result'] for a in status['attempts']], ['failed', 'ok'])
        self.assertEqual(client.read_feed.call_count, 2)

    def test_valid_html_cannot_silently_become_zero_papers(self):
        client = MagicMock()
        client.read_feed.return_value = b'<html><body>Cookie verification</body></html>'
        with patch.object(sources.time, 'sleep'):
            rows, status = sources.fetch_rss(client, self.feed, '2026-10-26', '2026-10-28')
        self.assertEqual(status['status'], 'failed')
        self.assertEqual(client.read_feed.call_count, 3)
        self.assertIn('not an RSS', status['attempts'][-1]['error'])

    def test_client_challenge_requires_real_rss_from_browser_before_success(self):
        client = MagicMock()
        def challenge(url, info):
            info['page_title'] = 'Client Challenge'
            return b'<html>javascript'
        client.read_feed.side_effect = challenge
        client.read_browser_feed.return_value = b'<rss><channel/></rss>'
        with patch.object(sources.time, 'sleep'):
            _, status = sources.fetch_rss(client, self.feed, '2026-10-26', '2026-10-28')
        self.assertEqual(status['status'], 'ok')
        self.assertEqual(len(status['attempts']), 4)
        self.assertEqual(status['attempts'][-1]['transport'], 'chromium')
        self.assertEqual(status['attempts'][-1]['result'], 'ok')

    def test_browser_challenge_failure_is_not_ignored(self):
        client = MagicMock()
        def challenge(url, info):
            info['page_title'] = 'Client Challenge'
            return b'<html>javascript'
        client.read_feed.side_effect = challenge
        client.read_browser_feed.side_effect = m.SourceError('Client Challenge unresolved')
        with patch.object(sources.time, 'sleep'):
            _, status = sources.fetch_rss(client, self.feed, '2026-10-26', '2026-10-28')
        self.assertEqual(status['status'], 'failed')
        with self.assertRaises(m.SourceError):
            m.require_complete({'sources': {'RSS': status}})

    def test_response_diagnostics_strip_query_and_do_not_store_cookie_or_body(self):
        response = MagicMock()
        response.status = 200
        response.url = 'https://www.nature.com/nataging.rss?code=PRIVATE&error=cookies'
        response.headers = {'Content-Type': 'text/html', 'Set-Cookie': 'PRIVATE'}
        response.read.return_value = b'<html><body>PRIVATE</body></html>'
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        info = {}
        m.Client().read(self.feed['url'], opener=opener, diagnostics=info)
        self.assertTrue(info['looks_like_html'])
        self.assertNotIn('PRIVATE', json.dumps(info))
        self.assertEqual(info['final_url'], self.feed['url'])


if __name__ == '__main__':
    unittest.main()
