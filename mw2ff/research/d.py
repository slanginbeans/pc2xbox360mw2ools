import sys,capstone,struct
from xref import pe,B,calls,refs
md=capstone.Cs(capstone.CS_ARCH_PPC, capstone.CS_MODE_32|capstone.CS_MODE_BIG_ENDIAN)
def dis(a,n):
    out=[]
    for k in range(n):
        x=a+k*4; b=pe[x-B:x-B+4]
        ins=list(md.disasm(b,x))
        s='%s %s'%(ins[0].mnemonic,ins[0].op_str) if ins else '.word 0x%s'%b.hex()
        out.append('%x: %s'%(x,s))
    return out
if __name__=='__main__':
    a=int(sys.argv[1],16); n=int(sys.argv[2]) if len(sys.argv)>2 else 40
    print('\n'.join(dis(a,n)))
