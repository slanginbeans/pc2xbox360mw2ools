import tu6
import sys,pickle,os,struct
from run1 import *
import run1, mw2tex
def oracle(path):
    st=os.stat(path)   # (keyed by size and time too: a converted map is rewritten under one name)
    out=os.path.join(tu6.CACHE,'%s.%d.%d.oracle'%(os.path.basename(path),st.st_size,st.st_mtime_ns))
    if os.path.exists(out): return pickle.load(open(out,'rb'))
    os.makedirs(os.path.dirname(out),exist_ok=True)
    zone=mw2tex.FastFile(path).zone
    run1.ASSETS.clear()
    e=run(zone)
    reads={}
    for (pos,n,lr,dst) in e.trace:
        if pos in reads: continue
        blk=-1; off=dst
        for i,b in enumerate(e.blocks):
            if b<=dst<b+max(e.blocksizes[i],1)+0x10000: blk=i; off=dst-b; break
        reads[pos]=(n,blk,off,lr)
    res={'err':e.err,'end':e.pos,'reads':reads,'assets':list(run1.ASSETS)}
    pickle.dump(res,open(out,'wb'))
    return res
if __name__=='__main__':
    r=oracle(sys.argv[1]); print(r['err'],r['end'],len(r['reads']),len(r['assets']))
