#!/usr/bin/env python3
"""Daily literature feeds. Python 3.11+, standard library, no model API."""
import argparse
import copy
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
EPMC = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
BIO = 'https://api.biorxiv.org'
CROSSREF = 'https://api.crossref.org/works'


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


class Client:
    def __init__(self, timeout=35):
        self.timeout = timeout

    def get(self, url, params=None):
        if params:
            url += '?' + urllib.parse.urlencode(params)
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={'User-Agent': 'LiteratureDaily/1.0', 'Accept': 'application/json'})
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if attempt == 2:
                    # Never include request URLs in errors: push URLs contain secrets.
                    raise RuntimeError(type(exc).__name__) from None
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
    return dict(article, topics=topics, reasons=reasons, score=score, signals={'mechanism': mechanism_hits, 'method': method_hits, 'discovery_claim': discovery_hits}, preferred=article['venue'].casefold() in {j.casefold() for j in config['preferred_journals']}), ''


def aliases(article):
    result = set(article.get('ids', []))
    for value in [article.get('doi', '')] + article.get('related', []):
        if value:
            result.add('doi:' + root_doi(value))
    if not result:
        result.add('url:' + article['url'])
    return result


def consolidate(articles):
    groups = []
    for article in articles:
        keys = aliases(article)
        overlap = [group for group in groups if keys & group['aliases']]
        if overlap:
            group = overlap[0]
            group['articles'].append(article)
            group['aliases'].update(keys)
            for extra in overlap[1:]:
                group['articles'].extend(extra['articles'])
                group['aliases'].update(extra['aliases'])
                groups.remove(extra)
        else:
            groups.append({'aliases': keys, 'articles': [article]})
    output = []
    for group in groups:
        # The journal version takes precedence; otherwise keep latest preprint version.
        members = sorted(group['articles'], key=lambda a: (not a['preprint'], a['version'], a['date'], len(a['abstract'])), reverse=True)
        paper = copy.deepcopy(members[0])
        paper['aliases'] = sorted(group['aliases'])
        paper['sources'] = sorted({source for a in members for source in a['sources']})
        paper['related'] = sorted({value for a in members for value in [a['doi']] + a['related'] if value and value != paper['doi']})
        paper['retracted'] = any(a.get('retracted') for a in members)
        # Only supplement abstracts from the same DOI and same version.
        for a in members:
            if a['doi'] == paper['doi'] and a['version'] == paper['version'] and len(a['abstract']) > len(paper['abstract']):
                paper['abstract'] = a['abstract']
        output.append(paper)
    return output


def fingerprint(paper):
    # Bibliographic corrections and indexing changes are not substantive updates.
    return f"{paper['doi'] or paper['url']}|{'preprint' if paper['preprint'] else 'journal'}|{paper['version'] if paper['preprint'] else 0}"


def rank(paper):
    return (-paper['score'], -int(paper['preferred']), paper['title'].casefold())


def choose(papers, config):
    ordered = sorted(papers, key=rank)
    chosen = []
    chosen_ids = set()
    def add(p):
        if p['key'] not in chosen_ids and len(chosen) < config['max_papers']:
            chosen.append(p)
            chosen_ids.add(p['key'])
    # Protect each independent direction from being crowded out by a larger field.
    for index in range(config['reserved_per_feed']):
        for topic in config['topics']:
            candidates = [p for p in ordered if topic in p['topics']]
            if index < len(candidates):
                add(candidates[index])
    for p in ordered:
        add(p)
    return sorted(chosen, key=rank)


def ingest(records, state, config, report_date):
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
    selected = choose(pending, config)
    for p in selected:
        state['papers'][p['key']]['announced'].append(fingerprint(p))
    return selected, len(pending) - len(selected), rejected


def md(value):
    return re.sub(r'([\\`*_{}\[\]<>#|])', r'\\\1', clean(value))


