import sys,os,traceback
import tu6
import mw2tex, zone as Z
from oracle import oracle
from xref import func_start
def compare(path, memcheck=True, verbose=True):
    o=oracle(path)
    zb=mw2tex.FastFile(path).zone
    r=Z.Reader(zb,trace=True)
    err=None
    try: r.walk()
    except Exception as ex: err=ex; tb=traceback.format_exc()
    # compare
    reads=o['reads']
    first_bad=None
    for (pos,n,blk,off,where) in r.trace:
        if pos not in reads:
            first_bad=('pos not an oracle read start',pos,n,blk,off); break
        on,ob,oo,lr=reads[pos]
        if memcheck and (ob,oo)!=(blk,off):
            first_bad=("dst mismatch",pos,n,(blk,off),(ob,oo),hex(func_start(lr) or 0),where); break
    cover=r.pos
    if verbose:
        print(os.path.basename(path),'reader end',r.pos,'oracle end',o['end'],'assets',len(r.assets),'/',len(o['assets']))
        if first_bad: print('FIRST MISMATCH',first_bad)
        if err: print('ERROR',tb[-1500:])
    return r,o,first_bad,err
if __name__=='__main__':
    compare(sys.argv[1], memcheck='--nomem' not in sys.argv)
def mytrace(r,a,b):
    for t in r.trace:
        if a<=t[0]<b: print(t)
