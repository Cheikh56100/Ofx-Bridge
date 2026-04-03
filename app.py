# -*- coding: utf-8 -*-
"""
OFX Bridge — Interface Streamlit
Convertisseur de relevés bancaires PDF vers OFX
Supporte : Qonto, LCL, CA, CE, BP, CIC, CGD, LBP, SG, BNP, myPOS, Shine,
           CBAO, Ecobank, BCI, Coris, UBA, Orabank, BOA, ATB, BSIC, BIS, BNDE
"""

import io
import re
import hashlib
import logging
import tempfile
import os
from datetime import datetime
from pathlib import Path

import streamlit as st

# ── Logging minimal (Streamlit gère son propre stdout) ───────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(funcName)s — %(message)s")
logger = logging.getLogger("ofxbridge")

# ── Imports avec gestion d'erreur propre ─────────────────────────────────────
try:
    import pdfplumber
    _PDFPLUMBER_OK = True
except ImportError:
    _PDFPLUMBER_OK = False

_OCR_AVAILABLE = False
try:
    import pytesseract
    from pdf2image import convert_from_path
    _OCR_AVAILABLE = True
except ImportError:
    pass

try:
    from pydantic import BaseModel, field_validator
    _PYDANTIC_OK = True
except ImportError:
    _PYDANTIC_OK = False

# ════════════════════════════════════════════════════════════════════════════
# MODÈLE PYDANTIC
# ════════════════════════════════════════════════════════════════════════════
if _PYDANTIC_OK:
    class Transaction(BaseModel):
        date:   str
        type:   str
        amount: float
        name:   str
        memo:   str = ""
        fitid:  str

        @field_validator('date')
        @classmethod
        def date_must_be_8_digits(cls, v):
            if not re.match(r'^\d{8}$', v):
                raise ValueError(f"Date OFX invalide: '{v}'")
            return v

        @field_validator('type')
        @classmethod
        def type_must_be_valid(cls, v):
            if v not in ('CREDIT', 'DEBIT'):
                raise ValueError(f"Type invalide: '{v}'")
            return v

        @field_validator('amount')
        @classmethod
        def amount_must_be_nonzero(cls, v):
            if v == 0.0:
                raise ValueError("Montant nul détecté")
            return v


# ════════════════════════════════════════════════════════════════════════════
# UTILITAIRES COMMUNS (identiques à la version Tkinter)
# ════════════════════════════════════════════════════════════════════════════

def extract_words_by_page(pdf_path):
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_words(keep_blank_chars=False))
    return pages

def extract_text_by_page(pdf_path):
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return pages

def parse_amount(s):
    s = s.replace('\xa0','').replace(' ','').replace('*','').strip()
    if re.match(r'^\d{1,3}(\.\d{3})*,\d{2}$', s):
        return float(s.replace('.','').replace(',','.'))
    if re.match(r'^\d+,\d{2}$', s):
        return float(s.replace(',','.'))
    if re.match(r'^\d+\.\d{2}$', s):
        return float(s)
    cleaned = re.sub(r'[^\d,.]', '', s)
    cleaned = cleaned.replace(',', '.')
    try:
        return float(cleaned)
    except ValueError:
        return None

def group_words_by_row(words, tol=3.0):
    if not words:
        return []
    rows, cur, top = [], [words[0]], words[0]['top']
    for w in words[1:]:
        if abs(w['top'] - top) <= tol:
            cur.append(w)
        else:
            rows.append(sorted(cur, key=lambda x: x['x0']))
            cur, top = [w], w['top']
    if cur:
        rows.append(sorted(cur, key=lambda x: x['x0']))
    return sorted(rows, key=lambda r: r[0]['top'])

def clean_label(s):
    return re.sub(r'\s+', ' ', s).strip()

def _is_technical_label(label):
    if not label:
        return True
    if re.match(r'^\d{6}\s+CB\*+\d+\s+\w+\s*$', label):
        return True
    if not re.search(r'[A-Za-zÀ-ÿ]{3,}', label):
        return True
    return False

def _is_human_readable(label):
    if not label:
        return False
    if re.search(r'[A-Z0-9]{15,}', label):
        return False
    if re.match(r'^[\d\s\-\/.,]+$', label):
        return False
    readable_words = [w for w in label.split() if re.search(r'[A-Za-zÀ-ÿ]{2,}', w)
                      and not re.match(r'^\d', w)]
    return len(readable_words) >= 2

def smart_label(main_label, memo_lines):
    label = clean_label(main_label)
    memos = [clean_label(m) for m in memo_lines if clean_label(m)]
    if _is_technical_label(label) and memos:
        for candidate in memos:
            if _is_human_readable(candidate):
                remaining = ' | '.join(m for m in memos if m != candidate and m)
                return candidate, (label + (' | ' + remaining if remaining else ''))
        return label, ' | '.join(memos)
    return label, ' | '.join(memos)

def make_fitid(date, label, amount):
    return hashlib.md5(f"{date}{label}{amount:.2f}".encode()).hexdigest()

def date_jjmm_to_ofx(jjmm, year):
    p = jjmm.replace('.', '/').split('/')
    if len(p) == 2:
        return f"{year}{p[1].zfill(2)}{p[0].zfill(2)}"
    return f"{year}0101"

def date_full_to_ofx(date_str):
    date_str = date_str.replace('.', '/')
    p = date_str.split('/')
    if len(p) == 3:
        return f"{p[2]}{p[1].zfill(2)}{p[0].zfill(2)}"
    return datetime.now().strftime('%Y%m%d')

def extract_iban(text):
    m = re.search(r'IBAN\s*:?\s*((?:[A-Z]{2}\d{2}[\s\d]+))', text)
    if m:
        return re.sub(r'\s+', '', m.group(1)).strip()
    return ''

def iban_to_rib(iban):
    c = iban.replace(' ', '').upper()
    if c.startswith('FR') and len(c) == 27:
        r = c[4:]
        return r[0:5], r[5:10], r[10:21]
    return '99999', '00001', c[-11:] if len(c) >= 11 else c

def _year_from_text(text):
    m = re.search(r'\b(20\d{2})\b', text)
    return int(m.group(1)) if m else datetime.now().year

def _parse_col_amount(words):
    if not words:
        return None
    full = ' '.join(w['text'] for w in words).replace('\xa0', ' ').strip()
    if full in ('.', ',', ''):
        return None
    m = re.search(r'(\d{1,3}(?:[.\s]\d{3})+,\d{2})', full)
    if m:
        val = parse_amount(m.group(1).replace(' ', '.'))
        if val is not None and val > 0:
            return val
    m2 = re.search(r'(\d+,\d{2})', full)
    if m2:
        val = parse_amount(m2.group(1))
        if val is not None and val > 0:
            return val
    return None

def _parse_signed_amount(words):
    if not words:
        return None
    full = ' '.join(w['text'] for w in words).replace('\xa0', ' ').strip()
    m = re.search(r'([+\-])\s*([\d\s]+[,.][\d]{2})', full)
    if m:
        sign = 1.0 if m.group(1) == '+' else -1.0
        val = parse_amount(m.group(2))
        if val is not None:
            return sign * val
    m2 = re.search(r'([\d\s]+[,.][\d]{2})', full)
    if m2:
        val = parse_amount(m2.group(1))
        if val is not None:
            return val
    return None

def _make_txn(date_ofx, amount, label, memo=''):
    txn_dict = {
        'date':   date_ofx,
        'type':   'CREDIT' if amount >= 0 else 'DEBIT',
        'amount': amount,
        'name':   clean_label(label)[:64],
        'memo':   clean_label(memo)[:128],
        'fitid':  make_fitid(date_ofx, label, amount)
    }
    if _PYDANTIC_OK:
        try:
            Transaction(**txn_dict)
        except Exception as exc:
            logger.warning("Transaction ignorée [%s | %s | %.2f] : %s",
                           date_ofx, label[:40], amount, exc)
            return None
    return txn_dict

def _pdf_has_text(pages_text, min_chars=50):
    total = sum(len(p.strip()) for p in pages_text)
    return total >= min_chars

def _ocr_pdf(pdf_path):
    if not _OCR_AVAILABLE:
        raise RuntimeError(
            "Ce PDF semble scanné (aucun texte extractible). "
            "Les outils OCR (pytesseract, pdf2image, Tesseract) ne sont pas installés sur ce serveur."
        )
    images = convert_from_path(pdf_path, dpi=300)
    return [pytesseract.image_to_string(img, lang='fra+eng') for img in images]


