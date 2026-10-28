"""Independent journal acquisition through Crossref, PubMed and publisher feeds."""
import datetime as dt
import email.utils
import re
import time
import xml.etree.ElementTree as ET

from monitor import clean, doi, day, crossref_article, SourceError


def xml_text(element):
    return clean(''.join(element.itertext())) if element is not None else ''


def xml_date(element):
    if element is None:
        return ''
    year, month, date = (element.findtext(field, '') for field in ('Year', 'Month', 'Day'))
    if not year or not month or not date:
        return ''
    if not month.isdigit():
        try:
            month = str(dt.datetime.strptime(month[:3], '%b').month)
        except ValueError:
            return ''
    return day(f'{int(year):04d}-{int(month):02d}-{int(date):02d}')


def pubmed_article(node):
    citation = node.find('MedlineCitation')
    article = citation.find('Article')
    identifier = next((doi(x.text) for x in node.findall('./PubmedData/ArticleIdList/ArticleId') if x.get('IdType') == 'doi'), '')
    pmid = citation.findtext('PMID', '')
    dates = [xml_date(x) for x in article.findall('ArticleDate') if x.get('DateType') == 'Electronic']
    dates += [xml_date(x) for x in node.findall('./PubmedData/History/PubMedPubDate') if x.get('PubStatus') in ('epublish', 'aheadofprint')]
    published = min([x for x in dates if x], default='') or xml_date(article.find('./Journal/JournalIssue/PubDate'))
    abstracts = []
    for field in article.findall('./Abstract/AbstractText'):
        abstracts.append((field.get('Label', '') + ': ' if field.get('Label') else '') + xml_text(field))
    types = [xml_text(x) for x in article.findall('./PublicationTypeList/PublicationType')]
    authors = []
    for author in article.findall('./AuthorList/Author'):
        authors.append(author.findtext('CollectiveName') or ' '.join(filter(None, [author.findtext('ForeName'), author.findtext('LastName')])) )
    related = []
    retracted = any('retracted' in t.lower() for t in types)
    for comment in citation.findall('./CommentsCorrectionsList/CommentsCorrections'):
        kind = comment.get('RefType', '').lower()
        if kind in ('retractionin', 'retractionof'):
            retracted = True
        if kind in ('preprintin', 'preprintof') and comment.findtext('PMID'):
            related.append('PMID:' + comment.findtext('PMID'))
    return dict(doi=identifier, ids=['PMID:' + pmid] + related, title=xml_text(article.find('ArticleTitle')),
                authors='; '.join(authors), venue=article.findtext('./Journal/Title', ''), date=published,
                indexed=xml_date(citation.find('DateCompleted')), abstract=' '.join(abstracts), types=types,
                preprint='Preprint' in types, version=0,
                url='https://doi.org/' + identifier if identifier else f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/',
                related=[], sources=['PubMed'], retracted=retracted)


def pubmed_query(topics, start, end, incremental):
    terms = [f'"{term}"[Title/Abstract]' if ' ' in term else f'{term}[Title/Abstract]' for spec in topics.values() for term in spec['anchors']]
    dates = f'("{start}"[Date - Publication] : "{end}"[Date - Publication])'
    if incremental:
        dates = f'({dates} OR ("{start}"[Entrez Date] : "{end}"[Entrez Date]))'
    return '(' + ' OR '.join(terms) + ') AND ' + dates


def fetch_pubmed(client, topics, start, end, incremental, max_pages):
    base = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
    query = pubmed_query(topics, start, end, incremental)
    limit = min(9999, max_pages * 100)
    result = client.get(base + 'esearch.fcgi', {'db': 'pubmed', 'term': query, 'retmode': 'json', 'retmax': limit, 'sort': 'pub date', 'tool': 'literature_daily'})['esearchresult']
    if result.get('ERROR') or result.get('errorlist', {}).get('fieldnotfound'):
        raise RuntimeError('PubMed query rejected')
    ids, articles, total = result.get('idlist', []), [], int(result.get('count', 0))
    for offset in range(0, len(ids), 100):
        time.sleep(0.4)  # One PubMed worker, below the keyless 3 requests/s limit.
        root = ET.fromstring(client.read(base + 'efetch.fcgi', {'db': 'pubmed', 'id': ','.join(ids[offset:offset + 100]), 'retmode': 'xml', 'tool': 'literature_daily'}))
        if root.find('.//ERROR') is not None:
            raise RuntimeError('PubMed fetch rejected')
        articles.extend(pubmed_article(node) for node in root.findall('PubmedArticle'))
    return articles, {'status': 'ok' if len(articles) >= total else 'partial', 'fetched': len(articles), 'available': total, 'query': query, 'family': 'pubmed', 'topics': list(topics)}


def fetch_crossref_query(client, query, topic_key, start, end, max_pages):
    cursor, articles, total = '*', [], 0
    params = {'filter': f'type:journal-article,from-pub-date:{start},until-pub-date:{end}', 'query.bibliographic': query, 'rows': 100}
    for _ in range(max_pages):
        data = client.get('https://api.crossref.org/works', dict(params, cursor=cursor))['message']
        total = int(data['total-results'])
        batch = data.get('items', [])
        articles.extend(crossref_article(r) for r in batch)
        if len(articles) >= total:
            break
        next_cursor = data.get('next-cursor')
        if not batch or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return articles, {'status': 'ok' if len(articles) >= total else 'partial', 'fetched': len(articles), 'available': total, 'query': query, 'family': 'crossref', 'topics': [topic_key]}


