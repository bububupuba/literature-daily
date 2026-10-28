#!/usr/bin/env python3
"""Daily literature feeds. Python 3.11+, standard library, no model API."""
import argparse
import copy
import datetime as dt
import hashlib
import html
import http.cookiejar
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
EPMC = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
BIO = 'https://api.biorxiv.org'
CROSSREF = 'https://api.crossref.org/works'


class SourceError(RuntimeError):
    pass


def source_problems(statuses):
    return [name for name, status in statuses.items() if status.get('status') != 'ok'] or ([] if statuses else ['未执行任何数据源检索'])


def require_complete(report):
    problems = source_problems(report.get('sources', {}))
    if report.get('complete') is False and not problems:
        problems = ['检索或日报生成尚未完成']
    if problems:
        raise SourceError('Retrieval incomplete; publication and delivery stopped: ' + ', '.join(problems))


def clean(value):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html.unescape(str(value or '')))).strip()


def doi(value):
    value = urllib.parse.unquote(str(value or '')).strip().lower()
    value = re.sub(r'^(https?://(dx\.)?doi.org/|doi:\s*)', '', value)
    return value if re.match(r'^10\.\d{4,9}/\S+$', value) else ''


def root_doi(value):
    # Research Square registers a separate DOI for each version.
    return re.sub(r'/v\d+$', '', value) if value.startswith('10.21203/') else value


def number(value):
    found = re.search(r'\d+', str(value or ''))
    return int(found.group()) if found else 0


def day(value):
    try:
        return dt.date.fromisoformat(str(value)[:10]).isoformat()
    except (ValueError, TypeError):
        return ''


