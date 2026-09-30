import sys,re
from pseudo import pseudo
from ad import NAMES
SKIP={0x8241a768,0x8241a778,0x8241a77c,0x8241a774,0x8241a770,0x8241a75c,0x8241a750,0x821b2698,0x821b2728}
NAMES[0x821b2698]='Load_XString'; NAMES[0x821b2728]='Load_XStringArray'
def tree(a,seen,out):
    if a in seen: return
    seen.add(a)
    lines=pseudo(a)
    lines=[l for l in lines if ': r1[' not in l]
    out.append('== F_%x'%a); out.extend(lines)
    for l in lines:
        for m in re.finditer(r'F_([0-9a-f]{8})',l):
            t=int(m.group(1),16)
            if 0x821b0000<=t<0x821e3000 and t not in SKIP and 'tail' not in l: tree(t,seen,out)
if __name__=='__main__':
    out=[]; seen=set()
    for x in sys.argv[1:]: tree(int(x,16),seen,out)
    print('\n'.join(out))