# ════════════════════════════════════════════════════════════════════════════
# DÉTECTION DE LA BANQUE
# ════════════════════════════════════════════════════════════════════════════

def detect_bank(pages_text):
    text = pages_text[0][:3000].upper()
    if 'QONTO' in text or 'QNTOFRP' in text:
        return 'QONTO'
    if 'CREDIT LYONNAIS' in text or ('LCL' in text and 'RELEVE DE COMPTE COURANT' in text):
        return 'LCL'
    text_nospace = text.replace(' ', '')
    if ('SOCIETE GENERALE' in text or 'SOCIÉTÉ GÉNÉRALE' in text
            or 'SOCIETEGENERALE' in text_nospace) and (
            'SENEGAL' in text or 'SÉNÉGAL' in text or 'COTE D' in text
            or "CÔTE D'" in text or 'CAMEROUN' in text or 'DAKAR' in text
            or 'ABIDJAN' in text or 'DOUALA' in text or 'LOME' in text
            or 'BAMAKO' in text):
        return 'SG_AFRIQUE'
    if ('SOCIETE GENERALE' in text or 'SOCIÉTÉ GÉNÉRALE' in text
            or '552 120 222' in text or 'SOCIETEGENERALE' in text_nospace
            or 'SG.FR' in text or 'PROFESSIONNELS.SG.FR' in text):
        return 'SG'
    if 'CREDIT AGRICOLE' in text or 'AGRIFRPP' in text:
        return 'CA'
    if 'CAIXA GERAL' in text or 'CGDIFRPP' in text or 'CGD' in text[:500]:
        return 'CGD'
    if "CAISSE D'EPARGNE" in text or "CAISSE D.EPARGNE" in text or 'CEPAFRPP' in text:
        return 'CE'
    if 'BANQUE POPULAIRE' in text or 'CCBPFRPP' in text:
        return 'BP'
    if 'BANQUE POSTALE' in text or 'PSSTFRPP' in text or 'LABANQUEPOSTALE' in text:
        return 'LBP'
    if 'CREDIT INDUSTRIEL' in text or 'CMCIFRPP' in text or ('CIC' in text and 'RELEVE' in text):
        return 'CIC'
    if ('BNP PARIBAS' in text or 'BNPAFRPP' in text or 'BNP' in text[:500]
            or 'BANQUE NATIONALE DE PARIS' in text):
        return 'BNP'
    if 'MYPOS' in text or 'MYPOS LTD' in text or 'MY POS' in text:
        return 'MYPOS'
    if ('SNNNFR22XXX' in text or 'SHINE.FR' in text or 'SHINE SAS' in text
            or ('SHINE' in text and ('RELEVE' in text or 'SNNN' in text or '1741' in text))):
        return 'SHINE'
    if 'CBAO' in text or 'COMPAGNIE BANCAIRE DE L' in text:
        return 'CBAO'
    if 'ECOBANK' in text or 'ECOBANK SENEGAL' in text:
        return 'ECOBANK'
    if 'BANQUE POUR LE COMMERCE' in text and 'INDUSTRIE' in text:
        return 'BCI'
    if 'CORIS BANK' in text or 'CORISBANK' in text_nospace:
        return 'CORIS'
    if 'UNITED BANK FOR AFRICA' in text or 'UNAFSNDA' in text or ('UBA' in text[:400] and 'BANK' in text):
        return 'UBA'
    if 'ORABANK' in text:
        return 'ORABANK'
    if 'BANK OF AFRICA' in text:
        return 'BOA'
    if 'ARAB TUNISIAN BANK' in text:
        return 'ATB'
    if ('BSIC' in text or 'BANQUE SAHELO' in text or 'SN08SN111' in text_nospace):
        return 'BSIC'
    if ('BANQUE ISLAMIQUE DU SENEGAL' in text or 'ISLAMIQUE' in text and 'SENEGAL' in text):
        return 'BIS'
    if 'BNDE' in text or 'BANQUE NATIONALE POUR LE DEVELOPPEMENT' in text:
        return 'BNDE'
    return 'UNIVERSAL'


# ════════════════════════════════════════════════════════════════════════════
# PARSEURS BANCAIRES (identiques à la version Tkinter — non modifiés)
# ════════════════════════════════════════════════════════════════════════════

def parse_qonto(pages_words, pages_text):
    info = _extract_qonto_header(pages_text[0])
    year = int(info['period_start'].split('/')[2]) if info.get('period_start') else _year_from_text(pages_text[0])
    txns = []
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _qonto_date(row)
            if not date_str:
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 130 <= w['x0'] < 410).strip()
            amount = _qonto_amount(row)
            memo = ''
            j = i + 1
            while j < len(rows) and not _qonto_date(rows[j]):
                nl = ' '.join(w['text'] for w in rows[j] if 130 <= w['x0'] < 410).strip()
                na = _qonto_amount(rows[j])
                if na is not None and amount is None:
                    amount = na; memo = nl; j += 1; break
                elif na is None and nl:
                    memo = nl; j += 1; break
                else:
                    break
            i = j
            if amount is None or not label or label in ('Transactions', 'Date de valeur'):
                continue
            memo_clean = memo if memo.strip() not in ('', '-', '+') else ''
            name, memo_out = smart_label(label, [memo_clean] if memo_clean else [])
            txns.append(_make_txn(date_jjmm_to_ofx(date_str, year), amount, name, memo_out))
    return info, [t for t in txns if t is not None]

def _qonto_date(row):
    for w in row:
        if w['x0'] < 120 and re.match(r'^\d{2}/\d{2}$', w['text']):
            return w['text']
    return ''

def _qonto_amount(row):
    aw = [w for w in row if w['x0'] >= 400]
    if not aw: return None
    full = ' '.join(w['text'] for w in aw).replace('EUR','').replace('\xa0',' ').strip()
    m = re.search(r'([+\-])\s*([\d\s]+[.,]\d{2})', full)
    if m:
        sign = 1.0 if m.group(1)=='+' else -1.0
        try: return sign * float(m.group(2).replace(' ','').replace(',','.'))
        except: pass
    m2 = re.search(r'([\d\s]+[.,]\d{2})', full)
    if m2:
        sign = 1.0
        for w in aw:
            if w['text'] in ('+','-'): sign = 1.0 if w['text']=='+' else -1.0; break
            sm = re.match(r'^([+\-])([\d,.]+)$', w['text'])
            if sm: sign = 1.0 if sm.group(1)=='+' else -1.0; break
        try: return sign * float(m2.group(1).replace(' ','').replace(',','.'))
        except: pass
    return None

def _extract_qonto_header(text):
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    m = re.search(r'Du\s+(\d{2}/\d{2}/\d{4})\s+au\s+(\d{2}/\d{2}/\d{4})', text)
    if m: info['period_start'], info['period_end'] = m.group(1), m.group(2)
    bals = re.findall(r'Solde au \d{2}/\d{2}\s*[+\-]\s*([\d]+\.[\d]{2})\s*EUR', text)
    if len(bals) >= 1: info['balance_open']  = float(bals[0])
    if len(bals) >= 2: info['balance_close'] = float(bals[-1])
    return info

def parse_lcl(pages_words, pages_text):
    info = _extract_lcl_header(pages_text)
    year = _year_from_text(pages_text[0])
    txns = []
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _lcl_date(row)
            if not date_str:
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 70 <= w['x0'] < 360).strip()
            debit_words = [w for w in row if 360 <= w['x0'] < 490
                           and not re.match(r'^\d{2}\.\d{2}(\.\d{2,4})?$', w['text'])]
            debit_amt  = _parse_col_amount(debit_words)
            credit_amt = _parse_col_amount([w for w in row if w['x0'] >= 490])
            memo = ''
            j = i + 1
            while j < len(rows) and not _lcl_date(rows[j]):
                nl = ' '.join(w['text'] for w in rows[j] if 70 <= w['x0'] < 360).strip()
                if nl and nl not in ('DEBIT','CREDIT','VALEUR','DATE','LIBELLE','ANCIEN SOLDE'):
                    memo = (memo + ' ' + nl).strip()
                j += 1
            i = j
            if not label or label in ('DEBIT','CREDIT','VALEUR','DATE','LIBELLE','ANCIEN SOLDE'):
                continue
            date_ofx = date_jjmm_to_ofx(date_str, year)
            name, memo_out = smart_label(label, [memo] if memo else [])
            if debit_amt is not None:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo_out))
            elif credit_amt is not None:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo_out))
    return info, [t for t in txns if t is not None]