def json_read(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def json_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def load_config():
    config = json_read(ROOT / 'config.json', {})
    config['journal_metrics'] = json_read(ROOT / 'journal_metrics.json', {})
    return config


class Client:
    def __init__(self, timeout=35):
        self.timeout = timeout
        self.crossref_lock = threading.Lock()
        self.crossref_last_request = 0.0
        self.crossref_next_allowed = 0.0
        # Publisher RSS redirects can require an anonymous session cookie.
        self.rss_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.rss_lock = threading.Lock()
        self.rss_last_request = 0.0

    def read(self, url, params=None, opener=None, diagnostics=None):
        if params:
            url += '?' + urllib.parse.urlencode(params)
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={'User-Agent': 'LiteratureDaily/2.0 (https://github.com/bububupuba/literature-daily)', 'Accept': '*/*'})
                crossref = urllib.parse.urlparse(url).hostname == 'api.crossref.org'
                with self.crossref_lock if crossref else nullcontext():
                    if crossref:
                        delay = max(self.crossref_last_request + 1.2, self.crossref_next_allowed) - time.monotonic()
                        if delay > 60:
                            raise SourceError('Crossref cooldown exceeds one minute; deferred')
                        if delay > 0:
                            time.sleep(delay)
                        self.crossref_last_request = time.monotonic()
                    open_request = opener.open if opener else urllib.request.urlopen
                    with open_request(request, timeout=self.timeout) as response:
                        data = response.read()
                        if diagnostics is not None:
                            final = urllib.parse.urlsplit(response.url)
                            diagnostics.update(http_status=response.status, content_type=response.headers.get('Content-Type', ''),
                                               content_encoding=response.headers.get('Content-Encoding', ''),
                                               final_url=urllib.parse.urlunsplit((final.scheme, final.netloc, final.path, '', '')),
                                               bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                                               looks_like_html=bool(re.search(br'<(?:!doctype\s+html|html)(?:\s|>)', data[:1000], re.I)))
                        return data
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if attempt == 2:
                    # Never include request URLs in errors: push URLs contain secrets.
                    raise SourceError(f'HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__) from None
                if isinstance(exc, urllib.error.HTTPError) and exc.code in (429, 503):
                    try:
                        pause = float(exc.headers.get('Retry-After', 15))
                    except (ValueError, TypeError, AttributeError):
                        pause = 15
                    pause = max(pause, 2 ** attempt)
                    if urllib.parse.urlparse(url).hostname == 'api.crossref.org':
                        with self.crossref_lock:
                            self.crossref_next_allowed = max(self.crossref_next_allowed, time.monotonic() + pause)
                    if pause > 60:
                        raise SourceError(f'HTTP {exc.code}; long Retry-After deferred') from None
                    time.sleep(pause)
                else:
                    time.sleep(2 ** attempt)

    def read_feed(self, url, diagnostics):
        # Serialize feed traffic and retain cookies across normal publisher redirects.
        with self.rss_lock:
            delay = self.rss_last_request + 1.2 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.rss_last_request = time.monotonic()
            return self.read(url, opener=self.rss_opener, diagnostics=diagnostics)

    def get(self, url, params=None):
        for attempt in range(3):
            try:
                return json.loads(self.read(url, params))
            except (json.JSONDecodeError, UnicodeDecodeError):
                if attempt == 2:
                    raise SourceError('Invalid JSON response') from None
                time.sleep(2 ** attempt)


def matcher(term):
    pattern = re.escape(term.lower()).replace(r'\*', r'\w*').replace(r'\ ', r'[\s-]+')
    return re.compile(r'(?<!\w)' + pattern + r'(?!\w)', re.I)


def matches(text, terms):
    return [term for term in terms if matcher(term).search(text)]


def occurrences(text, terms):
    return sum(len(matcher(term).findall(text)) for term in terms)


def epmc_query(topic, start, end, incremental=False):
    terms = ' OR '.join('TITLE_ABS:' + ('"' + term + '"' if ' ' in term else term) for term in topic['anchors'])
    dates = f'FIRST_PDATE:[{start} TO {end}]'
    if incremental:
        dates = f'({dates} OR FIRST_IDATE:[{start} TO {end}] OR UPDATE_DATE:[{start} TO {end}])'
    return f'({terms}) AND {dates} AND (SRC:MED OR SRC:PMC OR SRC:PPR)'


def epmc_article(raw):
    identifier = doi(raw.get('doi'))
    journal = raw.get('journalInfo', {}).get('journal', {}).get('title', '')
    preprint = raw.get('source') == 'PPR' or 'Preprint' in raw.get('pubTypeList', {}).get('pubType', [])
    links = []
    for link in raw.get('commentCorrectionList', {}).get('commentCorrection', []):
        if str(link.get('type', '')).lower() in ('preprintof', 'preprintin', 'publishedas', 'haspreprint'):
            linked = doi(link.get('doi'))
            if linked:
                links.append(linked)
    return {
        'doi': identifier, 'ids': [f"{raw.get('source')}:{raw.get('id')}"] + ([f"PMID:{raw['pmid']}"] if raw.get('pmid') else []),
        'title': clean(raw.get('title')), 'authors': clean(raw.get('authorString')),
        'venue': journal or ('Research Square' if identifier.startswith('10.21203/') else '预印本平台（数据库未注明）' if preprint else '数据库未注明'),
        'date': day(raw.get('firstPublicationDate')), 'indexed': day(raw.get('firstIndexDate')),
        'abstract': clean(raw.get('abstractText')), 'types': raw.get('pubTypeList', {}).get('pubType', []),
        'preprint': preprint, 'version': number(raw.get('versionNumber')),
        'url': 'https://doi.org/' + identifier if identifier else f"https://europepmc.org/article/{raw.get('source')}/{raw.get('id')}",
        'related': links, 'sources': ['Europe PMC'],
        'retracted': any('retract' in str(c.get('type', '')).lower() for c in raw.get('commentCorrectionList', {}).get('commentCorrection', [])) or any('retracted' in t.lower() for t in raw.get('pubTypeList', {}).get('pubType', [])),
    }


def fetch_epmc(client, topic, start, end, incremental, max_pages):
    query = epmc_query(topic, start, end, incremental)
    articles, cursor, total = [], '*', 0
    for _ in range(max_pages):
        data = client.get(EPMC, {'query': query, 'format': 'json', 'resultType': 'core', 'pageSize': 100, 'cursorMark': cursor})
        if 'hitCount' not in data:
            raise RuntimeError('invalid Europe PMC response')
        batch = data.get('resultList', {}).get('result', [])
        total = int(data['hitCount'])
        articles.extend(epmc_article(r) for r in batch)
        nxt = data.get('nextCursorMark')
        if len(articles) >= total:
            return articles, {'status': 'ok', 'fetched': len(articles), 'available': total, 'query': query}
        if not batch or not nxt or nxt == cursor:
            break
        cursor = nxt
    return articles, {'status': 'partial', 'fetched': len(articles), 'available': total, 'query': query}


def biorxiv_article(raw, server):
    identifier = doi(raw.get('doi'))
    return {
        'doi': identifier, 'ids': [], 'title': clean(raw.get('title')), 'authors': clean(raw.get('authors')),
        'venue': 'bioRxiv' if server == 'biorxiv' else 'medRxiv', 'date': day(raw.get('date')), 'indexed': '',
        'date_kind': 'version_posted',
        'abstract': clean(raw.get('abstract')), 'types': [raw.get('type', 'preprint')], 'preprint': True,
        'version': number(raw.get('version')), 'url': 'https://doi.org/' + identifier,
        'related': [doi(raw['published'])] if doi(raw.get('published')) else [],
        'sources': [server], 'retracted': 'withdraw' in raw.get('title', '').lower(),
    }


def fetch_biorxiv(client, server, start, end, max_pages):
    articles, cursor, total = [], 0, 0
    for _ in range(max_pages):
        data = client.get(f'{BIO}/details/{server}/{start}/{end}/{cursor}')
        message = data.get('messages', [{}])[0]
        if message.get('status') not in ('ok', 'no posts found'):
            raise RuntimeError('invalid preprint response')
        total = int(message.get('total', 0))
        batch = data.get('collection', [])
        articles.extend(biorxiv_article(r, server) for r in batch)
        cursor += len(batch)
        if cursor >= total:
            return articles, {'status': 'ok', 'fetched': cursor, 'available': total}
        if not batch:
            break
    return articles, {'status': 'partial', 'fetched': cursor, 'available': total}


def crossref_date(raw, keys):
    for key in keys:
        parts = raw.get(key, {}).get('date-parts', [[]])[0]
        if len(parts) == 3:
            return day(f'{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}')
    return ''  # A year/month alone cannot establish a daily publication date.


def crossref_article(raw):
    identifier = doi(raw.get('DOI'))
    links = []
    for kind in ('is-preprint-of', 'has-preprint', 'is-version-of', 'has-version'):
        for link in raw.get('relation', {}).get(kind, []):
            linked = doi(link.get('id'))
            if linked:
                links.append(linked)
    return {
        'doi': identifier, 'ids': [], 'title': clean(' '.join(raw.get('title', []))),
        'authors': '; '.join(clean(a.get('given', '') + ' ' + a.get('family', a.get('name', ''))) for a in raw.get('author', [])),
        'venue': 'Research Square' if identifier.startswith('10.21203/') else clean(' '.join(raw.get('container-title', []))),
        'date': crossref_date(raw, ('posted', 'published-online', 'published', 'issued')),
        'indexed': day(raw.get('created', {}).get('date-time')),
        'abstract': clean(raw.get('abstract')), 'types': [raw.get('type', '')],
        'preprint': raw.get('type') == 'posted-content', 'version': number(identifier.rsplit('/v', 1)[-1]) if '/v' in identifier else 0,
        'url': 'https://doi.org/' + identifier, 'related': links, 'sources': ['Crossref'],
        'retracted': any('retract' in str(item.get('type', '')).lower() for item in raw.get('update-to', [])),
    }


def fetch_research_square(client, start, end, incremental, max_pages):
    date_filter = 'update' if incremental else 'posted'
    filters = f'prefix:10.21203,type:posted-content,from-{date_filter}-date:{start},until-{date_filter}-date:{end}'
    cursor, articles, total = '*', [], 0
    for _ in range(max_pages):
        data = client.get(CROSSREF, {'filter': filters, 'rows': 100, 'cursor': cursor})['message']
        total = int(data['total-results'])
        batch = data.get('items', [])
        articles.extend(crossref_article(r) for r in batch)
        if len(articles) >= total:
            return articles, {'status': 'ok', 'fetched': len(articles), 'available': total}
        nxt = data.get('next-cursor')
        if not batch or not nxt or nxt == cursor:
            break
        cursor = nxt
    return articles, {'status': 'partial', 'fetched': len(articles), 'available': total}


def classify(article, config):
    title, abstract = article['title'], article['abstract']
    text = title + ' ' + abstract
    if article.get('retracted') or re.match(r'(?i)^(retraction|withdrawn|retracted|correction|erratum)\b', title):
        return None, '撤稿、撤回或更正记录'
    if article.get('doi', '').startswith('10.1038/d41586'):
        return None, 'Nature 新闻或评论记录'
    types = ' '.join(article.get('types', [])).lower()
    if re.search(r'\b(editorial|news|comment|letter|case reports?)\b', types) and not re.search(r'review|research|journal article', types):
        return None, '无实质研究或综述的文章类型'
    if len(abstract.split()) < 35:
        return None, '摘要不足，无法可靠筛选'
    biological = bool(re.search(r'(?i)\b(cell\w*|gene\w*|protein\w*|molecul\w*|biolog\w*|organism\w*|mice|mouse|human\w*|animal\w*|worm\w*|caenorhabditis|tissue\w*|metabol\w*|enzyme\w*|neuron\w*|membrane\w*|bacter\w*|yeast|lifespan|healthspan)\b', text))
    if not biological:
        return None, '未找到生物学背景'
    if re.search(r'(?i)\b(workforce|retirement|job insecurity|organizational behavior|petroleum|lubricant|asphalt|concrete aging|battery aging)\b', title):
        return None, '社会或非生物材料主题'
    topics, reasons, relevance = [], {}, 0
    for key, topic in config['topics'].items():
        title_hits = matches(title, topic['anchors'])
        abstract_hits = matches(abstract, topic['anchors'])
        # C. elegans must be explicitly identified; broad terms like RNAi do not imply species.
        include = bool(title_hits or (abstract_hits and (key == 'celegans' or occurrences(abstract, topic['anchors']) >= 2)))
        if not include:
            continue
        topics.append(key)
        keywords = matches(text, topic['keywords'])
        reasons[key] = {'title_terms': title_hits, 'abstract_terms': abstract_hits, 'supporting_terms': keywords[:8]}
        relevance += min(24, len(title_hits) * 8 + len(abstract_hits) * 3 + min(len(keywords), 6))
    if not topics:
        return None, '主题仅偶然提及或未明确匹配'
    clinical = bool(re.search(r'(?i)\b(case report|clinical trial|randomized controlled trial)\b', text + ' ' + types))
    mechanism_terms = ['mechanism*', 'knockout', 'knockdown', 'mutant*', 'RNAi', 'CRISPR', 'epistasis', 'rescue', 'binding', 'enzyme*', 'signaling']
    mechanism_hits = matches(text, mechanism_terms)
    if clinical and not mechanism_hits:
        return None, '临床研究缺乏分子或细胞机制线索'
    observational = re.search(r'(?i)\b(cross-sectional|observational study|cohort study|prospective cohort|retrospective cohort|case report)\b', text)
    molecular_evidence = matches(text, ['knockout', 'knockdown', 'mutant*', 'RNAi', 'CRISPR', 'epistasis', 'transcriptom*', 'proteom*', 'lipidomic*', 'genom*', 'mendelian randomization', 'cell culture', 'in vitro', 'mouse', 'mice'])
    if observational and not molecular_evidence:
        return None, '纯临床或人群关联，缺乏基础分子研究证据线索'
    method_hits = matches(text, ['genetic screen*', 'genome-wide', 'single-cell', 'lipidomics', 'proteomics', 'cryo-EM', 'method*', 'protocol*', 'resource', 'atlas'])
    discovery_hits = matches(text, ['previously unknown', 'previously unrecognized', 'new mechanism', 'novel gene', 'unexpected', 'challenge*', 'uncover*'])
    # These are reading-priority indicators, not ratings of validity or novelty.
    score = relevance + min(12, 3 * len(mechanism_hits)) + min(8, 2 * len(method_hits)) + min(6, 2 * len(discovery_hits)) + (10 if len(topics) > 1 else 0)
    paper = dict(article, topics=topics, reasons=reasons, score=score, signals={'mechanism': mechanism_hits, 'method': method_hits, 'discovery_claim': discovery_hits}, preferred=article['venue'].casefold() in {j.casefold() for j in config['preferred_journals']})
    return decorate(paper, config), ''


def journal_key(value):
    value = clean(value).casefold()
    value = value.replace('the ', '', 1) if value.startswith('the ') else value
    value = re.sub(r'\([^)]*\)', '', value)
    return re.sub(r'[^a-z0-9]', '', value).removesuffix('cb')


def decorate(paper, config):
    paper = dict(paper)
    metrics = config.get('journal_metrics', {})
    metric = next((entry for entry in metrics.values() if journal_key(paper['venue']) in {journal_key(name) for name in [entry['journal']] + entry.get('aliases', [])}), None)
    paper['jif'] = metric if metric and not paper['preprint'] else None
    thresholds = config.get('tiers', {'must_read': 28, 'recommended': 16})
    paper['tier'] = '必读候选' if paper['score'] >= thresholds['must_read'] else '值得一读' if paper['score'] >= thresholds['recommended'] else '其他相关'
    return paper


def aliases(article):
    result = set(article.get('ids', []))
    for value in [article.get('doi', '')] + article.get('related', []):
        if value:
            result.add('doi:' + root_doi(value))
    if not result:
        result.add('url:' + article['url'])
    return result


def consolidate(articles):
    parents = list(range(len(articles)))
    owners = {}
    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index
    # Index persistent identifiers instead of comparing every record with every group.
    for index, article in enumerate(articles):
        for identifier in aliases(article):
            if identifier in owners:
                parents[find(index)] = find(owners[identifier])
            owners[identifier] = index
    grouped = {}
    for index, article in enumerate(articles):
        group = grouped.setdefault(find(index), {'aliases': set(), 'articles': []})
        group['aliases'].update(aliases(article))
        group['articles'].append(article)
    groups = grouped.values()
    output = []
    for group in groups:
        # The journal version takes precedence; otherwise keep latest preprint version.
        members = sorted(group['articles'], key=lambda a: (not a['preprint'], a['version'], a['date'], len(a['abstract'])), reverse=True)
        paper = copy.deepcopy(members[0])
        paper['aliases'] = sorted(group['aliases'])
        paper['sources'] = sorted({source for a in members for source in a['sources']})
        paper['related'] = sorted({value for a in members for value in [a['doi']] + a['related'] if value and value != paper['doi']})
        paper['retracted'] = any(a.get('retracted') for a in members)
        same = [a for a in members if a['doi'] == paper['doi'] and a['preprint'] == paper['preprint']]
        dated = [a['date'] for a in same if a['date']]
        if dated and not paper['preprint']:
            paper['date'] = min(dated)  # Online date must not be replaced by a later issue date.
        elif paper['preprint']:
            posted = [a['date'] for a in same if a.get('date_kind') == 'version_posted' and a['version'] == paper['version'] and a['date']]
            if posted:
                paper['date'] = max(posted)  # Europe PMC may retain v1's original publication date.
        # Only supplement abstracts from the same DOI and same version.
        for a in members:
            if a['doi'] == paper['doi'] and a['version'] == paper['version'] and len(a['abstract']) > len(paper['abstract']):
                paper['abstract'] = a['abstract']
            if a['doi'] == paper['doi']:
                for field in ('authors', 'venue'):
                    if not paper[field] or paper[field] == '数据库未注明':
                        paper[field] = a[field]
        output.append(paper)
    return output


def fingerprint(paper):
    # Bibliographic corrections and indexing changes are not substantive updates.
    return f"{paper['doi'] or paper['url']}|{'preprint' if paper['preprint'] else 'journal'}|{paper['version'] if paper['preprint'] else 0}"


def rank(paper):
    metric = paper.get('jif')
    impact = float(metric['value']) if metric and not paper['preprint'] else -1
    return (-impact, -paper['score'], -int(paper['preferred']), paper['title'].casefold())


def choose(papers, config):
    if 'pool_limits' in config:
        selected = []
        for preprint, pool in ((False, 'journals'), (True, 'preprints')):
            selected.extend(allocate([decorate(p, config) for p in papers if p['preprint'] == preprint], config['pool_limits'][pool], config))
        return selected
    ordered = sorted(papers, key=rank)
    chosen = []
    chosen_ids = set()
    def add(p):
        if p['key'] not in chosen_ids and len(chosen) < config['max_papers']:
            chosen.append(p)
            chosen_ids.add(p['key'])
    # Protect each independent direction from being crowded out by a larger field.
    for index in range(config.get('reserved_per_feed', 15)):
        for topic in config['topics']:
            candidates = [p for p in ordered if topic in p['topics']]
            if index < len(candidates):
                add(candidates[index])
    for p in ordered:
        add(p)
    return sorted(chosen, key=rank)


def allocate(papers, limit, config):
    """Unique papers fill five quotas; spare slots are redistributed within this pool."""
    ordered = sorted(papers, key=rank)
    categories = ['interdisciplinary'] + list(config['topics'])
    slots = [category for category in categories for _ in range(config.get('quota_per_feed', 5))]
    assigned = {}
    def eligible(p, category):
        return len(p['topics']) >= 2 if category == 'interdisciplinary' else category in p['topics']
    def place(p, visited):
        for index, category in enumerate(slots):
            if index in visited or not eligible(p, category):
                continue
            visited.add(index)
            previous = assigned.get(index)
            if previous is None or place(previous, visited):
                assigned[index] = p
                return True
        return False
    for paper in ordered:
        if len(assigned) >= min(limit, len(slots)):
            break
        place(paper, set())
    result = [dict(p, feed=slots[index], redistributed=False) for index, p in assigned.items()]
    used = {p['key'] for p in result}
    for paper in ordered:
        if len(result) >= limit:
            break
        if paper['key'] in used:
            continue
        counts = {category: sum(p['feed'] == category for p in result) for category in categories}
        possible = [category for category in categories if eligible(paper, category)]
        if not possible:
            continue
        category = min(possible, key=lambda name: (counts[name], categories.index(name)))
        result.append(dict(paper, feed=category, redistributed=True))
        used.add(paper['key'])
    return sorted(result, key=rank)


def ingest(records, state, config, report_date, select=True):
    state.setdefault('papers', {})
    initial_start = state.setdefault('initial_start', (dt.date.fromisoformat(report_date) - dt.timedelta(days=config['initial_days'] - 1)).isoformat())
    rejected = {}
    lookup = {alias: key for key, item in state['papers'].items() for alias in item['aliases']}
    for paper in consolidate(records):
        old_keys = sorted({lookup[a] for a in paper['aliases'] if a in lookup})
        key = old_keys[0] if old_keys else hashlib.sha256('|'.join(paper['aliases']).encode()).hexdigest()[:24]
        old = state['papers'].get(key)
        # Merge histories if a later data source establishes an explicit relationship.
        histories = [state['papers'][k] for k in old_keys]
        announced = sorted({fp for item in histories for fp in item.get('announced', [])})
        merged_aliases = sorted(set(paper['aliases']).union(*(set(item['aliases']) for item in histories)))
        dates = [item['first_seen'] for item in histories]
        if paper['date'] and paper['date'] > report_date:
            rejected['未来出版日期，等待实际日期或在线日期'] = rejected.get('未来出版日期，等待实际日期或在线日期', 0) + 1
            continue
        if not old and (not paper['date'] or paper['date'] < initial_start):
            rejected['首次回溯窗口之前或日期不完整'] = rejected.get('首次回溯窗口之前或日期不完整', 0) + 1
            continue
        evaluated, reason = classify(paper, config)
        if not evaluated:
            rejected[reason] = rejected.get(reason, 0) + 1
            if paper.get('retracted'):
                for k in old_keys:
                    state['papers'][k]['suppressed'] = True
            continue
        if old and not old['article']['preprint'] and paper['preprint']:
            evaluated = old['article']
        elif old and paper['preprint'] and old['article']['preprint'] and old['article']['version'] > paper['version']:
            evaluated = old['article']
        for extra in old_keys[1:]:
            del state['papers'][extra]
        state['papers'][key] = {'aliases': merged_aliases, 'article': evaluated, 'first_seen': min(dates) if dates else report_date, 'last_seen': report_date, 'announced': announced, 'suppressed': False}
        for alias in merged_aliases:
            lookup[alias] = key
    pending = []
    for key, item in state['papers'].items():
        if item.get('suppressed'):
            continue
        p = item['article']
        if fingerprint(p) in item['announced']:
            continue
        label = '新收录'
        if item['announced']:
            label = '预印本版本更新' if p['preprint'] else '预印本正式发表／关联版本更新'
        elif p['preprint'] and p['version'] > 1:
            label = '新收录的预印本修订版'
        if item['first_seen'] < report_date and not item['announced']:
            label += '（此前超过每日上限，顺延）'
        pending.append(dict(p, key=key, event=label))
    selected = choose(pending, config) if select else []
    for p in selected:
        state['papers'][p['key']]['announced'].append(fingerprint(p))
    return selected, len(pending) - len(selected), rejected


def md(value):
    return re.sub(r'([\\`*_{}\[\]<>#|])', r'\\\1', clean(value))


def render(report, config):
    papers = [decorate(p, config) for p in report['papers']]
    counts = {'journals': sum(not p['preprint'] for p in papers), 'preprints': sum(p['preprint'] for p in papers)}
    limits = config.get('pool_limits', {'journals': 25, 'preprints': 25})
    names = dict(interdisciplinary='交叉研究', **{key: spec['name'] for key, spec in config['topics'].items()})
    lines = [f"# 文献日报 · {report['date']}", '',
        f"期刊论文 **{counts['journals']}/{limits['journals']}** 篇；预印本 **{counts['preprints']}/{limits['preprints']}** 篇；合计 **{len(papers)}** 篇独立文献，另有 {report['deferred']} 篇候选顺延。", '',
        '四方向与交叉研究各预分配 5 个名额，缺额在同组内转给其他栏目；每篇仅占一个名额。期刊组与预印本组分别分配，互不占用名额。', '',
        '**排序：** 相关性筛选合格后，期刊论文优先按已核验的期刊影响因子降序；未知指标不估算，预印本不赋影响因子。各栏目内预印本按相关性、方法和机制关键词排序。指标年份及来源逐篇列出，已核验指标覆盖仍不完整。', '',
        '**阅读分级：** 必读候选／值得一读／其他相关。分级取决于主题与实验方法关键词规则，不是科学质量评审，也不因影响因子高就自动评为必读。', '',
        '**无大模型模式：** 中文栏目与匹配理由；标题、作者和摘要保留原文。摘要为作者表述，可能混合实验描述、解释与假设，尚未自动证据分层。', '',
        f"检索窗口：{report['start']} 至 {report['date']}（北京时间）。期刊 RSS 为滚动条目，数据库可能延迟收录。", '',
        '## 数据源状态', '']
    labels = {'ok': '完成', 'partial': '覆盖不完整', 'failed': '失败'}
    for source, status in report['sources'].items():
        suffix = f" 错误：{status['error']}" if status.get('error') else ''
        if status.get('scope'):
            suffix += ' ' + status['scope']
        lines.append(f"- {source}：{labels[status['status']]}；获取 {status.get('fetched', 0)} / {status.get('available', '未知')} 条。{suffix}")
    if any(s['status'] != 'ok' for s in report['sources'].values()):
        failure = '所有来源检索失败' if all(s['status'] == 'failed' for s in report['sources'].values()) else '部分来源未完成'
        lines += ['', f'**{failure}，存在检索缺口。不能把零结果理解为没有新论文，也不能把备用来源成功理解为缺失来源已完全覆盖。**']
    elif not papers:
        lines += ['', '在已查询数据源与当前规则下，本次没有新增或更新的合格文献。']
    categories = list(config['topics']) + ['interdisciplinary']
    for preprint, label, pool in ((False, '期刊论文', 'journals'), (True, '预印本（未经同行评审）', 'preprints')):
        lines += ['', f"## {label} · {counts[pool]}/{limits[pool]} 篇", '', '| 栏目 | 基础名额 | 实际入选 |', '|---|---:|---:|']
        subset = [(i, p) for i, p in enumerate(papers, 1) if p['preprint'] == preprint]
        for key in categories:
            n = sum(p.get('feed', p['topics'][0]) == key for _, p in subset)
            lines.append(f"| {names[key]} | 5 | {n} |")
        for key in categories:
            lines += ['', f"### {names[key]}", '']
            group = [(i, p) for i, p in subset if p.get('feed', p['topics'][0]) == key]
            for i, p in group:
                metric = p.get('jif')
                impact = f"IF {metric['value']:g}（{metric['year'] or '官网未注明年份'}）" if metric else '预印本，无 IF' if preprint else 'IF 未核验'
                lines.append(f"- [#{i} {md(p['title'])}](#paper-{i}) — **{p['tier']}** · {impact}")
            if not group:
                lines.append('本栏无合格新增候选，空缺名额已用于其他栏目；全组不足时不凑数。')
    lines += ['', '## 文献详情', '']
    for i, p in enumerate(papers, 1):
        status = f"**预印本：未经同行评审 · v{p['version'] or '未知'}**" if p['preprint'] else '期刊发表版本（同行评审过程未单独核验）'
        metric = p.get('jif')
        impact = f"[{metric['value']:g}（{metric['year'] or '官网未注明年份'}）]({metric['source']})；核验日期 {metric['verified_at']}" if metric else '不适用（预印本）' if p['preprint'] else '未核验，不以猜测值参与排序'
        lines += [f'<a id="paper-{i}"></a>', f"### {i}. {md(p['title'])}", '',
            f"- 作者：{md(p['authors']) or '数据库未提供'}", f"- 来源：{md(p['venue'])} · 日期：{p['date'] or '未提供'}",
            f"- 状态：{status}；{p.get('event', '新收录')}", f"- 原文：[{md(p['doi']) or '原始页面'}]({p['url']})",
            f"- 期刊影响因子：{impact}", f"- 阅读分级：**{p['tier']}**；相关性规则分 {p['score']}（不是质量分）",
            '- 关联方向：' + '、'.join(config['topics'][t]['name'] for t in p['topics']),
            '- 名额归属：' + names.get(p.get('feed', p['topics'][0]), '') + ('（调剂名额）' if p.get('redistributed') else '')]
        for topic in p['topics']:
            reason = p['reasons'][topic]
            terms = reason['title_terms'] or reason['abstract_terms']
            lines.append(f"- 与{config['topics'][topic]['name']}相关的依据：{'标题' if reason['title_terms'] else '摘要'}匹配 {md('；'.join(terms))}。")
        if p['related']:
            lines.append('- 关联版本：' + '；'.join(f'[{md(d)}](https://doi.org/{d})' for d in p['related']))
        lines += ['', '**原文摘要（作者表述，未经自动证据分层）**', '', '> ' + md(p['abstract']), '',
            '**科学意义、实验可靠性与推测边界：** 待阅读全文判断，规则分级与期刊影响因子都不替代逐篇审阅。', '']
    lines += ['## 范围与局限', '',
        '- 不设期刊白名单；期刊论文通过 Europe PMC、PubMed、Crossref 和已配置的期刊 RSS 补充获取。各源覆盖和时间窗不同，不能保证零遗漏。',
        '- DOI、PMID、数据库编号与明确登记的版本关系用于去重。没有登记关系的预印本转期刊可能漏识别；检测到关联后优先保留期刊版本。',
        '- 缺少足够摘要、完整日期或生物学背景的记录暂不推荐，过滤统计保存在同日 JSON。',
        '- 影响因子来自可核验来源且标注年份；未核验期刊仍可入选，排序靠后。不同年份的数值可比性有限，不把期刊影响因子称为论文影响因子。',
        '- 预印本版本号变化会提示更新；无新版本号的内容修订与未及时登记的撤稿仍可能漏识别。',
        '- 报告生成和微信投递分别保存状态：未送达报告在补发时汇总链接；已确认送达的日报不自动重发。', '']
    return '\n'.join(lines)

def collect(config, start, end, incremental):
    from sources_extra import fetch_crossref_journals, fetch_pubmed, fetch_rss
    client = Client(config['timeout_seconds'])
    tasks = {}
    limit = config['max_pages_per_source']
    records, statuses = [], {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        if config['sources']['europepmc']:
            for key, spec in config['topics'].items():
                tasks[pool.submit(fetch_epmc, client, spec, start, end, incremental, limit)] = 'Europe PMC / ' + spec['name']
        for server in ('biorxiv', 'medrxiv'):
            if config['sources'][server]:
                tasks[pool.submit(fetch_biorxiv, client, server, start, end, limit)] = server
        if config['sources']['research_square']:
            tasks[pool.submit(fetch_research_square, client, start, end, incremental, limit)] = 'Research Square / Crossref'
        if config['sources'].get('crossref_journals'):
            for key, queries in config['crossref_queries'].items():
                tasks[pool.submit(fetch_crossref_journals, client, queries, key, start, end, config.get('crossref_max_pages', 10))] = 'Crossref期刊 / ' + config['topics'][key]['name']
        if config['sources'].get('pubmed'):
            tasks[pool.submit(fetch_pubmed, client, config['topics'], start, end, incremental, limit)] = 'PubMed / 四方向'
        if config['sources'].get('rss'):
            for feed in config['rss_feeds']:
                tasks[pool.submit(fetch_rss, client, feed, start, end)] = 'RSS / ' + feed['name']
        for future in as_completed(tasks):
            name = tasks[future]
            try:
                found, status = future.result()
                records.extend(found)
                statuses[name] = status
            except Exception as exc:
                statuses[name] = {'status': 'failed', 'error': str(exc) if isinstance(exc, SourceError) else type(exc).__name__, 'fetched': 0}
            print(name, statuses[name]['status'], statuses[name].get('fetched', 0), flush=True)
    return records, statuses


def build(config, directory, today, preview=False, refresh=False):
    state_path = directory / 'state' / 'history.json'
    report_path = directory / 'reports' / (today + '.json')
    # Keep the day's report stable on reruns, including after failed notification.
    previous = json_read(report_path, None) if not preview else None
    latest_attempt = json_read(directory / 'state/retrieval_status.json', {})
    if previous and not refresh and not source_problems(previous.get('sources', {})) and latest_attempt.get('complete', True):
        print('Report already prepared; preserving same-day report and delivery identity.')
        return json_read(report_path, {})
    state = json_read(state_path, {'papers': {}})
    initial = not state.get('last_complete_date')
    date = dt.date.fromisoformat(today)
    start = date - dt.timedelta(days=(config['initial_days'] if initial else config['overlap_days']) - 1)
    if state.get('last_complete_date'):
        start = min(start, dt.date.fromisoformat(state['last_complete_date']) - dt.timedelta(days=2))
    elif state.get('initial_start'):
        start = min(start, dt.date.fromisoformat(state['initial_start']))
    retrieval = {'date': today, 'attempted_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                 'start': start.isoformat(), 'complete': False, 'phase': 'collecting', 'sources': {}}
    # Persist the stop marker before network work, including for interrupted runs.
    json_write(directory / 'state/retrieval_status.json', retrieval)
    records, statuses = collect(config, start.isoformat(), today, not initial)
    retrieval.update(sources=statuses, phase='failed' if source_problems(statuses) else 'generating')
    json_write(directory / 'diagnostics' / ('retrieval-' + today + '.json'), retrieval)
    json_write(directory / 'state/retrieval_status.json', retrieval)
    if preview:
        json_write(directory / 'raw-preview.json', records)
    # Gate BEFORE ingest mutates announcement history or any report is overwritten.
    require_complete({'sources': statuses})
    if not statuses or all(s['status'] == 'failed' for s in statuses.values()):
        selected, deferred, rejected = [], 0, {}
    else:
        selected, deferred, rejected = ingest(records, state, config, today, select=not bool(previous))
        if previous:
            # Rebuild today's selection from the old report plus unannounced candidates.
            # Keep prior announcements in history so removed entries do not reappear tomorrow.
            old_aliases = set().union(*(aliases(p) for p in previous['papers'])) if previous['papers'] else set()
            candidates = []
            for key, item in state['papers'].items():
                if item.get('suppressed'):
                    continue
                p = item['article']
                if fingerprint(p) not in item['announced'] or old_aliases.intersection(item['aliases']):
                    candidates.append(dict(p, key=key, event='当日规则更新' if old_aliases.intersection(item['aliases']) else '新收录'))
            selected = choose(candidates, config)
            for p in selected:
                announced = state['papers'][p['key']]['announced']
                if fingerprint(p) not in announced:
                    announced.append(fingerprint(p))
            deferred = sum(not item.get('suppressed') and fingerprint(item['article']) not in item['announced'] for item in state['papers'].values())
    report = {'date': today, 'start': start.isoformat(), 'papers': selected, 'sources': statuses, 'deferred': deferred, 'rejected': rejected, 'mode': 'no-model-api', 'preview': preview}
    text = render(report, config)
    (directory / 'reports').mkdir(parents=True, exist_ok=True)
    json_write(report_path, report)
    (directory / 'reports' / (today + '.md')).write_text(text)
    if not preview:
        state['last_complete_date'] = today
        state['last_attempt_date'] = today
        state.setdefault('initial_start', start.isoformat())
        json_write(state_path, state)
    retrieval.update(complete=True, phase='complete')
    json_write(directory / 'state/retrieval_status.json', retrieval)
    json_write(directory / 'diagnostics' / ('retrieval-' + today + '.json'), retrieval)
    print(f"Report: {len(selected)} unique papers; {deferred} deferred. Preview={preview}")
    return report


def push_payload(report, repository):
    count = len(report['papers'])
    degraded = any(s['status'] != 'ok' for s in report['sources'].values())
    title = f"文献日报 {report['date']}：{count}篇" + ('（检索有缺口）' if degraded else '')
    url = f"https://github.com/{repository}/blob/main/reports/{report['date']}.md"
    journals = sum(not p['preprint'] for p in report['papers'])
    lines = [f"期刊 {journals}/25 篇，预印本 {count - journals}/25 篇；四方向与交叉各预分配5篇，缺额组内调剂。", f"[完整日报（公开阅读）]({url})", '']
    if degraded:
        lines += ['部分数据源失败或分页不完整，不能据此认定没有新论文。', '']
    elif count == 0:
        lines += ['在已查询数据源及当前规则下，没有新增或更新的合格论文。', '']
    highlights = [p for p in report['papers'] if not p['preprint']][:4] + [p for p in report['papers'] if p['preprint']][:4]
    for paper in highlights:
        metric = paper.get('jif')
        impact = f" IF {metric['value']:g}" if metric else ''
        entry = f"- [{'[预印本] ' if paper['preprint'] else ''}{md(paper['title'])}]({paper['url']}){impact}"
        if len('\n'.join(lines + [entry])) > 3000:
            break
        lines.append(entry)
    lines += ['', '手机通知展示精选标题；全部条目与原文摘要见公开日报。']
    return title, '\n'.join(lines)


def notify(report, directory, repository, sendkey):
    from delivery import deliver
    return deliver([report], directory, repository, sendkey)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('build', 'preview', 'notify', 'retry', 'status'))
    parser.add_argument('--date')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--retry-uncertain', action='store_true')
    parser.add_argument('--directory', type=Path, default=ROOT)
    args = parser.parse_args()
    config = load_config()
    today = args.date or dt.datetime.now(ZoneInfo(config['timezone'])).date().isoformat()
    if args.command in ('notify', 'retry'):
        from delivery import notify_pending
        notify_pending(args.directory, today, os.environ.get('GITHUB_REPOSITORY', config['github_owner'] + '/' + config['repository_name']), os.environ.get('SERVERCHAN_SENDKEY', ''), args.retry_uncertain)
    elif args.command == 'status':
        from delivery import status_summary
        status_summary(args.directory, today)
    else:
        build(config, args.directory, today, preview=args.command == 'preview', refresh=args.refresh)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('ERROR:', str(error), file=sys.stderr)
        sys.exit(1)
