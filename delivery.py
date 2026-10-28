"""Persistent, batch delivery of current and missed daily reports."""
import datetime as dt
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from monitor import json_read, json_write, push_payload, require_complete


class DeliveryError(RuntimeError):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def send(sendkey, title, body):
    if not sendkey:
        raise DeliveryError('Missing SERVERCHAN_SENDKEY; no notification sent.')
    if not re.fullmatch(r'[A-Za-z0-9]+', sendkey):
        raise DeliveryError('Invalid SendKey format; secret omitted.')
    sc3 = re.match(r'^sctp(\d+)t', sendkey)
    endpoint = f'https://{sc3.group(1)}.push.ft07.com/send/{sendkey}.send' if sc3 else f'https://sctapi.ftqq.com/{sendkey}.send'
    request = urllib.request.Request(endpoint, data=urllib.parse.urlencode({'title': title, 'desp': body}).encode(), headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise DeliveryError(f'Notification service HTTP {error.code}; secret omitted.', uncertain=error.code >= 500) from None
    except urllib.error.URLError as error:
        known_unsent = isinstance(error.reason, (socket.gaierror, ConnectionRefusedError))
        raise DeliveryError('Notification connection failed; secret omitted.', uncertain=not known_unsent) from None
    except Exception:
        raise DeliveryError('Notification response uncertain; secret omitted.', uncertain=True) from None
    if result.get('code') != 0:
        raise DeliveryError('Notification service rejected request; secret omitted.')


def acknowledged(receipts, identity):
    return receipts.get(identity, {}).get('acknowledged') is True


def deliver(reports, directory, repository, sendkey, retry_uncertain=False):
    directory = Path(directory)
    retrieval = json_read(directory / 'state/retrieval_status.json', {})
    if retrieval and not retrieval.get('complete'):
        require_complete(retrieval)
    receipts_path = directory / 'state/deliveries.json'
    attempts_path = directory / 'state/delivery_attempts.json'
    receipts = json_read(receipts_path, {})
    attempts = json_read(attempts_path, {})
    waiting, uncertain = [], []
    for report in sorted(reports, key=lambda r: r['date']):
        identity = report['date']
        if acknowledged(receipts, identity):
            continue
        require_complete(report)
        if attempts.get(identity, {}).get('status') in ('uncertain', 'sending') and not retry_uncertain:
            uncertain.append(identity)
            continue
        waiting.append(report)
    if not waiting:
        if uncertain:
            raise DeliveryError('Delivery needs verification for: ' + ', '.join(uncertain) + '. Check ServerChan delivery history before explicit retry.', uncertain=True)
        print('No pending notification; all report dates acknowledged.')
        return 0
    if not sendkey:
        raise DeliveryError('Missing SERVERCHAN_SENDKEY; no notification sent.')
    latest = waiting[-1]
    title, body = push_payload(latest, repository)
    if len(waiting) > 1:
        title += f'，补发{len(waiting) - 1}份日报'
        # Include all missed report URLs without duplicating individual paper entries.
        links = [f"- [{r['date']}：{len(r['papers'])}篇](https://github.com/{repository}/blob/main/reports/{r['date']}.md)" for r in waiting[:-1]]
        body = '此前未送达日报（完整链接补发）：\n' + '\n'.join(links) + '\n\n' + body
    if uncertain:
        body = '以下日期投递结果待核实：' + '、'.join(uncertain) + '。请先检查 Server酱记录，避免重复通知。\n\n' + body
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for report in waiting:
        identity = report['date']
        attempts[identity] = {'status': 'sending', 'attempts': attempts.get(identity, {}).get('attempts', 0) + 1, 'at': now}
    json_write(attempts_path, attempts)  # Persist before POST so a killed job is not silently retried.
    try:
        send(sendkey, title, body)
    except DeliveryError as error:
        for report in waiting:
            attempts[report['date']]['status'] = 'uncertain' if error.uncertain else 'failed'
        json_write(attempts_path, attempts)
        raise
    for report in waiting:
        identity = report['date']
        receipts[identity] = {'acknowledged': True, 'at': now, 'batch_latest_date': latest['date']}
        attempts[identity]['status'] = 'acknowledged'
    json_write(receipts_path, receipts)
    json_write(attempts_path, attempts)
    print(f'Notification acknowledged; {len(waiting)} report dates delivered in one digest.')
    return len(waiting)


def pending_reports(directory, today):
    return [r for path in sorted((Path(directory) / 'reports').glob('????-??-??.json'))
            if (r := json_read(path, {})) and not r.get('preview') and r.get('date', '9999') <= today]


def notify_pending(directory, today, repository, sendkey, retry_uncertain=False):
    return deliver(pending_reports(directory, today), directory, repository, sendkey, retry_uncertain)


def status_summary(directory, today):
    directory = Path(directory)
    receipts = json_read(directory / 'state/deliveries.json', {})
    attempts = json_read(directory / 'state/delivery_attempts.json', {})
    reports = pending_reports(directory, today)
    pending = [r['date'] for r in reports if not acknowledged(receipts, r['date'])]
    uncertain = [date for date in pending if attempts.get(date, {}).get('status') in ('uncertain', 'sending')]
    latest = reports[-1] if reports else None
    retrieval = json_read(directory / 'state/retrieval_status.json', {})
    blocked = bool(retrieval) and not retrieval.get('complete')
    sources = retrieval.get('sources', {}) if retrieval else latest.get('sources', {}) if latest else {}
    status = {'date': today, 'pending_report_dates': pending, 'uncertain_report_dates': uncertain,
              'latest_report': latest['date'] if latest else None,
              'retrieval_blocked': blocked, 'last_retrieval_attempt': retrieval.get('date'),
              'source_problems': [name for name, s in sources.items() if s['status'] != 'ok'] if sources else ['检索尚未完成']}
    json_write(directory / 'state/health.json', status)
    text = '\n'.join(['## 文献日报运行状态', '', f"最近日报：{status['latest_report'] or '无'}", f"未确认送达：{', '.join(pending) or '无'}", f"需核实投递：{', '.join(uncertain) or '无'}", f"检索失败已停止发布和推送：{'是' if blocked else '否'}", f"数据源异常：{', '.join(status['source_problems']) or '无'}", '', '检索失败或分页不完整：修复并成功重跑前停止发布、推送及补发。投递明确失败：北京时间 10:00、12:00 补发；投递结果不确定：核实后手动重试。', ''])
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write(text)
    print(text)
    return status