def _lcl_date(row):
    for w in row:
        if w['x0'] < 100 and re.match(r'^\d{2}\.\d{2}$', w['text']):
            return w['text']
    return ''

def _extract_lcl_header(pages_text):
    text = ' '.join(pages_text[:2])
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    m = re.search(r'du\s+(\d{2}\.\d{2}\.\d{4})\s+au\s+(\d{2}\.\d{2}\.\d{4})', text, re.IGNORECASE)
    if m:
        info['period_start'] = m.group(1).replace('.','/')
        info['period_end']   = m.group(2).replace('.','/')
    return info

def _ca_parse_zone(row, x_min, x_max):
    col = [w for w in row if x_min <= w['x0'] < x_max and re.match(r'^\d', w['text'])]
    if not col: return None
    last = col[-1]['text']
    if not re.match(r'^\d+,\d{2}$', last): return None
    if len(col) == 1: return parse_amount(last)
    prefix_tokens = [w['text'] for w in col[:-1]]
    if all(re.match(r'^\d+$', p) for p in prefix_tokens):
        try:
            return float(''.join(prefix_tokens) + last.replace(',', '.'))
        except ValueError:
            pass
    return None

def parse_ca(pages_words, pages_text):
    info = _extract_ca_header(pages_text)
    year = _year_from_text(pages_text[0])
    txns = []
    SKIP = {'Débit','Crédit','Date','Libellé','Total des opérations','Nouveau solde',
            'opé.','valeur','Libellé des opérations','Ancien solde débiteur','Nouveau solde débiteur'}
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _ca_date(row)
            if not date_str:
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 70 <= w['x0'] < 420).strip()
            debit_amt  = _ca_parse_zone(row, 415, 490)
            credit_amt = _ca_parse_zone(row, 490, 560)
            memo_parts = []
            j = i + 1
            while j < len(rows) and not _ca_date(rows[j]):
                nl = ' '.join(w['text'] for w in rows[j] if 70 <= w['x0'] < 420).strip()
                if nl and nl not in SKIP and len(nl) > 1:
                    memo_parts.append(nl)
                j += 1
            i = j
            if not label or label in SKIP: continue
            date_ofx = date_jjmm_to_ofx(date_str, year)
            name, memo = smart_label(label, memo_parts)
            if debit_amt is not None:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt is not None:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))
    return info, [t for t in txns if t is not None]

def _ca_date(row):
    for w in row:
        if w['x0'] < 50 and re.match(r'^\d{2}\.\d{2}$', w['text']):
            return w['text']
    return ''

def _extract_ca_header(pages_text):
    text = ' '.join(pages_text[:2])
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    mois_map = {'janvier':'01','février':'02','mars':'03','avril':'04','mai':'05','juin':'06',
                'juillet':'07','août':'08','septembre':'09','octobre':'10','novembre':'11','décembre':'12'}
    m = re.search(r'Date d.arrêté\s*:\s*(\d+)\s+(\w+)\s+(\d{4})', text)
    if m:
        mn = mois_map.get(m.group(2).lower(), '01')
        info['period_end']   = f"{m.group(1).zfill(2)}/{mn}/{m.group(3)}"
        info['period_start'] = f"01/{mn}/{m.group(3)}"
    return info

def parse_ce(pages_words, pages_text):
    info = _extract_ce_header(pages_text)
    txns = []
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _ce_date(row)
            if not date_str:
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 155 <= w['x0'] < 500).strip()
            amount = _parse_signed_amount([w for w in row if w['x0'] >= 500])
            memo = ''
            j = i + 1
            while j < len(rows) and not _ce_date(rows[j]):
                nl = ' '.join(w['text'] for w in rows[j] if 155 <= w['x0'] < 500).strip()
                if nl and len(nl) > 2:
                    memo = (memo + ' ' + nl).strip()
                j += 1
            i = j
            if not label or amount is None: continue
            skip_kw = {'DATE','VALEUR','MONTANT','OPERATIONS','SOLDE','TOTAL','DETAIL'}
            if any(s in label.upper() for s in skip_kw): continue
            date_ofx = date_full_to_ofx(date_str)
            name, memo_out = smart_label(label, [memo] if memo else [])
            txns.append(_make_txn(date_ofx, amount, name, memo_out))
    return info, [t for t in txns if t is not None]

def _ce_date(row):
    for w in row:
        if w['x0'] < 100 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text']):
            return w['text']
    return ''

def _extract_ce_header(pages_text):
    text = ' '.join(pages_text[:2])
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    return info

def parse_bp(pages_words, pages_text):
    info = _extract_bp_header(pages_text)
    year = _year_from_text(pages_text[0])
    txns = []
    SKIP_KW = {'DATE','LIBELLE','REFERENCE','COMPTA','VALEUR','MONTANT','SOLDE','TOTAL','DETAIL','OPERATION'}
    for pw in pages_words:
        rows = group_words_by_row(pw)
        skip_from = None
        for idx, row in enumerate(rows):
            row_text = ' '.join(w['text'] for w in row).upper()
            if 'DETAIL DE VOS MOUVEMENTS SEPA' in row_text or 'DETAIL DE VOS PRELEVEMENTS SEPA' in row_text:
                skip_from = idx; break
        i = 0
        while i < len(rows):
            if skip_from is not None and i >= skip_from: break
            row = rows[i]
            date_str = _bp_date(row)
            if not date_str:
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 80 <= w['x0'] < 355).strip()
            amount = _bp_amount([w for w in row if w['x0'] >= 490])
            memo_parts = []
            j = i + 1
            while j < len(rows) and not _bp_date(rows[j]):
                if skip_from is not None and j >= skip_from: break
                nl = ' '.join(w['text'] for w in rows[j] if 80 <= w['x0'] < 355).strip()
                if nl and len(nl) > 2 and not re.match(r'^[\d\s.,€%=\-EUR]+$', nl):
                    memo_parts.append(nl)
                j += 1
            i = j
            if not label or amount is None: continue
            if any(s in label.upper() for s in SKIP_KW): continue
            date_ofx = date_jjmm_to_ofx(date_str, year)
            name, memo = smart_label(label, memo_parts)
            txns.append(_make_txn(date_ofx, amount, name, memo))
    return info, [t for t in txns if t is not None]

def _bp_date(row):
    for w in row:
        if w['x0'] < 80 and re.match(r'^\d{2}/\d{2}$', w['text']):
            return w['text']
    return ''

def _bp_amount(words):
    if not words: return None
    full = ' '.join(w['text'] for w in words).replace('€','').replace('\xa0',' ').strip()
    m = re.search(r'-\s*([\d\s]+[,.][\d]{2})', full)
    if m:
        try: return -abs(float(m.group(1).replace(' ','').replace(',','.')))
        except: pass
    m2 = re.search(r'\+\s*([\d\s]+[,.][\d]{2})', full)
    if m2:
        try: return abs(float(m2.group(1).replace(' ','').replace(',','.')))
        except: pass
    m3 = re.search(r'([\d\s]+[,.][\d]{2})', full)
    if m3:
        try:
            val = float(m3.group(1).replace(' ','').replace(',','.'))
            return val if val > 0 else None
        except: pass
    return None

def _extract_bp_header(pages_text):
    text = pages_text[0] if pages_text else ''
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    return info

def parse_cic(pages_words, pages_text):
    info = _extract_cic_header(pages_text)
    txns = []
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _cic_date(row)
            if not date_str:
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 140 <= w['x0'] < 430).strip()
            debit_amt  = _parse_col_amount([w for w in row if 420 <= w['x0'] < 500])
            credit_amt = _parse_col_amount([w for w in row if w['x0'] >= 500])
            memo = ''
            j = i + 1
            while j < len(rows) and not _cic_date(rows[j]):
                nl = ' '.join(w['text'] for w in rows[j] if 140 <= w['x0'] < 430).strip()
                if nl and len(nl) > 2 and not re.match(r'^[\d.,]+$', nl):
                    memo = (memo + ' ' + nl).strip()
                j += 1
            i = j
            if not label: continue
            skip_kw = {'DATE','DÉBIT','CRÉDIT','EUROS','SOLDE CREDITEUR','CREDIT INDUSTRIEL','TOTAL DES MOUVEMENTS'}
            if any(s in label.upper() for s in skip_kw): continue
            date_ofx = date_full_to_ofx(date_str)
            name, memo_out = smart_label(label, [memo] if memo else [])
            if debit_amt is not None:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo_out))
            elif credit_amt is not None:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo_out))
    return info, [t for t in txns if t is not None]

