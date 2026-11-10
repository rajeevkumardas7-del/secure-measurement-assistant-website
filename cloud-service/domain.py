"""Validated measurement arithmetic and isolated persistent job records."""
import ast
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation


def number(value, blank='0'):
    text = str(value if value is not None else '').strip()
    if not text:
        return Decimal(blank)
    if len(text) > 120:
        raise ValueError('Expression is too long.')
    tree = ast.parse(text, mode='eval')
    if len(list(ast.walk(tree))) > 60:
        raise ValueError('Expression is too complex.')
    def evaluate(node):
        if isinstance(node, ast.Expression): return evaluate(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return Decimal(ast.get_source_segment(text, node))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return evaluate(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Add): return a + b
            if isinstance(node.op, ast.Sub): return a - b
            if isinstance(node.op, ast.Mult): return a * b
            if b == 0: raise ValueError('Division by zero.')
            return a / b
        raise ValueError('Use numbers, brackets and + - * / only.')
    try: result = evaluate(tree)
    except (InvalidOperation, ArithmeticError) as exc: raise ValueError('Invalid arithmetic.') from exc
    if not result.is_finite() or abs(result) > Decimal('1e15'):
        raise ValueError('Number is outside the supported range.')
    return result


def calculate(payload):
    rows = payload.get('rows')
    if not isinstance(rows, list) or len(rows) > 2000:
        raise ValueError('Supply at most 2,000 rows.')
    result = []; total = Decimal(0); labour = Decimal(0)
    for index, row in enumerate(rows):
        if not isinstance(row, dict): raise ValueError('Invalid row.')
        code = str(row.get('code', '')).strip()
        if not code or len(code) > 128: raise ValueError(f'Row {index+1}: item code required.')
        # Never combine repeated item codes or repeated serial numbers.
        qty = number(row.get('quantity'))
        rate = number(row.get('rate')); wage = number(row.get('labourRate'))
        if rate < 0 or wage < 0: raise ValueError('Rates cannot be negative.')
        if row.get('remaining') not in (None, '') and qty > number(row['remaining']):
            raise ValueError(f'Row {index+1}: quantity exceeds remaining quantity.')
        amount = qty * rate; labour_amount = qty * wage
        total += amount; labour += labour_amount
        result.append({**{k: str(row.get(k, '')) for k in ('code','name','quantity','rate','labourRate','remaining')},
                       'serial': index+1, 'calculatedQuantity': str(qty), 'deduction': qty < 0,
                       'amount': str(amount), 'labourAmount': str(labour_amount)})
    days = number(payload.get('personDays')); daily = number(payload.get('dailyWage'))
    if days < 0 or daily < 0: raise ValueError('Person days and wage must be nonnegative.')
    maximum = days * daily
    return {'rows':result, 'total':str(total), 'labourTotal':str(labour),
            'maximumLabour':str(maximum), 'perPersonDay':str(labour/days) if days else None,
            'overLimit': bool(days and daily and labour > maximum)}


class Jobs:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL, module TEXT NOT NULL, payload TEXT NOT NULL, created INTEGER NOT NULL)')
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path); db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()
    def create(self, owner, module, payload):
        if module not in ('secure','emb'): raise ValueError('Unknown workspace.')
        calculate(payload)
        job = str(uuid.uuid4())
        with self.connect() as db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)', (job,owner,'awaiting_browser',module,json.dumps(payload),int(time.time())))
        return self.get(owner,job)
    def get(self,owner,job):
        with self.connect() as db:
            row = db.execute('SELECT id,state,module,created FROM jobs WHERE owner=? AND id=?',(owner,job)).fetchone()
        if not row: raise LookupError('Job not found.')
        return dict(row)
    def cancel(self,owner,job):
        self.get(owner,job)
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='cancelled' WHERE owner=? AND id=? AND state='awaiting_browser'",(owner,job))
        return self.get(owner,job)