def render(report, config):
    papers = report['papers']
    lines = [f"# 文献日报 · {report['date']}", '', '**无大模型模式：中文栏目与匹配理由；标题、作者和摘要保留原文。**', '',
             '排序为可复核的关键词与方法线索排序，不代表创新性、严谨性、机制深度或影响力已经经过评审。摘要为作者原文，可能同时包含实验描述、解释与假设；本程序不把这些表述改写为已证实事实。', '',
             f"本次纳入 **{len(papers)}** 篇独立文献；交叉栏目与各方向共享编号；另有 {report['deferred']} 篇顺延。", '',
             f"检索窗口：{report['start']} 至 {report['date']}（北京时间）。初次回溯 3 个日历日；后续重叠检索并按持久标识符去重。", '', '## 数据源状态', '']
    labels = {'ok': '完成', 'partial': '覆盖不完整', 'failed': '失败'}
    for source, status in report['sources'].items():
        lines.append(f"- {source}：{labels[status['status']]}；获取 {status.get('fetched', 0)} / {status.get('available', '未知')} 条。" + (f" 错误：{status['error']}" if status.get('error') else ''))
    if any(s['status'] != 'ok' for s in report['sources'].values()):
        lines += ['', '**本次存在检索缺口，不能把零结果理解为没有新论文。**']
    elif not papers:
        lines += ['', '在已查询数据源与当前规则下，本次没有新增或更新的合格文献。']
    lines += ['', '## 优先关注的交叉研究（规则筛选候选）', '']
    cross = [(i, p) for i, p in enumerate(papers, 1) if len(p['topics']) >= 2]
    for i, p in cross:
        lines.append(f"- [#{i} {md(p['title'])}](#paper-{i}) — " + ' × '.join(config['topics'][t]['name'] for t in p['topics']))
    if not cross:
        lines.append('本次没有符合两个以上方向筛选规则的候选论文。')
    for topic, spec in config['topics'].items():
        lines += ['', f"## {spec['name']}", '']
        subset = [(i, p) for i, p in enumerate(papers, 1) if topic in p['topics']]
        lines += [f"- [#{i} {md(p['title'])}](#paper-{i})" for i, p in subset] or ['本次无新增或更新。']
    lines += ['', '## 文献详情', '']
    for i, p in enumerate(papers, 1):
        status = f"**预印本：未经同行评审 · v{p['version'] or '未知'}**" if p['preprint'] else '期刊发表版本（同行评审过程未单独核验）'
        lines += [f'<a id="paper-{i}"></a>', f"### {i}. {md(p['title'])}", '', f"- 作者：{md(p['authors']) or '数据库未提供'}", f"- 来源：{md(p['venue'])} · 日期：{p['date'] or '未提供'}", f"- 状态：{status}；{p['event']}", f"- 原文：[{md(p['doi']) or '原始页面'}]({p['url']})", f"- 阅读优先分：{p['score']}（规则分，不是科学质量分）", '- 关联方向：' + '、'.join(config['topics'][t]['name'] for t in p['topics'])]
        for topic in p['topics']:
            r = p['reasons'][topic]
            terms = r['title_terms'] or r['abstract_terms']
            lines.append(f"- 与{config['topics'][topic]['name']}相关的依据：{'标题' if r['title_terms'] else '摘要'}匹配 {md('；'.join(terms))}。")
        if p['related']:
            lines.append('- 关联版本：' + '；'.join(f'[{md(d)}](https://doi.org/{d})' for d in p['related']))
        lines += ['', '**原文摘要（作者表述，未经自动证据分层）**', '', '> ' + md(p['abstract']), '', '**科学意义、实验可靠性与推测边界：** 待阅读全文人工判断；不根据期刊名或关键词自动下结论。', '']
    lines += ['## 范围与局限', '', '- 不设期刊白名单；偏好期刊只在规则分相同时作为次级排序依据。', '- Europe PMC 的期刊、预印本索引存在收录延迟；bioRxiv、medRxiv另查官方接口；Research Square另查 Crossref。不能保证全球文献零遗漏。', '- 缺少足够摘要、日期或生物学背景的记录不会进入推荐；过滤数量保存在 JSON 报告。', '- DOI、PMID、数据库标识符及明确版本关系用于去重。无法确认关系时保留记录，避免把相似标题误合并；没有登记的预印本—期刊关系可能漏识别。', '- 预印本版本号变化会提示更新；元数据不含新版本号时不能可靠识别所有内容修订。', '- 已明确标记的撤稿／撤回记录排除；数据库延迟标记的撤稿可能尚未识别。', '']
    return '\n'.join(lines)


def collect(config, start, end, incremental):
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
        for future in as_completed(tasks):
            name = tasks[future]
            try:
                found, status = future.result()
                records.extend(found)
                statuses[name] = status
            except Exception as exc:
                statuses[name] = {'status': 'failed', 'error': type(exc).__name__, 'fetched': 0}
            print(name, statuses[name]['status'], statuses[name].get('fetched', 0), flush=True)
    return records, statuses