def _cic_date(row):
    for w in row:
        if w['x0'] < 100 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text']):
            return w['text']
    return ''

def _extract_cic_header(pages_text):
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    for pt in reversed(pages_text):
        iban = extract_iban(pt)
        if iban:
            info['iban'] = iban; break
    return info

def parse_cgd(pages_words, pages_text):
    info = _extract_cgd_header(pages_text)
    year = _year_from_text(pages_text[0])
    txns = []
    SKIP = {'A REPORTER','REPORT','TOTAL','NOUVEAU','ANCIEN','SARL','CPT ORD'}
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            if not (len(row) >= 2
                    and re.match(r'^\d{2}$', row[0]['text']) and row[0]['x0'] < 50
                    and re.match(r'^\d{2}$', row[1]['text']) and row[1]['x0'] < 55):
                i += 1; continue
            dd, mm = row[0]['text'], row[1]['text']
            label = ' '.join(w['text'] for w in row if 70 <= w['x0'] < 310).strip()
            if not label or any(s in label.upper() for s in SKIP):
                i += 1; continue
            debit_amt  = _cgd_amount_in_zone(row, 395, 500)
            credit_amt = _cgd_amount_in_zone(row, 500, 570)
            memo_parts = []
            j = i + 1
            while j < len(rows):
                r2 = rows[j]
                if len(r2) >= 2 and re.match(r'^\d{2}$', r2[0]['text']) and r2[0]['x0'] < 50: break
                nl = ' '.join(w['text'] for w in r2 if 70 <= w['x0'] < 310).strip()
                if nl: memo_parts.append(nl)
                j += 1
            i = j
            date_ofx = f"{year}{mm.zfill(2)}{dd.zfill(2)}"
            name, memo = smart_label(label, memo_parts)
            if debit_amt:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))
    return info, [t for t in txns if t is not None]

def _cgd_amount_in_zone(row, x_min, x_max):
    col = [w for w in row if x_min <= w['x0'] < x_max and re.match(r'^\d', w['text'])]
    if not col: return None
    return parse_amount(col[-1]['text'])

def _extract_cgd_header(pages_text):
    text = ' '.join(pages_text[:2])
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    return info

def parse_lbp(pages_words, pages_text):
    info = _extract_lbp_header(pages_text)
    year = _year_from_text(pages_text[0])
    txns = []
    SKIP = {'TOTAL DES','NOUVEAU SOLDE','ANCIEN SOLDE','VOS OPERATIONS','DATE OPERATION','SITUATION DU','PAGE'}
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            if not (row[0]['x0'] < 60 and re.match(r'^\d{2}/\d{2}$', row[0]['text'])):
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 85 <= w['x0'] < 430).strip()
            label = re.sub(r'\(cid:\d+\)', '', label).strip()
            if not label or any(s in label.upper() for s in SKIP):
                i += 1; continue
            debit_amt  = _lbp_amount_in_zone(row, 430, 500)
            credit_amt = _lbp_amount_in_zone(row, 500, 560)
            j = i + 1
            while j < len(rows):
                r2 = rows[j]
                if r2[0]['x0'] < 60 and re.match(r'^\d{2}/\d{2}$', r2[0]['text']): break
                j += 1
            i = j
            date_ofx = f"{year}{row[0]['text'][3:5]}{row[0]['text'][:2]}"
            name, memo = smart_label(label, [])
            if debit_amt:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))
    return info, [t for t in txns if t is not None]

def _lbp_amount_in_zone(row, x_min, x_max):
    col = [w for w in row if x_min <= w['x0'] < x_max and re.match(r'^\d', w['text'])]
    if not col: return None
    last = col[-1]['text']
    if not re.match(r'^\d+,\d{2}$', last): return None
    if len(col) == 1: return parse_amount(last)
    prefix_tokens = [w['text'] for w in col[:-1]]
    if all(re.match(r'^\d+$', p) for p in prefix_tokens):
        try: return float(''.join(prefix_tokens) + last.replace(',', '.'))
        except: pass
    return parse_amount(last)

def _extract_lbp_header(pages_text):
    text = re.sub(r'\(cid:\d+\)', ' ', ' '.join(pages_text[:2]))
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    return info

def parse_sg(pages_words, pages_text):
    info = _extract_sg_header(pages_text)
    txns = []
    SKIP = {'TOTAUX DES','NOUVEAU SOLDE','SOLDE PRECEDENT','PROGRAMME DE','RAPPEL DES','MONTANT CUMULE'}
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            if not (row[0]['x0'] < 45 and re.match(r'^\d{2}/\d{2}/\d{4}$', row[0]['text'])):
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 120 <= w['x0'] < 430).strip()
            if not label or any(s in label.upper() for s in SKIP):
                i += 1; continue
            debit_amt  = _sg_amount_in_zone(row, 430, 510)
            credit_amt = _sg_amount_in_zone(row, 510, 570)
            memo_parts = []
            j = i + 1
            while j < len(rows):
                r2 = rows[j]
                if r2[0]['x0'] < 45 and re.match(r'^\d{2}/\d{2}/\d{4}$', r2[0]['text']): break
                nl = ' '.join(w['text'] for w in r2 if 120 <= w['x0'] < 430).strip()
                if nl and not any(s in nl.upper() for s in ('TOTAUX','NOUVEAU','PROGRAMME','RAPPEL')):
                    memo_parts.append(nl)
                j += 1
            i = j
            date_ofx = date_full_to_ofx(row[0]['text'])
            name, memo = smart_label(label, memo_parts)
            if debit_amt:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))
    return info, [t for t in txns if t is not None]

def _sg_amount_in_zone(row, x_min, x_max):
    col = [w for w in row if x_min <= w['x0'] < x_max]
    if not col: return None
    return parse_amount(col[-1]['text'])

def _extract_sg_header(pages_text):
    text = ' '.join(pages_text[:2])
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    m = re.search(r'du\s+(\d{2}/\d{2}/\d{4})\s+au\s+(\d{2}/\d{2}/\d{4})', text, re.IGNORECASE)
    if m:
        info['period_start'] = m.group(1); info['period_end'] = m.group(2)
    return info

def parse_bnp(pages_words, pages_text):
    info = _extract_bnp_header(pages_text)
    year = _year_from_text(' '.join(pages_text[:2]))
    txns = []
    SKIP = {'DATE','LIBELLE','VALEUR','DEBIT','CREDIT','EUROS','SOLDE','TOTAL','OPERATIONS',
            'ANCIEN SOLDE','NOUVEAU SOLDE','VIREMENT RECU','RELEVE DE COMPTE'}
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _bnp_date(row)
            if not date_str:
                i += 1; continue
            label = ' '.join(w['text'] for w in row if 85 <= w['x0'] < 430).strip()
            if not label or any(s in label.upper() for s in SKIP):
                i += 1; continue
            debit_amt  = _parse_col_amount([w for w in row if 480 <= w['x0'] < 560])
            credit_amt = _parse_col_amount([w for w in row if w['x0'] >= 560])
            memo_parts = []
            j = i + 1
            while j < len(rows) and not _bnp_date(rows[j]):
                nl = ' '.join(w['text'] for w in rows[j] if 85 <= w['x0'] < 430).strip()
                if nl and len(nl) > 2:
                    memo_parts.append(nl)
                j += 1
            i = j
            date_ofx = _bnp_date_to_ofx(date_str, year)
            name, memo = smart_label(label, memo_parts)
            if debit_amt is not None:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt is not None:
                txns.append(_make_txn(date_ofx, credit_amt, name, memo))
    return info, [t for t in txns if t is not None]

def _bnp_date(row):
    for w in row:
        if w['x0'] < 80:
            if re.match(r'^\d{2}/\d{2}/\d{2}$', w['text']): return w['text']
            if re.match(r'^\d{2}/\d{2}/\d{4}$', w['text']): return w['text']
    return ''

