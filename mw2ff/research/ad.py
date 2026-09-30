import sys,re
from d import dis
from xref import func_start, calls
NAMES={0x821e2e98:'Load_Stream',0x821e2d18:'Push',0x821e2da0:'Pop',0x821e2e28:'Align',0x821e2e40:'IncPos',0x821e2e58:'InsertPtr',
0x821e2f10:'ConvAlias',0x821e2f38:'ConvPtr',0x821e2f60:'XStringCustom',0x821de528:'DB_AddXAsset',0x821e25b0:'DB_FindXAssetHeader',
0x822a2e18:'SL_GetString',0x821dd038:'Hunk_Alloc',0x8241a890:'memset',0x821e2fe0:'Load_TempString'}
def fn_end(a):
    # until next mflr r12 function start or blr followed by padding
    b=a+4
    from xref import pe,B
    while True:
        w=pe[b-B:b-B+4]
        if w==bytes.fromhex('7d8802a6'): return b
        b+=4
        if b-a>0x4000: return b
def ann(a,n=None):
    end=fn_end(a) if n is None else a+4*n
    for line in dis(a,(end-a)//4):
        m=re.search(r'(bl?) 0x([0-9a-f]+)',line)
        if m:
            t=int(m.group(2),16)
            if t in NAMES: line+='   <'+NAMES[t]+'>'
        m=re.search(r'(-?0x[0-9a-f]+)\((r\d+)\)',line)
        print(line)
if __name__=='__main__':
    for x in sys.argv[1:]:
        print('====',x); ann(int(x,16))