def build(config, directory, today, preview=False):
    state_path = directory / 'state' / 'history.json'
    report_path = directory / 'reports' / (today + '.json')
    # Keep the day's report stable on reruns, including after failed notification.
    if report_path.exists() and not preview:
        print('Report already prepared; preserving same-day report and delivery identity.')
        return json_read(report_path, {})
    state = json_read(state_path, {'papers': {}})
    initial = not state.get('last_complete_date')
    date = dt.date.fromisoformat(today)
    start = date - dt.timedelta(days=(config['initial_days'] if initial else config['overlap_days']) - 1)
    if state.get('last_complete_date'):
        start = min(start, dt.date.fromisoformat(state['last_complete_date']) - dt.timedelta(days=2))
    records, statuses = collect(config, start.isoformat(), today, not initial)
    if preview:
        json_write(directory / 'raw-preview.json', records)
    if not statuses or all(s['status'] == 'failed' for s in statuses.values()):
        selected, deferred, rejected = [], 0, {}
    else:
        selected, deferred, rejected = ingest(records, state, config, today)
    report = {'date': today, 'start': start.isoformat(), 'papers': selected, 'sources': statuses, 'deferred': deferred, 'rejected': rejected, 'mode': 'no-model-api', 'preview': preview}
    text = render(report, config)
    (directory / 'reports').mkdir(parents=True, exist_ok=True)
    json_write(report_path, report)
    (directory / 'reports' / (today + '.md')).write_text(text)
    if not preview:
        if statuses and all(s['status'] == 'ok' for s in statuses.values()):
            state['last_complete_date'] = today
        json_write(state_path, state)
    print(f"Report: {len(selected)} unique papers; {deferred} deferred. Preview={preview}")
    return report


def push_payload(report, repository):
    count = len(report['papers'])
    degraded = any(s['status'] != 'ok' for s in report['sources'].values())
    title = f"文献日报 {report['date']}：{count}篇" + ('（检索有缺口）' if degraded else '')
    url = f"https://github.com/{repository}/blob/main/reports/{report['date']}.md"
    lines = [f"本次 {count} 篇独立文献，四方向分别检索；无 AI 总结。", f"[完整日报（需要登录 GitHub）]({url})", '']
    if degraded:
        lines += ['部分数据源失败或分页不完整，不能据此认定没有新论文。', '']
    elif count == 0:
        lines += ['在已查询数据源及当前规则下，没有新增或更新的合格论文。', '']
    for paper in report['papers'][:8]:
        entry = f"- [{'[预印本] ' if paper['preprint'] else ''}{md(paper['title'])}]({paper['url']})"
        if len('\n'.join(lines + [entry])) > 3000:
            break
        lines.append(entry)
    lines += ['', '手机通知展示精选标题；全部条目与原文摘要见私有仓库。']
    return title, '\n'.join(lines)


def notify(report, directory, repository, sendkey):
    if not sendkey:
        raise RuntimeError('Missing SERVERCHAN_SENDKEY; no notification was sent.')
    if not re.fullmatch(r'[A-Za-z0-9]+', sendkey):
        raise RuntimeError('Invalid SendKey format.')
    receipts_path = directory / 'state' / 'deliveries.json'
    receipts = json_read(receipts_path, {})
    title, body = push_payload(report, repository)
    identity = report['date']
    if identity in receipts:
        print('Notification already acknowledged for this date; skipped.')
        return
    sc3 = re.match(r'^sctp(\d+)t', sendkey)
    endpoint = f"https://{sc3.group(1)}.push.ft07.com/send/{sendkey}.send" if sc3 else f'https://sctapi.ftqq.com/{sendkey}.send'
    request = urllib.request.Request(endpoint, data=urllib.parse.urlencode({'title': title, 'desp': body}).encode(), headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        # Do not automatically retry POST: the first request may already be delivered.
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        if result.get('code') != 0:
            raise RuntimeError('Notification service returned nonzero status.')
    except Exception:
        raise RuntimeError('Notification failed or delivery is uncertain; inspect service delivery history before rerunning. Secret omitted.') from None
    receipts[identity] = {'acknowledged': True, 'at': dt.datetime.now(dt.timezone.utc).isoformat()}
    json_write(receipts_path, receipts)
    print('Notification service acknowledged delivery.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('build', 'preview', 'notify'))
    parser.add_argument('--date')
    parser.add_argument('--directory', type=Path, default=ROOT)
    args = parser.parse_args()
    config = json_read(ROOT / 'config.json', {})
    today = args.date or dt.datetime.now(ZoneInfo(config['timezone'])).date().isoformat()
    if args.command == 'notify':
        report = json_read(args.directory / 'reports' / (today + '.json'), None)
        if not report or report.get('preview'):
            raise RuntimeError('No production report available.')
        notify(report, args.directory, os.environ.get('GITHUB_REPOSITORY', config['github_owner'] + '/' + config['repository_name']), os.environ.get('SERVERCHAN_SENDKEY', ''))
    else:
        build(config, args.directory, today, preview=args.command == 'preview')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('ERROR:', str(error), file=sys.stderr)
        sys.exit(1)