def _bnp_date_to_ofx(date_str, year_hint):
    parts = date_str.split('/')
    if len(parts) == 3:
        dd, mm, yy = parts[0].zfill(2), parts[1].zfill(2), parts[2]
        if len(yy) == 2:
            full_year = (2000 + int(yy)) if int(yy) <= 30 else (1900 + int(yy))
        else:
            full_year = int(yy)
        return f"{full_year}{mm}{dd}"
    return str(year_hint) + '0101'

def _extract_bnp_header(pages_text):
    text = ' '.join(pages_text[:2])
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    return info

def parse_mypos(pages_words, pages_text):
    info = _extract_mypos_header(pages_text)
    txns = []
    full_text = '\n'.join(pages_text)
    lines = [l.strip() for l in full_text.splitlines()]
    txn_re = re.compile(
        r'^(\d{2}\.\d{2}\.\d{4})\s+\d{2}:\d{2}\s+'
        r'(System Fee|myPOS Payment|Glass Payment|Outgoing bank transfer|POS Payment|Mobile)\s*'
        r'.*?1\.0000\s+([\d,]+\.?\d*)\s+([\d,]+\.?\d*)\s*$'
    )
    for idx, line in enumerate(lines):
        m = txn_re.match(line)
        if not m: continue
        date_raw = m.group(1)
        txn_type = m.group(2).strip()
        try:
            debit_val  = float(m.group(3).replace(',', ''))
            credit_val = float(m.group(4).replace(',', ''))
        except ValueError:
            continue
        date_ofx = f"{date_raw[6:10]}{date_raw[3:5]}{date_raw[0:2]}"
        description = ''
        for back in (1, 2):
            if idx >= back:
                prev = lines[idx - back].strip()
                if prev and not re.match(r'^\d{2}\.\d{2}\.\d{4}', prev):
                    description = prev; break
        if txn_type == 'System Fee':
            name, memo = 'myPOS Fee', description
        else:
            name, memo = description or txn_type, description
        amount = -debit_val if debit_val > 0 else (credit_val if credit_val > 0 else None)
        if amount is None: continue
        txns.append(_make_txn(date_ofx, amount, name[:64], memo[:128]))
    return info, [t for t in txns if t is not None]

def _extract_mypos_header(pages_text):
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    text = pages_text[0] if pages_text else ''
    m = re.search(r'IBAN\s*:?\s*(IE\d{2}[A-Z0-9]+)', text)
    if m: info['iban'] = m.group(1).replace(' ','')
    m2 = re.search(r'Monthly statement\s*[-–]\s*(\d{2})\.(\d{4})', text, re.IGNORECASE)
    if m2:
        import calendar
        month, year = m2.group(1), m2.group(2)
        last_day = calendar.monthrange(int(year), int(month))[1]
        info['period_start'] = f"01/{month}/{year}"
        info['period_end']   = f"{last_day:02d}/{month}/{year}"
    return info

def parse_shine(pages_words, pages_text):
    info = _extract_shine_header(pages_text)
    txns = []
    SKIP = {'DATE','TYPE','OPÉRATION','OPERATION','DÉBIT','DEBIT','CRÉDIT','CREDIT',
            '(EURO)','SOLDE','TOTAL','NOUVEAU','COMMISSIONS','MOUVEMENTS','PAGE','LES','RELEVÉ'}
    for pw in pages_words:
        rows = group_words_by_row(pw)
        i = 0
        while i < len(rows):
            row = rows[i]
            date_str = _shine_date(row)
            if not date_str:
                i += 1; continue
            txn_type = ' '.join(w['text'] for w in row if 95 <= w['x0'] < 160).strip()
            label    = ' '.join(w['text'] for w in row if 160 <= w['x0'] < 453).strip()
            debit_amt  = _parse_col_amount([w for w in row if 453 <= w['x0'] < 513])
            credit_amt = _parse_col_amount([w for w in row if w['x0'] >= 513])
            memo_parts = []
            j = i + 1
            while j < len(rows) and not _shine_date(rows[j]):
                nl = ' '.join(w['text'] for w in rows[j] if 95 <= w['x0'] < 453).strip()
                if nl and len(nl) > 2:
                    memo_parts.append(nl)
                j += 1
            i = j
            full_label = (txn_type + ' ' + label).strip() if txn_type else label
            if any(s in full_label.upper() for s in SKIP) or len(full_label) < 2: continue
            date_ofx = date_full_to_ofx(date_str)
            name, memo = smart_label(full_label, memo_parts)
            if debit_amt is not None:
                txns.append(_make_txn(date_ofx, -debit_amt, name, memo))
            elif credit_amt is not None:
                txns.append(_make_txn(date_ofx,  credit_amt, name, memo))
    return info, [t for t in txns if t is not None]

def _shine_date(row):
    for w in row:
        if w['x0'] < 60 and re.match(r'^\d{2}/\d{2}/\d{4}$', w['text']):
            return w['text']
    return ''

def _extract_shine_header(pages_text):
    text = ' '.join(pages_text[:3])
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    info['iban'] = extract_iban(text)
    m = re.search(r'De\s+(\d{2}/\d{2}/\d{4})\s+[àa]\s+(\d{2}/\d{2}/\d{4})', text, re.IGNORECASE)
    if m:
        info['period_start'] = m.group(1); info['period_end'] = m.group(2)
    return info


# ════════════════════════════════════════════════════════════════════════════
# PARSEUR UNIVERSEL
# ════════════════════════════════════════════════════════════════════════════

_COL_SYNONYMS = {
    'date':   ['date','date opé','date opé.','date opération','date val','date valeur','date comptable','valeur','jour','date op'],
    'label':  ['libellé','libelle','opération','operation','description','motif','désignation','nature','détail','detail','mouvement','intitulé','label','wording','particulars','narration'],
    'debit':  ['débit','debit','débit (euro)','debit (euro)','sorties','sortie','retrait','retraits','paiements','débit fcfa','débit xof','withdrawals','withdrawal','payments','dr','déb','deb'],
    'credit': ['crédit','credit','crédit (euro)','credit (euro)','entrées','entrée','versement','versements','encaissements','crédit fcfa','crédit xof','deposits','deposit','receipts','cr','cré','cred'],
    'amount': ['montant','amount','somme','mouvement','débit/crédit','debit/credit','montant net','net'],
    'balance':['solde','balance','solde après','running balance'],
}

_DATE_PATTERNS = [
    (r'^(\d{2})[/\-\.](\d{2})[/\-\.](\d{4})$', 'dmy4'),
    (r'^(\d{4})[/\-\.](\d{2})[/\-\.](\d{2})$', 'ymd4'),
    (r'^(\d{2})[/\-\.](\d{2})[/\-\.](\d{2})$', 'dmy2'),
    (r'^(\d{2})[/\-\.](\d{2})$',                'dm'),
    (r'^(\d{8})$',                               'Ymd8'),
]

def _match_col(cell_text, col_type):
    if not cell_text: return False
    t = str(cell_text).strip().lower()
    for syn in _COL_SYNONYMS[col_type]:
        if t == syn or t.startswith(syn + ' ') or t.startswith(syn + '('): return True
    return False

def _detect_header_row(table):
    for row_idx, row in enumerate(table[:20]):
        col_map = {}
        for col_idx, cell in enumerate(row):
            if not cell: continue
            for ctype in ('date','label','debit','credit','amount','balance'):
                if ctype not in col_map and _match_col(str(cell), ctype):
                    col_map[ctype] = col_idx
        has_date  = 'date' in col_map
        has_money = ('debit' in col_map and 'credit' in col_map) or 'amount' in col_map
        if has_date and has_money:
            return row_idx, col_map
    return None, {}

