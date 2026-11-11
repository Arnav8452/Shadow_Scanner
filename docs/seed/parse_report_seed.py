"""Seed parser for reference 'Trade History' PDFs (used to mine strategies) (proven on three reports, 9 Nov 2026).

Usage: pdftotext -layout report.pdf report.txt && python parse_report.py report.txt out.csv
Validation required after parsing (see docs/SPEC.md §9): per-day net == stated Daily Net,
and (exit - entry) * qty * side == gross for every row. Port into the package with tests;
inputs live in data/private/ only; anonymize outputs.
"""
import re, csv, sys
L=open(sys.argv[1]).read().replace('\f','\n').split('\n')
tre=re.compile(r'^\s*(\d{2}:\d{2}:\d{2})\b')
head=re.compile(r'\s(\d{4})\s+([A-Z&][A-Z0-9&]*)\s+(BUY|SELL)\s+(\d+)\s+(\S+)')
head2=re.compile(r'^\s+([A-Z&][A-Z0-9&]*\d[A-Z0-9&]*)\s+(BUY|SELL)\s+(\d+)\s+(\S+)')
amt=re.compile(r'₹\s*(-?[\d,]+\.\d+)')
dec=re.compile(r'(?<![\d,\.+\-₹])([\d,]*\d\.\d+)')
date=None; rows=[]; used=set()
for j,l in enumerate(L):
    m=re.match(r'^\s*(\d{4}-\d{2}-\d{2})\s+True Equity',l)
    if m: date=m.group(1); continue
    if tre.match(l): continue
    mm=head.search(l); acct=None
    if mm: acct,inst,side,qty,mg=mm.groups()
    else:
        m2=head2.match(l)
        if not m2: continue
        inst,side,qty,mg=m2.groups(); mm=m2
        for k in range(j-1,max(j-4,0),-1):
            a=re.match(r'^\s*\d{2}:\d{2}:\d{2}\s+(\d{4})',L[k])
            if a: acct=a.group(1); break
    rest=l[mm.end():]; amline=l
    if '₹' not in l: rest+=' '+L[j+1]; amline=L[j+1]
    ps=[float(x.replace(',','')) for x in dec.findall(amt.sub(' ',rest))]
    a=amt.findall(amline)
    if len(ps)<2 or len(a)<2: print('SKIP',date,l.strip()[:120]); continue
    tin=tout=None; mo=''
    for k in range(j-1,max(j-7,0),-1):
        t=tre.match(L[k])
        if t and k not in used: tin=t.group(1); used.add(k); break
    for k in range(j+1,min(j+7,len(L))):
        t=tre.match(L[k])
        if t: tout=t.group(1); used.add(k); x=re.search(r'(MARKET|LIMIT|SL_M)',L[k]); mo=x.group(1) if x else ''; break
    blk=' '.join(L[max(j-3,0):j+5])
    if 'SL_M' in l and not mo: mo='SL_M'
    g=float(a[0].replace(',','')); n=float(a[-1].replace(',',''))
    br=re.search(r'\(\+\s*₹?\s*([\d,]+)',blk) if 'BREACH' in blk else None
    rows.append(dict(date=date,tin=tin,tout=tout,acct=acct,inst=inst,side=side,qty=int(qty),margin=mg,entry=ps[0],exit=ps[1],gross=g,net=n,charges=round(g-n,2),flag='QTY_MISMATCH' in blk,breach=float(br.group(1).replace(',','')) if br else 0.0,live='LIVE' in blk,mod_out=mo))
w=csv.DictWriter(open(sys.argv[2],'w'),fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
print(len(rows))