def fetch_crossref_journals(client, queries, topic_key, start, end, max_pages):
    articles, statuses = [], []
    for query in queries:
        try:
            found, status = fetch_crossref_query(client, query, topic_key, start, end, max_pages)
            articles.extend(found)
            statuses.append(status)
        except Exception as error:
            statuses.append({'status': 'failed', 'query': query, 'error': type(error).__name__})
    status = 'ok' if all(x['status'] == 'ok' for x in statuses) else 'failed' if all(x['status'] == 'failed' for x in statuses) else 'partial'
    return articles, {'status': status, 'fetched': len(articles), 'available': sum(x.get('available', 0) for x in statuses), 'family': 'crossref', 'topics': [topic_key], 'queries': statuses}


def feed_date(text):
    parsed = day(text)
    if parsed:
        return parsed
    try:
        return email.utils.parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return ''


def parse_feed(data, feed, start, end):
    root = ET.fromstring(data)
    if root.tag.rsplit('}', 1)[-1] not in ('RDF', 'rss', 'feed'):
        raise SourceError('Response is not an RSS/RDF/Atom feed')
    articles = []
    for item in root.iter():
        if item.tag.rsplit('}', 1)[-1] not in ('item', 'entry'):
            continue
        fields = {}
        for child in item:
            local = child.tag.rsplit('}', 1)[-1]
            fields.setdefault(local, []).append(child)
        def texts(*names):
            return [xml_text(x) for name in names for x in fields.get(name, []) if xml_text(x)]
        links = [x.get('href') or xml_text(x) for x in fields.get('link', []) if x.get('rel', 'alternate') == 'alternate']
        link = next((x for x in links if x.startswith('http')), '')
        identifiers = texts('doi', 'identifier') + [link]
        found = next((doi(x) for x in identifiers if doi(x)), '')
        if not found:
            for value in identifiers:
                match = re.search(r'10\.\d{4,9}/[^\s<>"?#]+', value)
                if match:
                    found = doi(match.group())
                    break
        dates = [feed_date(x) for x in texts('publicationDate', 'date', 'published', 'pubDate')]
        date = min([x for x in dates if x], default='')
        if not date or date < start or date > end:
            continue
        abstract = max(texts('description', 'abstract', 'summary', 'encoded', 'content'), key=len, default='')
        title = next(iter(texts('title')), '')
        if not title or not (found or link):
            continue
        articles.append(dict(doi=found, ids=[], title=title, authors='; '.join(texts('creator', 'author')),
            venue=feed['name'], date=date, indexed='', abstract=abstract, types=texts('type', 'genre') or ['Journal Article'], preprint=False,
            version=0, url='https://doi.org/' + found if found else link, related=[], sources=['RSS / ' + feed['name']], retracted=False))
    # RSS is a rolling feed, not a complete historical API; say so explicitly.
    return articles, {'status': 'ok', 'fetched': len(articles), 'available': len(articles), 'family': 'rss', 'scope': '滚动订阅源当前可见条目，不代表期刊完整历史', 'url': feed['url']}


def fetch_rss(client, feed, start, end):
    attempts = []
    for attempt in range(3):
        diagnostics = {'attempt': attempt + 1}
        try:
            data = client.read_feed(feed['url'], diagnostics)
            articles, status = parse_feed(data, feed, start, end)
            diagnostics['result'] = 'ok'
            attempts.append(diagnostics)
            status['attempts'] = attempts
            return articles, status
        except (ET.ParseError, SourceError, UnicodeDecodeError) as error:
            diagnostics.update(result='failed', error=str(error) if isinstance(error, (ET.ParseError, SourceError)) else type(error).__name__)
            attempts.append(diagnostics)
            if attempt < 2:
                time.sleep(2 ** attempt)
    # A real browser can execute the publisher's ordinary anonymous JS session.
    # Do not substitute another database or mark a challenge page as an empty feed.
    if feed['url'].startswith('https://www.nature.com/') and any(a.get('page_title') == 'Client Challenge' for a in attempts):
        diagnostics = {'attempt': 4, 'transport': 'chromium'}
        try:
            data = client.read_browser_feed(feed['url'], diagnostics)
            articles, status = parse_feed(data, feed, start, end)
            diagnostics['result'] = 'ok'
            attempts.append(diagnostics)
            status['attempts'] = attempts
            return articles, status
        except (ET.ParseError, SourceError, UnicodeDecodeError) as error:
            diagnostics.update(result='failed', error=str(error) if isinstance(error, (ET.ParseError, SourceError)) else type(error).__name__)
            attempts.append(diagnostics)
    return [], {'status': 'failed', 'fetched': 0, 'family': 'rss', 'url': feed['url'],
                'error': 'RSS retrieval/validation failed after available HTTP and browser attempts', 'attempts': attempts}