def _parse_date_universal(raw, year_hint=None):
    if not raw: return None
    raw = str(raw).strip()
    raw = re.sub(r'^[A-Za-zÀ-ÿ]+\.?\s*', '', raw).strip()
    raw = raw.split('\n')[0].strip()
    for pattern, fmt in _DATE_PATTERNS:
        m = re.match(pattern, raw)
        if not m: continue
        if fmt == 'dmy4': return f"{m.group(3)}{m.group(2).zfill(2)}{m.group(1).zfill(2)}"
        elif fmt == 'ymd4': return f"{m.group(1)}{m.group(2).zfill(2)}{m.group(3).zfill(2)}"
        elif fmt == 'dmy2':
            yy = int(m.group(3))
            return f"{2000+yy if yy<=30 else 1900+yy}{m.group(2).zfill(2)}{m.group(1).zfill(2)}"
        elif fmt == 'dm':
            yr = str(year_hint) if year_hint else str(datetime.now().year)
            return f"{yr}{m.group(2).zfill(2)}{m.group(1).zfill(2)}"
        elif fmt == 'Ymd8':
            s = m.group(0); return f"{s[:4]}{s[4:6]}{s[6:8]}"
    return None

def _parse_amount_cell(cell_text):
    if not cell_text: return None
    s = str(cell_text).strip().replace('\xa0',' ').replace('\u202f',' ').replace('\n',' ').strip()
    s = re.sub(r'[€$£FCFAXOF]','',s,flags=re.IGNORECASE).strip().replace('*','').strip()
    if not s or s in ('.', ',', '-', '—', '–', ''): return None
    negative = False
    if s.startswith('(') and s.endswith(')'): s = s[1:-1].strip(); negative = True
    if s.startswith('-'): negative = True; s = s[1:].strip()
    elif s.startswith('+'): s = s[1:].strip()
    s_nospace = s.replace(' ','')
    m = re.match(r'^(\d{1,3}(?:[.,]\d{3})+)[,.](\d{2})$', s_nospace)
    if m:
        integer_part = re.sub(r'[,.]','',m.group(1))
        val = float(f"{integer_part}.{m.group(2)}")
        return -val if negative else val
    m2 = re.match(r'^(\d+)[,.](\d{1,2})$', s_nospace)
    if m2:
        val = float(f"{m2.group(1)}.{m2.group(2)}")
        return -val if negative else val
    m3 = re.match(r'^\d[\d\s]*\d$|^\d$', s)
    if m3:
        val = float(s.replace(' ',''))
        return -val if negative else val
    return None

def _extract_universal_header(pages_text):
    text = ' '.join(pages_text[:3])
    info = {'iban':'','period_start':'','period_end':'','balance_open':0.0,'balance_close':0.0}
    m_iban = re.search(r'IBAN\s*:?\s*([A-Z]{2}\d{2}[\s\dA-Z]+)', text)
    if m_iban: info['iban'] = re.sub(r'\s+','',m_iban.group(1)).strip()[:34]
    m1 = re.search(
        r'(?:du|from|de|period[e]?\s*:?)\s*(\d{1,2}[/\-.]\d{2}[/\-.]\d{2,4})'
        r'\s*(?:au|to|[àa]|\-)\s*(\d{1,2}[/\-.]\d{2}[/\-.]\d{2,4})',
        text, re.IGNORECASE)
    if m1:
        info['period_start'] = m1.group(1).replace('-','/').replace('.','/') 
        info['period_end']   = m1.group(2).replace('-','/').replace('.','/')
    return info

def _universal_parse_path(pdf_path, pages_text):
    info = _extract_universal_header(pages_text)
    year_hint = _year_from_text(' '.join(pages_text[:2]))
    txns = []
    SKIP_LABELS = {'TOTAL','TOTAUX','SOLDE','SOUS-TOTAL','REPORT','A REPORTER',
                   'NOUVEAU SOLDE','ANCIEN SOLDE','SOLDE INITIAL','SOLDE FINAL'}
    TABLE_SETTINGS_LIST = [
        {"vertical_strategy":"text","horizontal_strategy":"text","snap_tolerance":4,"join_tolerance":4},
        {"vertical_strategy":"lines","horizontal_strategy":"lines","snap_tolerance":3},
        {"vertical_strategy":"lines","horizontal_strategy":"text","snap_tolerance":4},
    ]
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            table = None
            for settings in TABLE_SETTINGS_LIST:
                t = page.extract_table(settings)
                if t and len(t) >= 3:
                    table = t; break
            if not table: continue
            table_clean = [[str(c).replace('\n',' ').strip() if c else '' for c in row] for row in table]
            header_idx, col_map = _detect_header_row(table_clean)
            if header_idx is None: continue
            for row in table_clean[header_idx + 1:]:
                if not any(row): continue
                date_col = col_map.get('date')
                if date_col is None or date_col >= len(row): continue
                date_ofx = _parse_date_universal(row[date_col], year_hint)
                if not date_ofx: continue
                label_col = col_map.get('label')
                label = row[label_col].strip() if (label_col is not None and label_col < len(row)) else row[date_col]
                label_up = label.upper().strip()
                if not label or len(label) < 2: continue
                if any(skip in label_up for skip in SKIP_LABELS): continue
                if re.match(r'^[\d\s.,\-]+$', label): continue
                amount = None
                if 'debit' in col_map and 'credit' in col_map:
                    d_col, c_col = col_map['debit'], col_map['credit']
                    dv = _parse_amount_cell(row[d_col] if d_col < len(row) else '')
                    cv = _parse_amount_cell(row[c_col] if c_col < len(row) else '')
                    if dv and dv > 0: amount = -dv
                    elif cv and cv > 0: amount = cv
                elif 'amount' in col_map:
                    a_col = col_map['amount']
                    amount = _parse_amount_cell(row[a_col] if a_col < len(row) else '')
                if amount is None or amount == 0.0: continue
                name, memo = smart_label(label, [])
                txn = _make_txn(date_ofx, amount, name, memo)
                if txn: txns.append(txn)
    return info, [t for t in txns if t is not None]

def _make_african_parser(bank_name):
    def _parser(pages_words, pages_text, _pdf_path=''):
        if _pdf_path and Path(_pdf_path).exists():
            return _universal_parse_path(_pdf_path, pages_text)
        return _extract_universal_header(pages_text), []
    return _parser

parse_cbao      = _make_african_parser('CBAO')
parse_bci       = _make_african_parser('BCI')
parse_coris     = _make_african_parser('Coris')
parse_orabank   = _make_african_parser('Orabank')
parse_boa       = _make_african_parser('BOA')
parse_atb       = _make_african_parser('ATB')
parse_bnde      = _make_african_parser('BNDE')
parse_universal = _make_african_parser('Universal')

# Parseurs africains dédiés simplifiés (utilisent le moteur universel via pdf_path)
def parse_ecobank(pages_words, pages_text, _pdf_path=''):
    if _pdf_path and Path(_pdf_path).exists():
        return _universal_parse_path(_pdf_path, pages_text)
    return _extract_universal_header(pages_text), []

def parse_bsic(pages_words, pages_text, _pdf_path=''):
    if _pdf_path and Path(_pdf_path).exists():
        return _universal_parse_path(_pdf_path, pages_text)
    return _extract_universal_header(pages_text), []

def parse_bis(pages_words, pages_text, _pdf_path=''):
    return _extract_universal_header(pages_text), []

def parse_uba(pages_words, pages_text, _pdf_path=''):
    if _pdf_path and Path(_pdf_path).exists():
        return _universal_parse_path(_pdf_path, pages_text)
    return _extract_universal_header(pages_text), []

def parse_sg_afrique(pages_words, pages_text, _pdf_path=''):
    if _pdf_path and Path(_pdf_path).exists():
        return _universal_parse_path(_pdf_path, pages_text)
    return _extract_universal_header(pages_text), []


# ════════════════════════════════════════════════════════════════════════════
# DEVISE & LABELS
# ════════════════════════════════════════════════════════════════════════════

BANK_CURRENCY = {
    'QONTO':'EUR','LCL':'EUR','CA':'EUR','CE':'EUR','BP':'EUR','CIC':'EUR',
    'CGD':'EUR','LBP':'EUR','SG':'EUR','BNP':'EUR','MYPOS':'EUR','SHINE':'EUR',
    'CBAO':'XOF','ECOBANK':'XOF','BCI':'XOF','CORIS':'XOF','UBA':'XOF',
    'ORABANK':'XOF','BOA':'XOF','ATB':'TND','SG_AFRIQUE':'XOF','BSIC':'XOF',
    'BIS':'XOF','BNDE':'XOF','UNIVERSAL':'XOF',
}

