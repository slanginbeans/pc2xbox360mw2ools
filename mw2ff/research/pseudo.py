import sys,re,struct
import capstone
from xref import pe,B,func_start
from ad import NAMES, fn_end
md=capstone.Cs(capstone.CS_ARCH_PPC, capstone.CS_MODE_32|capstone.CS_MODE_BIG_ENDIAN)
md.detail=False
VARN={}
def s(v):
    if v is None: return '?'
    k=v[0]
    if k=='c': return hex(v[1]) if abs(v[1])>9 else str(v[1])
    if k=='g': return 'g%x'%v[1]
    if k=='ld': return '%s[%s]'%(s(v[1]),hex(v[2]))
    if k=='add': return '(%s+%s)'%(s(v[1]),hex(v[2]))
    if k=='r': return v[1]
    if k=='op': return '(%s %s %s)'%(s(v[2]),v[1],s(v[3]))
    return str(v)
def pseudo(a,end=None):
    end=end or fn_end(a)
    R={}
    out=[]
    for i in md.disasm(pe[a-B:end-B],a):
        m,o=i.mnemonic,i.op_str
        ops=[x.strip() for x in o.split(',')] if o else []
        def reg(x): return R.get(x,('r',x))
        def mem(x):
            mm=re.match(r'(-?0x[0-9a-f]+|-?\d+)\((r\d+)\)',x)
            return int(mm.group(1),0),mm.group(2)
        line=None
        if m=='lis': R[ops[0]]=('c',(int(ops[1],0)<<16)&0xffffffff)
        elif m=='li': R[ops[0]]=('c',int(ops[1],0))
        elif m=='addi':
            b=reg(ops[1]); k=int(ops[2],0)
            if b[0]=='c': R[ops[0]]=('g',(b[1]+k)&0xffffffff)
            elif b[0]=='g': R[ops[0]]=('g',(b[1]+k)&0xffffffff)
            else: R[ops[0]]=('add',b,k)
        elif m in('lwz','lbz','lhz','lha','lwzx'):
            if m=='lwzx': R[ops[0]]=('ld',('op','+',reg(ops[1]),reg(ops[2])),0); continue
            off,b=mem(ops[1]); bv=reg(b)
            if bv[0]=='c': R[ops[0]]=('g',(bv[1]+off)&0xffffffff); R[ops[0]]=('ld',('g',(bv[1]+off)&0xffffffff),0)
            else: R[ops[0]]=('ld',bv,off)
            if m!='lwz': R[ops[0]]=(R[ops[0]][0],R[ops[0]][1],R[ops[0]][2]) 
            if m=='lbz': R[ops[0]]=('ld',R[ops[0]][1],R[ops[0]][2]) ; 
            if m in ('lbz','lhz','lha'): out.append('  %x: %s = %s  (%s)'%(i.address,ops[0],s(R[ops[0]]),m))
        elif m in ('stw','stb','sth'):
            off,b=mem(ops[1]); bv=reg(b)
            tgt=('ld',('g',(bv[1]+off)&0xffffffff),0) if bv[0]=='c' else ('ld',bv,off)
            line='%s := %s'%(s(tgt),s(reg(ops[0])))+('' if m=='stw' else ' ('+m+')')
        elif m=='mr': R[ops[0]]=reg(ops[1])
        elif m in ('slwi','mulli','add','subf','mullw','rlwinm','clrlwi','srwi','extsh','extsb','addic.','addic','subfic','neg'):
            args=[reg(x) if x.startswith('r') else ('c',int(x,0)) for x in ops[1:]]
            R[ops[0]]=('op',m,args[0],args[1] if len(args)>1 else ('c',0))
        elif m in ('cmpwi','cmplwi','cmpw','cmplw'):
            if len(ops)==3: cr,x,y=ops
            else: cr='cr0';x,y=ops
            yv=reg(y) if y.startswith('r') else ('c',int(y,0))
            line='cmp %s, %s'%(s(reg(x)),s(yv))
        elif m.startswith('b') and m not in ('bl','b','blr','bctr','bctrl'):
            line='%s %s'%(m,o)
        elif m=='bl':
            t=int(o,16)
            nm=NAMES.get(t,'F_%x'%t)
            args=','.join(s(reg('r%d'%k)) for k in (3,4,5))
            line='CALL %s(%s)'%(nm,args)
            for k in range(0,13): R.pop('r%d'%k,None)
            R['r3']=('r','ret_'+nm)
        elif m=='b':
            t=int(o,16); line='goto %x'%t+(('  <%s>'%NAMES[t]) if t in NAMES else ('' if a<=t<end else '  (tail F_%x)'%t))
        elif m in ('blr',): line='return'
        elif m in ('mflr','mtlr','stwu','std','ld','nop'): pass
        else:
            line='%s %s'%(m,o)
            if ops and ops[0].startswith('r'): R.pop(ops[0],None)
        if line: out.append('  %x: %s'%(i.address,line))
    return out
if __name__=='__main__':
    for x in sys.argv[1:]:
        a=int(x,16); print('== F_%x'%a); print('\n'.join(pseudo(a)))
