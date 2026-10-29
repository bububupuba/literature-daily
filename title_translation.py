"""Offline Chinese titles, pinned Tencent HY-MT1.5-1.8B Q4_K_M model."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import time
import urllib.request

MODEL_ID = 'hy-mt1.5-1.8b-q4km-glossary-v2'
MODEL_SHA256 = '4383ac0c3c8e476de98ff979c2a3f069f8c4fb385e7860cf2d28da896cc477c7'
MODEL_NAME = 'HY-MT1.5-1.8B-Q4_K_M.gguf'
MODEL_URL = ('https://huggingface.co/tencent/HY-MT1.5-1.8B-GGUF/resolve/'
             '265b2e615a7dc9b06c435dc878829ad99a512ba2/' + MODEL_NAME)
GLOSSARY = {
    'Caenorhabditis elegans': '秀丽隐杆线虫', 'C. elegans': '秀丽隐杆线虫',
    'mitochondrial': '线粒体', 'mitochondria': '线粒体', 'mitophagy': '线粒体自噬',
    'cristae': '线粒体嵴', 'cardiolipin': '心磷脂',
    'phosphoinositides': '磷脂酰肌醇类', 'phosphoinositide': '磷脂酰肌醇',
    'phospholipid': '磷脂', 'sphingolipid': '鞘脂', 'ceramide': '神经酰胺',
    'lipid droplet': '脂滴', 'lipidomics': '脂质组学',
    'healthspan': '健康寿命', 'lifespan': '寿命', 'longevity': '长寿',
    'senescence': '衰老', 'ferroptosis': '铁死亡', 'proteostasis': '蛋白质稳态',
    'hydroxyproline': '羟脯氨酸', 'branched-chain amino acid': '支链氨基酸',
    'sonic hedgehog': 'Sonic hedgehog（SHH）',
    'matrix metalloproteinase': '基质金属蛋白酶', 'rhabdomyosarcoma': '横纹肌肉瘤',
    'cell traps': '细胞捕获装置', 'depression': '抑郁', 'picoliter': '皮升',
    'untargeted': '非靶向', 'targeted': '靶向', 'mitochondrial dynamics': '线粒体动力学',
    'O-GlcNAcylation': 'O-GlcNAc糖基化', 'NAD': 'NAD', 'rRNA': 'rRNA',
}


def valid_chinese(value):
    return isinstance(value, str) and bool(re.search(r'[\u3400-\u9fff]', value)) and len(value.strip()) >= 3


def cache_key(title):
    return hashlib.sha256((MODEL_ID + '\n' + title).encode()).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def ensure_model():
    cache = Path(os.environ.get('TITLE_MODEL_CACHE', Path(__file__).resolve().parent / '.models'))
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / MODEL_NAME
    if target.exists() and file_hash(target) == MODEL_SHA256:
        return target
    errors = []
    for attempt in range(3):
        temporary = target.with_suffix('.download')
        try:
            request = urllib.request.Request(MODEL_URL, headers={'User-Agent': 'LiteratureDaily/3.0'})
            with urllib.request.urlopen(request, timeout=120) as response, temporary.open('wb') as out:
                shutil.copyfileobj(response, out)
            if file_hash(temporary) != MODEL_SHA256:
                raise RuntimeError('Model checksum mismatch')
            temporary.replace(target)
            return target
        except Exception as exc:
            errors.append(type(exc).__name__)
            if temporary.exists():
                temporary.unlink()
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError('Offline title model unavailable; publication stopped: ' + ', '.join(errors))


def protected_names(title):
    names = re.findall(r'(?<![A-Za-z0-9])(?:[A-Z][A-Z0-9]{1,}|[A-Za-z][A-Za-z0-9]*[0-9][A-Za-z0-9]*)(?![A-Za-z0-9])', title)
    return sorted({name for name in names if name not in ('II', 'III', 'IV', 'VI', 'VII', 'VIII', 'IX', 'UK', 'US', 'USA', 'EU')})


def translation_prompt(title):
    terms = [f'{source} 翻译成 {target}' for source, target in GLOSSARY.items() if source.casefold() in title.casefold()]
    terms.extend(f'{name} 翻译成 {name}' for name in protected_names(title))
    guidance = '参考下面的翻译：\n' + '\n'.join(terms) + '\n\n' if terms else ''
    message = guidance + '将以下生物学论文标题翻译为简体中文，保留基因和蛋白名称及缩写，只输出翻译后的标题，不要额外解释：\n\n' + title
    # Official model template; no system prompt.
    return '<｜hy_begin▁of▁sentence｜><｜hy_User｜>' + message + '<｜hy_Assistant｜>'


def translate_batch(titles):
    from llama_cpp import Llama
    translator = Llama(model_path=str(ensure_model()), n_ctx=2048, n_threads=2,
                       n_threads_batch=2, n_batch=256, n_gpu_layers=0,
                       offload_kqv=False, op_offload=False, verbose=False)
    results = []
    try:
        for index, title in enumerate(titles, 1):
            prompt = translation_prompt(title)
            tokens = translator.tokenize(prompt.encode(), add_bos=False, special=True)
            if len(tokens) + 384 > 2048:
                raise RuntimeError('Title exceeds model input limit; refusing to truncate')
            for attempt in range(3):
                output = translator(tokens, max_tokens=384, temperature=0 if attempt == 0 else 0.7,
                                    top_k=20, top_p=0.6, repeat_penalty=1.05, seed=42 + attempt)
                choice = output['choices'][0]
                translated = choice['text'].strip()
                missing = [name for name in protected_names(title) if name not in translated]
                valid = (choice['finish_reason'] != 'length' and valid_chinese(translated)
                         and '\n' not in translated and '<｜' not in translated and not missing)
                if valid:
                    break
                print(f'Retrying incomplete title translation {index}: attempt {attempt + 1}', flush=True)
            else:
                raise RuntimeError('Chinese title incomplete or identifiers missing; publication stopped. Title: ' + title + '; missing identifiers: ' + ', '.join(missing))
            results.append(translated)
            print(f'Chinese titles translated: {index}/{len(titles)}', flush=True)
    finally:
        translator.close()
    return results


def translate_titles(papers, cache_path):
    from monitor import json_read, json_write
    if not papers:
        return
    cache = json_read(cache_path, {})
    titles = list(dict.fromkeys(p['title'] for p in papers))
    missing = [title for title in titles if not (
        cache.get(cache_key(title), {}).get('source') == title
        and cache.get(cache_key(title), {}).get('model') == MODEL_ID
        and valid_chinese(cache.get(cache_key(title), {}).get('zh')))]
    if missing:
        translated = translate_batch(missing)
        if len(translated) != len(missing) or any(not valid_chinese(title) for title in translated):
            raise RuntimeError('Chinese title translation incomplete; publication stopped')
        for source, zh in zip(missing, translated):
            cache[cache_key(source)] = {'source': source, 'zh': zh, 'model': MODEL_ID}
        json_write(cache_path, cache)
    for p in papers:
        p['title_zh'] = cache[cache_key(p['title'])]['zh']
        p['title_translation'] = MODEL_ID