BANK_LABELS = {
    'QONTO':'Qonto','LCL':'LCL (Crédit Lyonnais)','CA':'Crédit Agricole',
    'CE':"Caisse d'Épargne",'BP':'Banque Populaire','CIC':'CIC',
    'CGD':'Caixa Geral de Depositos','LBP':'La Banque Postale',
    'SG':'Société Générale','BNP':'BNP Paribas','MYPOS':'myPOS',
    'SHINE':'Shine (néo-banque pro)','CBAO':'CBAO (Sénégal)',
    'ECOBANK':'Ecobank','BCI':'BCI','CORIS':'Coris Bank','UBA':'UBA',
    'ORABANK':'Orabank','BOA':'Bank of Africa','ATB':'Arab Tunisian Bank',
    'SG_AFRIQUE':'Société Générale Afrique','BSIC':'BSIC (Sénégal)',
    'BIS':'Banque Islamique du Sénégal','BNDE':'BNDE','UNIVERSAL':'Format universel',
}

AFRICAN_BANKS = {'CBAO','ECOBANK','BCI','CORIS','UBA','ORABANK','BOA','ATB',
                 'SG_AFRIQUE','UNIVERSAL','BSIC','BIS','BNDE'}

PARSERS = {
    'QONTO':parse_qonto,'LCL':parse_lcl,'CA':parse_ca,'CE':parse_ce,
    'BP':parse_bp,'CIC':parse_cic,'CGD':parse_cgd,'LBP':parse_lbp,
    'SG':parse_sg,'BNP':parse_bnp,'MYPOS':parse_mypos,'SHINE':parse_shine,
    'CBAO':parse_cbao,'ECOBANK':parse_ecobank,'BCI':parse_bci,'CORIS':parse_coris,
    'UBA':parse_uba,'ORABANK':parse_orabank,'BOA':parse_boa,'ATB':parse_atb,
    'SG_AFRIQUE':parse_sg_afrique,'UNIVERSAL':parse_universal,
    'BSIC':parse_bsic,'BIS':parse_bis,'BNDE':parse_bnde,
}


# ════════════════════════════════════════════════════════════════════════════
# GÉNÉRATION OFX
# ════════════════════════════════════════════════════════════════════════════

def period_to_ofx(date_str):
    try:
        p = date_str.split('/')
        return f"{p[2]}{p[1].zfill(2)}{p[0].zfill(2)}"
    except:
        return datetime.now().strftime('%Y%m%d')

def generate_ofx(info, txns, target='quadra', currency='EUR'):
    bid, brid, aid = iban_to_rib(info.get('iban',''))
    ds  = period_to_ofx(info.get('period_start',''))
    de  = period_to_ofx(info.get('period_end',''))
    dn  = datetime.now().strftime('%Y%m%d%H')
    bal = info.get('balance_close', 0.0)
    memo_carries_label = target in ('myunisoft','sage','ebp')
    lines = [
        'OFXHEADER:100','DATA:OFXSGML','VERSION:102','SECURITY:NONE',
        'ENCODING:USASCII','CHARSET:1252','COMPRESSION:NONE',
        'OLDFILEUID:NONE','NEWFILEUID:NONE',
        '<OFX>','<SIGNONMSGSRSV1>','<SONRS>','<STATUS>',
        '<CODE>0','<SEVERITY>INFO','</STATUS>',
        f'<DTSERVER>{dn}','<LANGUAGE>FRA',
        '</SONRS>','</SIGNONMSGSRSV1>',
        '<BANKMSGSRSV1>','<STMTTRNRS>','<TRNUID>00',
        '<STATUS>','<CODE>0','<SEVERITY>INFO','</STATUS>',
        '<STMTRS>',f'<CURDEF>{currency}','<BANKACCTFROM>',
        f'<BANKID>{bid}',f'<BRANCHID>{brid}',
        f'<ACCTID>{aid}','<ACCTTYPE>CHECKING','</BANKACCTFROM>',
        '<BANKTRANLIST>',f'<DTSTART>{ds}',f'<DTEND>{de}',
    ]
    for t in txns:
        name = t['name']
        memo = t.get('memo', '') or ''
        if memo_carries_label:
            name_tag = name
            memo_tag = (name + ' | ' + memo) if memo else name
        else:
            name_tag = name
            memo_tag = memo
        lines += [
            '<STMTTRN>',
            f"<TRNTYPE>{t['type']}",
            f"<DTPOSTED>{t['date']}",
            f"<TRNAMT>{t['amount']:.2f}",
            f"<FITID>{t['fitid']}",
            '<NAME>' + name_tag,
            '<MEMO>' + memo_tag,
            '</STMTTRN>',
        ]
    lines += [
        '</BANKTRANLIST>',
        '<LEDGERBAL>',f'<BALAMT>{bal:.2f}',f'<DTASOF>{dn}','</LEDGERBAL>',
        '<AVAILBAL>',f'<BALAMT>{bal:.2f}',f'<DTASOF>{dn}','</AVAILBAL>',
        '</STMTRS>','</STMTTRNRS>','</BANKMSGSRSV1>','</OFX>',
    ]
    return '\n'.join(lines) + '\n'


# ════════════════════════════════════════════════════════════════════════════
# FONCTION PRINCIPALE DE CONVERSION (avec cache Streamlit)
# ════════════════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner=False)
def process_pdf(file_bytes: bytes, filename: str):
    """
    Traite un PDF (bytes) et retourne (bank, info, txns, error).
    Mis en cache par Streamlit — évite de ré-analyser si le fichier n'a pas changé.
    """
    if not _PDFPLUMBER_OK:
        return None, {}, [], "pdfplumber n'est pas installé. Ajoutez-le à requirements.txt."

    # Écrire dans un fichier temporaire (pdfplumber a besoin d'un chemin)
    with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        pages_words = extract_words_by_page(tmp_path)
        pages_text  = extract_text_by_page(tmp_path)

        if not _pdf_has_text(pages_text):
            if not _OCR_AVAILABLE:
                return None, {}, [], (
                    "Ce PDF semble scanné (aucun texte extractible). "
                    "L'OCR n'est pas disponible sur ce serveur. "
                    "Veuillez fournir un PDF avec texte sélectionnable."
                )
            pages_text  = _ocr_pdf(tmp_path)
            pages_words = []

        bank = detect_bank(pages_text)

        if bank in AFRICAN_BANKS:
            info, txns = PARSERS[bank](pages_words, pages_text, _pdf_path=tmp_path)
        else:
            info, txns = PARSERS[bank](pages_words, pages_text)

        return bank, info, txns, None

    except Exception as e:
        logger.error("Erreur traitement %s : %s", filename, e, exc_info=True)
        return None, {}, [], str(e)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
# INTERFACE STREAMLIT
# ════════════════════════════════════════════════════════════════════════════

def fmt_amount(amount: float, currency: str) -> str:
    if currency == 'EUR':
        return f"{abs(amount):,.2f} €".replace(",", "\u202f")
    else:
        return f"{abs(amount):,.0f} {currency}".replace(",", "\u202f")


