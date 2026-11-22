"""Dependency-free inference for the locally trained SECURE classifiers.

The model assists extraction/review. It never invents quantities or overrides
an exact code, source total, or saved-row verification rule.
"""
import json
import math
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

MODEL_PATH = Path(__file__).with_name('secure-reader-model.json')
CODE_FEATURES = ['family_equal', 'section_equal', 'number_equal', 'suffix_equal', 'both_valid']
ROW_FEATURES = ['serial_numeric', 'quantity_numeric', 'no_numeric', 'dimension_fraction',
                'cf_numeric', 'description_present', 'heading', 'resource_unit', 'negative', 'has_dimensions']

def code_parts(value):
    text = unicodedata.normalize('NFKC', str(value or '')).lower()
    text = re.sub(r'[\u200b-\u200d\ufeff]', '', text).strip()
    match = re.fullmatch(r'(\d{2})[.\s\-_/]+(\d{2})[.\s\-_/]+(\d{2,})(?:[.\s\-_/]*([a-z]))?[.\s]*', text)
    return tuple(p or '' for p in match.groups()) if match else None

def code_features(expected, candidate):
    a, b = code_parts(expected), code_parts(candidate)
    return [float(bool(a and b) and a[i] == b[i]) for i in range(4)] + [float(bool(a and b))]

def numeric(value):
    return bool(re.fullmatch(r'-?\d+(?:\.\d+)?', str(value or '').strip().replace(',', '')))

def row_features(row):
    desc = ' '.join(str(row.get('description', '')).lower().split())
    dims = [row.get(k, '') for k in ('l', 'b', 'd')]
    return [float(str(row.get('serial', '')).isdigit()), float(numeric(row.get('quantity'))),
            float(numeric(row.get('no'))), sum(numeric(v) for v in dims) / 3,
            float(numeric(row.get('cf'))), float(bool(desc)),
            float(desc in {'details', 'detail', 'description', 'list', 'total quantity', 'net total quantity', 'deduction'}),
            float(str(row.get('no', '')).strip().lower() in {'day', 'cum', 'sqm', 'kg', 'meter', 'each'}),
            float(str(row.get('quantity', '')).strip().startswith('-')), float(any(numeric(v) for v in dims))]

@lru_cache(maxsize=1)
def load_model():
    model = json.loads(MODEL_PATH.read_text(encoding='utf-8'))
    if model.get('schema') != 1 or model['code']['features'] != CODE_FEATURES or model['row']['features'] != ROW_FEATURES:
        raise ValueError('Trained reader model is incompatible. Reinstall the current app.')
    return model

def predict(features, model):
    z = model['bias'] + sum(w * x for w, x in zip(model['weights'], features))
    return 1 / (1 + math.exp(-max(-40, min(40, z))))

def audit_items(items, progress=None):
    model = load_model()
    for index, item in enumerate(items, 1):
        item['reader_mode'] = 'trained_ai'
        item['reader_model'] = model['version']
        warnings = []
        if item.get('total_warning'):
            warnings.append(item['total_warning'])
        if not code_parts(item['code']):
            warnings.append('Item code could not be split into ordered components; review it.')
        for row in item.get('rows', []):
            score = predict(row_features(row), model['row'])
            row['ai_measurement_score'] = round(score, 4)
            if score < model['row']['threshold']:
                message = f'Trained reader: row {row.get("serial", "?")} is uncertain. Check the source page.'
                row['warning'] = ' '.join(filter(None, [row.get('warning'), message]))
                warnings.append(message)
        if warnings:
            item['warning'] = ' '.join(filter(None, [item.get('warning'), *warnings]))
        if progress:
            progress(index, len(items), f'Trained model: checked {item["code"]}, {len(item.get("rows", []))} rows ({index}/{len(items)}).')
    return items