def main():
    st.set_page_config(
        page_title="OFX Bridge",
        page_icon="💱",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # ── CSS personnalisé ──────────────────────────────────────────────────────
    st.markdown("""
    <style>
      .stApp { background-color: #0b1120; }
      .block-container { padding-top: 2rem; }

      /* Titres et textes */
      h1, h2, h3 { color: #e2e8f0 !important; }
      p, label, .stMarkdown { color: #94a3b8; }

      /* Sidebar */
      [data-testid="stSidebar"] { background-color: #0e1628; border-right: 1px solid #1e3250; }
      [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 { color: #e2e8f0 !important; }

      /* Metric cards */
      [data-testid="metric-container"] {
        background: #152035; border: 1px solid #1e3250;
        border-radius: 8px; padding: 12px;
      }
      [data-testid="stMetricValue"] { color: #e2e8f0 !important; font-size: 1.2rem !important; }
      [data-testid="stMetricLabel"] { color: #94a3b8 !important; }

      /* Bouton primaire */
      .stDownloadButton button {
        background: #10b981 !important; color: white !important;
        border-radius: 8px !important; border: none !important;
        font-weight: 600 !important; padding: 0.5rem 1.5rem !important;
      }
      .stDownloadButton button:hover { background: #059669 !important; }

      /* File uploader */
      [data-testid="stFileUploader"] {
        background: #152035; border: 2px dashed #1e3250;
        border-radius: 10px; padding: 1rem;
      }

      /* Dataframe / table */
      [data-testid="stDataFrame"] { border: 1px solid #1e3250; border-radius: 8px; }

      /* Success / info / warning / error */
      [data-testid="stAlert"] { border-radius: 8px; }

      /* Badge banque */
      .bank-badge {
        background: #1e3a5f; color: #3b82f6;
        padding: 3px 10px; border-radius: 20px;
        font-size: 0.85rem; font-weight: 600;
        display: inline-block; margin-bottom: 8px;
      }
      .debit-cell { color: #f43f5e !important; font-weight: 600; }
      .credit-cell { color: #10b981 !important; font-weight: 600; }

      /* Séparateur */
      hr { border-color: #1e3250; }
    </style>
    """, unsafe_allow_html=True)

    # ── SIDEBAR ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("## 💱 OFX Bridge")
        st.markdown("**v2.1** — Convertisseur PDF → OFX")
        st.markdown("---")

        # Format logiciel comptable
        st.markdown("### ⚙️ Format OFX")
        target = st.selectbox(
            "Logiciel cible",
            options=["Quadra / Cegid", "MyUnisoft", "Sage", "EBP"],
            index=0,
            help="Affecte la disposition NAME/MEMO dans le fichier OFX."
        )
        target_map = {"Quadra / Cegid": "quadra", "MyUnisoft": "myunisoft",
                      "Sage": "sage", "EBP": "ebp"}
        target_code = target_map[target]

        st.markdown("---")
        st.markdown("### 🏦 Banques supportées")
        banks_list = [
            "🇫🇷 Qonto", "🇫🇷 LCL", "🇫🇷 Crédit Agricole", "🇫🇷 Caisse d'Épargne",
            "🇫🇷 Banque Populaire", "🇫🇷 CIC", "🇫🇷 La Banque Postale",
            "🇫🇷 Société Générale", "🇫🇷 BNP Paribas", "🇵🇹 CGD",
            "🌍 myPOS", "🇫🇷 Shine",
            "🌍 CBAO, Ecobank, BCI, Coris", "🌍 UBA, Orabank, BOA",
            "🌍 ATB, BSIC, BIS, BNDE", "🔍 Format universel",
        ]
        for b in banks_list:
            st.markdown(f"<small>{b}</small>", unsafe_allow_html=True)

        st.markdown("---")
        st.markdown("""
        <div style="background:#1e3a5f;border-radius:8px;padding:10px 14px;margin-top:8px">
        🔒 <strong style="color:#3b82f6">100 % local</strong><br>
        <small style="color:#94a3b8">Vos relevés ne quittent jamais ce serveur.
        Aucun envoi cloud, aucune IA distante.</small>
        </div>
        """, unsafe_allow_html=True)

    # ── CONTENU PRINCIPAL ─────────────────────────────────────────────────────
    st.markdown("# 💱 OFX Bridge")
    st.markdown("Convertissez vos relevés bancaires PDF en fichiers **OFX** importables dans Quadra, MyUnisoft, Sage ou EBP.")

    # Vérification dépendances
    if not _PDFPLUMBER_OK:
        st.error("❌ **pdfplumber n'est pas installé.** Ajoutez `pdfplumber` à votre `requirements.txt` et relancez l'application.")
        st.stop()

    st.markdown("---")

    # ── Upload ────────────────────────────────────────────────────────────────
    st.markdown("### 📂 Sélection des fichiers PDF")
    uploaded_files = st.file_uploader(
        "Déposez un ou plusieurs relevés bancaires au format PDF",
        type=["pdf"],
        accept_multiple_files=True,
        help="Formats supportés : PDF natif (texte extractible). Les PDF scannés nécessitent Tesseract OCR."
    )

    if not uploaded_files:
        st.info("👆 Déposez un relevé PDF pour commencer la conversion.")
        # Petite démo d'état vide
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Transactions", "—")
        with col2:
            st.metric("Total Débits", "—")
        with col3:
            st.metric("Total Crédits", "—")
        st.stop()

    # ── Traitement de chaque fichier ──────────────────────────────────────────
    for uploaded_file in uploaded_files:
        st.markdown(f"---")
        st.markdown(f"### 📄 `{uploaded_file.name}`")

        file_bytes = uploaded_file.read()

        with st.spinner(f"Analyse de {uploaded_file.name}…"):
            bank, info, txns, error = process_pdf(file_bytes, uploaded_file.name)

        if error:
            st.error(f"❌ **Erreur :** {error}")
            continue

        if not txns:
            st.warning("⚠️ Aucune transaction détectée dans ce fichier. Vérifiez qu'il s'agit bien d'un relevé bancaire.")
            continue

        # ── Badge banque ──────────────────────────────────────────────────────
        bank_label = BANK_LABELS.get(bank, bank)
        currency   = BANK_CURRENCY.get(bank, 'EUR')
        st.markdown(f'<span class="bank-badge">🏦 {bank_label}</span>', unsafe_allow_html=True)

        # Infos période / IBAN
        col_a, col_b = st.columns(2)
        with col_a:
            period_str = ""
            if info.get('period_start') and info.get('period_end'):
                period_str = f"{info['period_start']} → {info['period_end']}"
            st.markdown(f"**Période :** {period_str or '—'}")
        with col_b:
            iban_display = info.get('iban', '') or '—'
            st.markdown(f"**IBAN / Compte :** `{iban_display}`")

        # ── Métriques ─────────────────────────────────────────────────────────
        total_debit  = sum(abs(t['amount']) for t in txns if t['type'] == 'DEBIT')
        total_credit = sum(t['amount']      for t in txns if t['type'] == 'CREDIT')

        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("📊 Transactions", f"{len(txns)}")
        with col2:
            st.metric("🔴 Total Débits",  fmt_amount(total_debit,  currency))
        with col3:
            st.metric("🟢 Total Crédits", fmt_amount(total_credit, currency))

        # ── Tableau de transactions ───────────────────────────────────────────
        st.markdown("#### Aperçu des transactions")

        import pandas as pd
        rows_data = []
        for t in txns:
            d = t['date']
            date_fmt = f"{d[6:8]}/{d[4:6]}/{d[0:4]}"
            is_debit = t['type'] == 'DEBIT'
            rows_data.append({
                "Date":    date_fmt,
                "Type":    "💸 Débit" if is_debit else "💰 Crédit",
                "Libellé": t['name'],
                "Mémo":    t.get('memo', '') or '',
                "Débit":   fmt_amount(abs(t['amount']), currency) if is_debit  else "",
                "Crédit":  fmt_amount(t['amount'],      currency) if not is_debit else "",
            })

        df = pd.DataFrame(rows_data)
        st.dataframe(
            df,
            use_container_width=True,
            height=min(400, 40 + 35 * len(df)),
            hide_index=True,
            column_config={
                "Date":    st.column_config.TextColumn("Date",    width=90),
                "Type":    st.column_config.TextColumn("Type",    width=100),
                "Libellé": st.column_config.TextColumn("Libellé", width=250),
                "Mémo":    st.column_config.TextColumn("Mémo",    width=200),
                "Débit":   st.column_config.TextColumn("Débit",   width=110),
                "Crédit":  st.column_config.TextColumn("Crédit",  width=110),
            }
        )

        # ── Génération et téléchargement OFX ─────────────────────────────────
        st.markdown("#### 📥 Télécharger le fichier OFX")

        ofx_content = generate_ofx(info, txns, target=target_code, currency=currency)
        ofx_bytes   = ofx_content.encode('latin-1', errors='replace')
        ofx_name    = Path(uploaded_file.name).stem + ".ofx"

        col_dl1, col_dl2 = st.columns([1, 3])
        with col_dl1:
            st.download_button(
                label=f"⬇️ Télécharger {ofx_name}",
                data=ofx_bytes,
                file_name=ofx_name,
                mime="application/x-ofx",
                key=f"dl_{uploaded_file.name}",
            )
        with col_dl2:
            st.caption(
                f"Format : OFX Standard ({target}) · "
                f"Devise : {currency} · "
                f"{len(txns)} transactions · "
                f"Encodage : Latin-1 (compatible Cegid/MyUnisoft)"
            )

    # ── Footer ────────────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown(
        "<small style='color:#4a6080'>OFX Bridge v2.1 — Traitement 100 % local · "
        "Aucune donnée envoyée vers un serveur externe</small>",
        unsafe_allow_html=True
    )


if __name__ == "__main__":
    main()
